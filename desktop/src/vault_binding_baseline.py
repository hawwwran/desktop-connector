"""Initial baseline for a freshly-connected local binding (T10.3).

Pipeline:

    needs-preflight  →  download current remote files to local_path
                    →   seed vault_local_entries (downloaded + extras)
                    →   set binding state = bound, last_synced_revision

Pre-existing local files that aren't in the remote folder are
preserved verbatim (no deletions) and recorded as "extra" entries in
``vault_local_entries`` — `content_fingerprint = ""` flags them as
unsynced from the local-loop's perspective; the watcher (T10.4) is
free to upload them later if the user wants.

Reuses :mod:`vault_download` for the per-file decrypt path so the
chunk-fetch + AEAD logic stays in one place.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .vault_bindings import VaultBindingsStore, VaultBinding, VaultLocalEntry
from .vault_download import (
    DownloadProgress,
    default_vault_download_cache_dir,
    download_latest_file,
)


log = logging.getLogger(__name__)


@dataclass
class BaselineProgress:
    phase: str
    files_total: int = 0
    files_done: int = 0
    bytes_done: int = 0
    current_path: str = ""


@dataclass
class BaselineResult:
    downloaded_files: list[str] = field(default_factory=list)
    extra_files: list[str] = field(default_factory=list)
    bytes_downloaded: int = 0
    last_synced_revision: int = 0
    binding: VaultBinding | None = None


class BaselineVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...


class BaselineRelay(Protocol):
    def batch_head_chunks(
        self, vault_id: str, vault_access_secret: str, chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]: ...

    def get_chunk(
        self, vault_id: str, vault_access_secret: str, chunk_id: str,
    ) -> bytes: ...


def run_initial_baseline(
    *,
    vault: BaselineVault,
    relay: BaselineRelay,
    manifest: dict[str, Any],
    store: VaultBindingsStore,
    binding: VaultBinding,
    chunk_cache_dir: Path | None = None,
    progress: Callable[[BaselineProgress], None] | None = None,
) -> BaselineResult:
    """Materialize the binding's remote folder into ``binding.local_path``.

    Tombstones are skipped per §D15 ("Deleted files will not be applied
    to your local folder during initial binding"). Pre-existing local
    files are preserved and registered as ``extra`` rows.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    folder = _find_folder(manifest, binding.remote_folder_id)
    if folder is None:
        raise KeyError(
            f"remote folder not found in manifest: {binding.remote_folder_id}"
        )

    folder_display_name = str(folder.get("display_name_enc", ""))
    if not folder_display_name:
        raise ValueError("remote folder has no display name; cannot download")

    local_root = Path(binding.local_path)
    local_root.mkdir(parents=True, exist_ok=True)

    # Plan the download list: every non-deleted file entry's latest version.
    plan = _plan_baseline(folder)
    _emit(progress, "planning", len(plan), 0, 0, "")

    cache_dir = chunk_cache_dir or default_vault_download_cache_dir()
    manifest_revision = int(manifest.get("revision", 0))

    bytes_done = 0
    downloaded: list[str] = []
    for relative_path, entry in plan:
        target = local_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        display_path = f"{folder_display_name}/{relative_path}"
        _emit(progress, "downloading", len(plan), len(downloaded),
              bytes_done, relative_path)
        try:
            written_path = download_latest_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path=display_path,
                destination=target,
                existing_policy="overwrite",
                chunk_cache_dir=cache_dir,
            )
        except Exception:
            # Re-raise so the caller can flip the state file / surface
            # an error; partial progress is bounded to whatever already
            # landed atomically on disk.
            raise

        try:
            stat = written_path.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
        except OSError:
            size, mtime_ns = 0, 0

        latest = _latest_version(entry)
        fingerprint = str((latest or {}).get("content_fingerprint") or "")
        store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path=relative_path,
            content_fingerprint=fingerprint,
            size_bytes=size,
            mtime_ns=mtime_ns,
            last_synced_revision=manifest_revision,
        ))
        downloaded.append(relative_path)
        bytes_done += size

    # Pre-existing local files that aren't in the remote folder: log
    # as ``extra`` rows (content_fingerprint = "" so the watcher knows
    # they aren't backed by remote yet).
    extras: list[str] = []
    downloaded_set = set(downloaded)
    for absolute in _walk_local(local_root):
        relative = absolute.relative_to(local_root).as_posix()
        if relative in downloaded_set:
            continue
        try:
            stat = absolute.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
        except OSError:
            continue
        store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path=relative,
            content_fingerprint="",
            size_bytes=size,
            mtime_ns=mtime_ns,
            last_synced_revision=0,
        ))
        extras.append(relative)

    # Flip the binding to ``bound`` and stamp last_synced_revision.
    store.update_binding_state(
        binding.binding_id,
        state="bound",
        last_synced_revision=manifest_revision,
    )
    rebound = store.get_binding(binding.binding_id) or binding

    _emit(progress, "done", len(plan), len(downloaded), bytes_done, "")

    return BaselineResult(
        downloaded_files=downloaded,
        extra_files=extras,
        bytes_downloaded=bytes_done,
        last_synced_revision=manifest_revision,
        binding=rebound,
    )


def _plan_baseline(folder: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Walk the folder's entries and return ``(relative_path, entry)`` pairs.

    Skips tombstones (§D15) and entries without a downloadable version.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for entry in folder.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if bool(entry.get("deleted")):
            continue
        relative = str(entry.get("path") or "").strip()
        if not relative:
            continue
        # Defensive: never write outside the binding root.
        if relative.startswith("/") or ".." in relative.replace("\\", "/").split("/"):
            log.warning("vault.baseline.skip_unsafe path=%s", relative)
            continue
        latest = _latest_version(entry)
        if latest is None:
            continue
        out.append((relative.replace("\\", "/"), entry))
    return out


def _find_folder(manifest: dict[str, Any], remote_folder_id: str) -> dict[str, Any] | None:
    for folder in manifest.get("remote_folders", []) or []:
        if isinstance(folder, dict) and folder.get("remote_folder_id") == remote_folder_id:
            return folder
    return None


def _latest_version(entry: dict[str, Any]) -> dict[str, Any] | None:
    versions = [v for v in entry.get("versions", []) or [] if isinstance(v, dict)]
    latest_id = str(entry.get("latest_version_id") or "")
    if latest_id:
        for v in versions:
            if str(v.get("version_id", "")) == latest_id:
                return v
    return versions[-1] if versions else None


def _walk_local(root: Path) -> Iterable[Path]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield Path(dirpath) / name


def _emit(
    callback: Callable[[BaselineProgress], None] | None,
    phase: str,
    files_total: int,
    files_done: int,
    bytes_done: int,
    current_path: str,
) -> None:
    if callback is None:
        return
    callback(BaselineProgress(
        phase=phase,
        files_total=files_total,
        files_done=files_done,
        bytes_done=bytes_done,
        current_path=current_path,
    ))


__all__ = [
    "BaselineProgress",
    "BaselineResult",
    "run_initial_baseline",
]
