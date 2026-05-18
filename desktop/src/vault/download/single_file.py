"""Single-file downloads: ``download_latest_file`` and ``download_version``.

Both walk a single entry's chunk list, validate the result against
``logical_size``, and atomically write to the chosen destination via
:func:`paths.atomic_write_file`. The shared ``_report`` callback
formatter lives here because both flows use it identically; the
folder flow imports it from this module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from ..binding.lifecycle import SyncCancelledError
from ..ui.browser_model import get_file
from ..manifest import normalize_manifest_plaintext
from .cache import _load_cached_chunk, _store_cached_chunk
from .chunks import (
    _decrypt_chunk,
    _ensure_all_chunks_present,
    _get_chunk_with_retry,
)
from .manifest import (
    _find_version,
    _folder_for_display_path,
    _int_value,
    _latest_version,
    _version_chunks,
)
from .paths import (
    _preflight_disk_space,
    atomic_write_chunks,
    atomic_write_file,
    resolve_download_destination,
)
from .types import (
    ChunkRelay,
    DownloadProgress,
    DownloadVault,
    ExistingFilePolicy,
)


log = logging.getLogger(__name__)


def download_latest_file(
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
    """Download one file's latest non-deleted version to ``destination``.

    F-U03: ``should_continue`` is checked between every chunk fetch
    and once before the final atomic write. When it returns ``False``,
    the function raises :class:`vault_binding_lifecycle.SyncCancelledError`.
    Already-fetched plaintext is discarded (the destination's
    ``.dc-temp-…`` is cleaned up by the atomic-write guard); idempotent
    chunk dedup means a future restart pays the cache-hit price, not
    a re-fetch.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    normalized = normalize_manifest_plaintext(manifest)
    folder = _folder_for_display_path(normalized, path)
    entry = get_file(normalized, path)
    if bool(entry.get("deleted")):
        raise ValueError("cannot download a deleted file")

    version = _latest_version(entry)
    if version is None:
        raise ValueError("file has no downloadable version")

    chunks = _version_chunks(version)
    final_path = resolve_download_destination(Path(destination), existing_policy)
    _preflight_disk_space(final_path, _int_value(version.get("logical_size")))

    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    _report(progress, "checking", 0, len(chunks))
    heads = _ensure_all_chunks_present(
        relay=relay,
        vault_id=vault.vault_id,
        vault_access_secret=vault.vault_access_secret,
        chunk_ids=chunk_ids,
        should_continue=should_continue,
    )

    # Review §3.H3: stream plaintext through atomic_write_chunks so
    # peak RAM is ~1 chunk (2 MiB) instead of ``2 × file size``. The
    # buffer-then-write shape was OOMing the tray subprocess on
    # multi-GB single-file restores; the folder path was already
    # streaming, so this brings the two flows in line.
    completed = 0
    bytes_written = 0

    def plaintext_iter() -> Iterable[bytes]:
        nonlocal completed, bytes_written
        for chunk in chunks:
            # F-U03: bail before each chunk fetch so a Cancel click
            # lands within ~1 chunk's worth of network+decrypt work.
            if should_continue is not None and not should_continue():
                log.info(
                    "vault.download.cancelled vault=%s path=%s chunks_done=%d total=%d",
                    vault.vault_id, path, completed, len(chunks),
                )
                raise SyncCancelledError(
                    f"download cancelled at chunk {completed}/{len(chunks)} of {path}"
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
                remote_folder_id=str(folder["remote_folder_id"]),
                file_id=str(entry.get("entry_id", "")),
                version_id=str(version.get("version_id", "")),
                chunk=chunk,
                encrypted=encrypted,
            )
            _store_cached_chunk(chunk_cache_dir, vault.vault_id, chunk["chunk_id"], encrypted)
            completed += 1
            bytes_written += len(plaintext)
            _report(progress, "downloading", completed, len(chunks), bytes_written)
            yield plaintext

        if should_continue is not None and not should_continue():
            log.info(
                "vault.download.cancelled_pre_write vault=%s path=%s",
                vault.vault_id, path,
            )
            raise SyncCancelledError(f"download cancelled before write of {path}")

    written = atomic_write_chunks(final_path, plaintext_iter())
    expected_size = _int_value(version.get("logical_size"))
    if expected_size and written != expected_size:
        raise ValueError(
            f"downloaded size mismatch: expected {expected_size}, got {written}"
        )
    _report(progress, "done", len(chunks), len(chunks), written)
    return final_path


