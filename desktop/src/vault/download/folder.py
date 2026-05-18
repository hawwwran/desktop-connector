"""Folder downloads: ``download_folder``.

Streams every file in a remote folder to a local subtree. Differs from
the single-file flow in that each file's plaintext is streamed through
:func:`paths.atomic_write_chunks` rather than buffered into memory, so
folder size isn't bound by RAM. The chunk-presence check spans every
unique chunk across the folder so partial misses surface before any
local file is created.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from ..atomic import fsync_dir
from ..binding.lifecycle import SyncCancelledError
from ..manifest import normalize_manifest_plaintext
from .cache import _load_cached_chunk, _store_cached_chunk
from .chunks import (
    _decrypt_chunk,
    _ensure_all_chunks_present,
    _get_chunk_with_retry,
)
from .manifest import (
    _folder_file_plans,
    _int_value,
    _unique_chunk_ids,
)
from .paths import (
    _preflight_folder_disk_space,
    atomic_write_chunks,
    resolve_folder_destination,
)
from .single_file import _report
from .types import (
    ChunkRelay,
    DownloadProgress,
    DownloadVault,
    ExistingFilePolicy,
)


log = logging.getLogger(__name__)


def download_folder(
    *,
    vault: DownloadVault,
    relay: ChunkRelay,
    manifest: dict[str, Any],
    path: str,
    destination: Path,
    existing_policy: ExistingFilePolicy = "fail",
    chunk_cache_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> Path:
    """Download a remote folder's current, non-deleted tree.

    Review §3.H2: ``should_continue`` is checked between every chunk
    fetch (and between files) so a folder-restore hitting a missing
    chunk bails within ~1 chunk's worth of network work instead of
    burning ``4 × 60 s × N`` retries on each missing chunk in the
    plan. Matches the single-file download's F-U03 cancellation
    contract.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    normalized = normalize_manifest_plaintext(manifest)
    plans = _folder_file_plans(normalized, path)
    final_root = resolve_folder_destination(Path(destination), existing_policy)
    total_logical_size = sum(_int_value(plan.version.get("logical_size")) for plan in plans)
    _preflight_folder_disk_space(final_root, total_logical_size)

    total_chunks = sum(len(plan.chunks) for plan in plans)
    chunk_ids = _unique_chunk_ids(plan.chunks for plan in plans)
    _report(progress, "checking", 0, total_chunks)
    heads = _ensure_all_chunks_present(
        relay=relay,
        vault_id=vault.vault_id,
        vault_access_secret=vault.vault_access_secret,
        chunk_ids=chunk_ids,
    )

    final_root.mkdir(parents=True, exist_ok=True)
    completed = 0
    bytes_written = 0
    for plan in plans:
        if should_continue is not None and not should_continue():
            log.info(
                "vault.download.folder_cancelled vault=%s path=%s "
                "files_done=%d chunks_done=%d total_chunks=%d",
                vault.vault_id, path,
                plans.index(plan), completed, total_chunks,
            )
            raise SyncCancelledError(
                f"folder download cancelled at chunk {completed}/{total_chunks} "
                f"of {path}",
            )
        file_path = final_root / plan.relative_path

        def plaintext_chunks(plan=plan) -> Iterable[bytes]:
            nonlocal completed, bytes_written
            for chunk in plan.chunks:
                if should_continue is not None and not should_continue():
                    log.info(
                        "vault.download.folder_cancelled vault=%s path=%s "
                        "chunks_done=%d total_chunks=%d",
                        vault.vault_id, path, completed, total_chunks,
                    )
                    raise SyncCancelledError(
                        f"folder download cancelled at chunk "
                        f"{completed}/{total_chunks} of {path}",
                    )
                encrypted = _load_cached_chunk(
                    chunk_cache_dir=chunk_cache_dir,
                    vault_id=vault.vault_id,
                    chunk_id=chunk["chunk_id"],
                    head=heads[chunk["chunk_id"]],
                )
                if encrypted is None:
                    encrypted = _get_chunk_with_retry(
                        relay=relay,
                        vault_id=vault.vault_id,
                        vault_access_secret=vault.vault_access_secret,
                        chunk_id=chunk["chunk_id"],
                        should_continue=should_continue,
                    )

                plaintext = _decrypt_chunk(
                    vault=vault,
                    remote_folder_id=plan.remote_folder_id,
                    file_id=plan.file_id,
                    version_id=str(plan.version.get("version_id", "")),
                    chunk=chunk,
                    encrypted=encrypted,
                )
                _store_cached_chunk(chunk_cache_dir, vault.vault_id, chunk["chunk_id"], encrypted)
                completed += 1
                bytes_written += len(plaintext)
                _report(progress, "downloading", completed, total_chunks, bytes_written)
                yield plaintext

        written_for_file = atomic_write_chunks(file_path, plaintext_chunks())
        expected_size = _int_value(plan.version.get("logical_size"))
        if expected_size and written_for_file != expected_size:
            raise ValueError(
                f"downloaded size mismatch for {plan.display_path}: "
                f"expected {expected_size}, got {written_for_file}"
            )

    # Review §3.M6 — fsync every distinct directory we wrote into. The
    # per-file ``atomic_write_chunks`` fsyncs the file data, but the
    # directory entries (filename → inode pointers) only land on disk
    # when the directory itself is fsynced. Without this, a power loss
    # mid-restore of a thousand-file folder could survive every file's
    # bytes but lose the entries that name them — the user reopens
    # their disk and finds blank directories where they expected
    # files. Best-effort: ``fsync_dir`` silently no-ops on filesystems
    # that refuse directory fsync (FAT, some FUSE).
    fsynced_dirs: set[Path] = set()
    for plan in plans:
        parent = (final_root / plan.relative_path).parent
        if parent not in fsynced_dirs:
            fsync_dir(parent)
            fsynced_dirs.add(parent)
    if final_root not in fsynced_dirs:
        fsync_dir(final_root)

    _report(progress, "done", total_chunks, total_chunks, bytes_written)
    return final_root