def download_version(
    *,
    vault: DownloadVault,
    relay: ChunkRelay,
    manifest: dict[str, Any],
    path: str,
    version_id: str,
    destination: Path,
    existing_policy: ExistingFilePolicy = "fail",
    chunk_cache_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> Path:
    """Download a specific historical version to a side path.

    Per A20, downloading a previous version must never overwrite the
    file's current/latest bytes. Callers compose ``destination`` from
    :func:`previous_version_filename` so the leaf name carries a
    version timestamp and cannot collide with the latest file's name.
    The ``existing_policy`` is honoured if even that side-path already
    exists (e.g. the same version was downloaded twice).

    F-U03: ``should_continue`` is checked between every chunk fetch
    (same contract as :func:`download_latest_file`).
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")
    if not version_id:
        raise ValueError("version_id is required")

    normalized = normalize_manifest_plaintext(manifest)
    folder = _folder_for_display_path(normalized, path)
    entry = get_file(normalized, path, include_deleted=True)

    version = _find_version(entry, version_id)
    if version is None:
        raise KeyError(f"version not found: {version_id}")

    chunks = _version_chunks(version)
    final_path = resolve_download_destination(Path(destination), existing_policy)
    _preflight_disk_space(final_path, _int_value(version.get("logical_size")))

    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    _report(progress, "checking", 0, len(chunks))
    heads = _ensure_all_chunks_present(
        relay=relay,
        vault_id=vault.vault_id,
        vault_access_secret=vault.vault_access_secret,
        chunk_ids=chunk_ids,
        should_continue=should_continue,
    )

    # Review §3.H3: stream rather than buffer (same OOM concern as
    # download_latest_file). Multi-GB historical-version restores
    # otherwise peak at 2× file size.
    completed = 0
    bytes_written = 0

    def plaintext_iter() -> Iterable[bytes]:
        nonlocal completed, bytes_written
        for chunk in chunks:
            if should_continue is not None and not should_continue():
                log.info(
                    "vault.download.cancelled vault=%s path=%s version=%s chunks_done=%d total=%d",
                    vault.vault_id, path, version_id, completed, len(chunks),
                )
                raise SyncCancelledError(
                    f"version download cancelled at chunk {completed}/{len(chunks)} of {path}"
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
                remote_folder_id=str(folder["remote_folder_id"]),
                file_id=str(entry.get("entry_id", "")),
                version_id=str(version.get("version_id", "")),
                chunk=chunk,
                encrypted=encrypted,
            )
            _store_cached_chunk(chunk_cache_dir, vault.vault_id, chunk["chunk_id"], encrypted)
            completed += 1
            bytes_written += len(plaintext)
            _report(progress, "downloading", completed, len(chunks), bytes_written)
            yield plaintext

        if should_continue is not None and not should_continue():
            log.info(
                "vault.download.cancelled_pre_write vault=%s path=%s version=%s",
                vault.vault_id, path, version_id,
            )
            raise SyncCancelledError(
                f"version download cancelled before write of {path}"
            )

    written = atomic_write_chunks(final_path, plaintext_iter())
    expected_size = _int_value(version.get("logical_size"))
    if expected_size and written != expected_size:
        raise ValueError(
            f"downloaded size mismatch: expected {expected_size}, got {written}"
        )
    _report(progress, "done", len(chunks), len(chunks), written)
    return final_path


def _report(
    callback: Callable[[DownloadProgress], None] | None,
    phase: str,
    completed_chunks: int,
    total_chunks: int,
    bytes_written: int = 0,
) -> None:
    if callback is not None:
        callback(DownloadProgress(
            phase=phase,
            completed_chunks=completed_chunks,
            total_chunks=total_chunks,
            bytes_written=bytes_written,
        ))
