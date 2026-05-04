"""One-shot restore of a remote folder into a chosen local path (T11.2).

Different from a binding (T10): a restore is a single materialization
of the manifest's *current* state of one remote folder into a
user-picked destination directory, with no ongoing sync afterwards.
Per §gaps §12 the user can pick any local path (existing or new)
and the restore must:

- preflight free space against the total logical size of the folder's
  current files;
- write every non-tombstoned file via the §gaps §11 atomic-write
  pattern (so a crash leaves either the old file or the new file
  at every destination, never a partial);
- for each file that already exists locally with different content,
  preserve the local copy by writing the restored bytes to a §A20
  conflict-copy path (kind="restored");
- skip files where the destination already has byte-identical
  content — restore is idempotent on equal-bytes paths.

The actual chunk-fetch + AEAD-decrypt work is delegated to
:func:`vault_download.download_latest_file`. This module owns the
walk, the preflight, the conflict-copy decisions, and the result
summary.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from datetime import datetime, timezone

from .vault_conflict_naming import make_conflict_path
from .vault_download import (
    DownloadProgress,
    VaultLocalDiskFullError,
    default_vault_download_cache_dir,
    download_latest_file,
    download_version,
)


log = logging.getLogger(__name__)


@dataclass
class RestoreProgress:
    phase: str
    files_total: int = 0
    files_done: int = 0
    bytes_done: int = 0
    current_path: str = ""


@dataclass
class RestoreResult:
    written: list[str] = field(default_factory=list)
    skipped_identical: list[str] = field(default_factory=list)
    conflict_copies: list[tuple[str, str]] = field(default_factory=list)
    bytes_written: int = 0

    @property
    def total_files(self) -> int:
        return (
            len(self.written) + len(self.skipped_identical)
            + len(self.conflict_copies)
        )


class RestoreVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...


class RestoreRelay(Protocol):
    def batch_head_chunks(
        self, vault_id: str, vault_access_secret: str, chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]: ...

    def get_chunk(
        self, vault_id: str, vault_access_secret: str, chunk_id: str,
    ) -> bytes: ...


def restore_remote_folder(
    *,
    vault: RestoreVault,
    relay: RestoreRelay,
    manifest: dict[str, Any],
    remote_folder_id: str,
    destination: Path,
    device_name: str,
    chunk_cache_dir: Path | None = None,
    progress: Callable[[RestoreProgress], None] | None = None,
    when: Any = None,
) -> RestoreResult:
    """Materialize ``remote_folder_id``'s current state into ``destination``.

    Tombstoned entries and entries without a downloadable version are
    skipped. Returns a :class:`RestoreResult` summarising what landed.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    folder = _find_folder(manifest, remote_folder_id)
    if folder is None:
        raise KeyError(f"remote folder not found: {remote_folder_id}")

    folder_display_name = str(folder.get("display_name_enc", "")) or remote_folder_id
    plan = _plan_restore(folder)
    _emit(progress, "planning", len(plan), 0, 0, "")

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    _preflight_disk_for_plan(destination, plan)

    cache_dir = chunk_cache_dir or default_vault_download_cache_dir()
    result = RestoreResult()

    for relative_path, entry, version in plan:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        display_path = f"{folder_display_name}/{relative_path}"
        _emit(progress, "downloading", len(plan),
              len(result.written) + len(result.skipped_identical),
              result.bytes_written, relative_path)

        # If the destination already has byte-identical content, skip.
        # Otherwise: a colliding live file gets renamed via §A20
        # ("the local copy is preserved by being moved aside") and the
        # restored bytes land at the original path.
        existing_fingerprint = _file_sha256(target) if target.is_file() else None
        version_size = _int(version.get("logical_size"))

        if existing_fingerprint is not None:
            # We can shortcut on size first to avoid hashing huge files
            # whose sizes differ.
            try:
                same_size = target.stat().st_size == version_size
            except OSError:
                same_size = False
            if same_size and _bytes_match_remote(
                vault=vault, relay=relay, version=version,
                local_fingerprint=existing_fingerprint,
            ):
                result.skipped_identical.append(relative_path)
                continue

            # Different bytes → preserve the local copy by writing
            # restored bytes to a sibling A20 path.
            conflict_path = _unique_conflict_path(
                destination=destination,
                relative_path=relative_path,
                device_name=device_name,
                when=when,
            )
            conflict_target = destination / conflict_path
            conflict_target.parent.mkdir(parents=True, exist_ok=True)
            _download_one(
                vault=vault, relay=relay, manifest=manifest,
                display_path=display_path, target=conflict_target,
                cache_dir=cache_dir,
            )
            result.conflict_copies.append((relative_path, conflict_path))
        else:
            _download_one(
                vault=vault, relay=relay, manifest=manifest,
                display_path=display_path, target=target,
                cache_dir=cache_dir,
            )
            result.written.append(relative_path)

        try:
            result.bytes_written += target.stat().st_size
        except OSError:
            result.bytes_written += version_size

    _emit(progress, "done", len(plan),
          len(result.written) + len(result.skipped_identical)
          + len(result.conflict_copies),
          result.bytes_written, "")
    return result


def restore_remote_folder_at_date(
    *,
    vault: RestoreVault,
    relay: RestoreRelay,
    manifest: dict[str, Any],
    remote_folder_id: str,
    destination: Path,
    device_name: str,
    cutoff: datetime,
    chunk_cache_dir: Path | None = None,
    progress: Callable[[RestoreProgress], None] | None = None,
    when: Any = None,
) -> RestoreResult:
    """Materialize the snapshot of ``remote_folder_id`` as of ``cutoff``.

    For each file entry in the folder we pick the latest version whose
    ``created_at`` is ≤ ``cutoff`` and download that version. Entries
    that didn't exist yet at the cutoff (their earliest version
    post-dates it) are skipped. Entries that were already tombstoned
    at or before the cutoff are skipped. Entries tombstoned *after*
    the cutoff are restored from the snapshot version active at that
    time.

    The relay is read-only here — no manifest publish, no chunk
    deletion — so the current state of the vault is unchanged.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    folder = _find_folder(manifest, remote_folder_id)
    if folder is None:
        raise KeyError(f"remote folder not found: {remote_folder_id}")

    folder_display_name = str(folder.get("display_name_enc", "")) or remote_folder_id
    plan = _plan_restore_at_date(folder, cutoff)
    _emit(progress, "planning", len(plan), 0, 0, "")

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    _preflight_disk_for_plan(destination, plan)

    cache_dir = chunk_cache_dir or default_vault_download_cache_dir()
    result = RestoreResult()

    for relative_path, _entry, version in plan:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        display_path = f"{folder_display_name}/{relative_path}"
        _emit(progress, "downloading", len(plan),
              len(result.written) + len(result.skipped_identical),
              result.bytes_written, relative_path)

        existing_fingerprint = _file_sha256(target) if target.is_file() else None
        version_size = _int(version.get("logical_size"))

        if existing_fingerprint is not None:
            try:
                same_size = target.stat().st_size == version_size
            except OSError:
                same_size = False
            if same_size and _bytes_match_remote(
                vault=vault, relay=relay, version=version,
                local_fingerprint=existing_fingerprint,
            ):
                result.skipped_identical.append(relative_path)
                continue

            conflict_path = _unique_conflict_path(
                destination=destination,
                relative_path=relative_path,
                device_name=device_name,
                when=when,
            )
            conflict_target = destination / conflict_path
            conflict_target.parent.mkdir(parents=True, exist_ok=True)
            download_version(
                vault=vault, relay=relay, manifest=manifest,
                path=display_path, version_id=str(version.get("version_id", "")),
                destination=conflict_target,
                existing_policy="overwrite",
                chunk_cache_dir=cache_dir,
            )
            result.conflict_copies.append((relative_path, conflict_path))
        else:
            download_version(
                vault=vault, relay=relay, manifest=manifest,
                path=display_path, version_id=str(version.get("version_id", "")),
                destination=target,
                existing_policy="overwrite",
                chunk_cache_dir=cache_dir,
            )
            result.written.append(relative_path)

        try:
            result.bytes_written += target.stat().st_size
        except OSError:
            result.bytes_written += version_size

    _emit(progress, "done", len(plan),
          len(result.written) + len(result.skipped_identical)
          + len(result.conflict_copies),
          result.bytes_written, "")
    return result


def _plan_restore_at_date(
    folder: dict[str, Any], cutoff: datetime,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Return ``(relative_path, entry, version_at_or_before_cutoff)`` triples.

    Skips: entries that didn't exist yet at the cutoff, entries that
    were tombstoned at-or-before the cutoff, entries with no
    parseable timestamp.
    """
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for entry in folder.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        relative = str(entry.get("path") or "").strip()
        if not relative:
            continue
        if relative.startswith("/") or ".." in relative.replace("\\", "/").split("/"):
            log.warning("vault.restore.skip_unsafe path=%s", relative)
            continue

        # Skip entries that were tombstoned at or before the cutoff —
        # at that snapshot point the file no longer existed.
        if bool(entry.get("deleted")):
            deleted_when = _parse_rfc3339(entry.get("deleted_at"))
            if deleted_when is not None and deleted_when <= cutoff:
                continue

        version = _latest_version_at_or_before(entry, cutoff)
        if version is None:
            continue
        out.append((relative.replace("\\", "/"), entry, version))
    return out


def _latest_version_at_or_before(
    entry: dict[str, Any], cutoff: datetime,
) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for version in entry.get("versions", []) or []:
        if not isinstance(version, dict):
            continue
        when = _parse_rfc3339(version.get("created_at"))
        if when is None:
            continue
        if when <= cutoff:
            candidates.append((when, version))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def _parse_rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _plan_restore(folder: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
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
        if relative.startswith("/") or ".." in relative.replace("\\", "/").split("/"):
            log.warning("vault.restore.skip_unsafe path=%s", relative)
            continue
        version = _latest_version(entry)
        if version is None:
            continue
        out.append((relative.replace("\\", "/"), entry, version))
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


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _bytes_match_remote(
    *,
    vault: RestoreVault,
    relay: RestoreRelay,
    version: dict[str, Any],
    local_fingerprint: str,
) -> bool:
    """Quick equal-bytes shortcut.

    The manifest's per-version ``content_fingerprint`` is keyed
    (HMAC of plaintext SHA-256), so it is *not* the raw plaintext
    sha256 — we cannot compare directly. Instead we re-derive the
    keyed fingerprint from the local file's plaintext sha256 and
    compare. Returns False on any error (treats unknown as "different"
    so we err on the side of writing a conflict copy).
    """
    try:
        from .vault_crypto import (
            derive_content_fingerprint_key,
            make_content_fingerprint,
        )
    except ImportError:
        return False
    try:
        if not vault.master_key:
            return False
        key = derive_content_fingerprint_key(vault.master_key)
        local_keyed = make_content_fingerprint(
            key, bytes.fromhex(local_fingerprint),
        )
    except Exception:  # noqa: BLE001
        return False
    remote_keyed = str(version.get("content_fingerprint", ""))
    return bool(remote_keyed) and local_keyed == remote_keyed


def _unique_conflict_path(
    *,
    destination: Path,
    relative_path: str,
    device_name: str,
    when: Any,
) -> str:
    """Pick an §A20 conflict path that doesn't already exist on disk.

    The naming function is deterministic for `(path, device, when)`,
    so when the user re-runs a restore at the same minute we may
    collide with a prior conflict copy — recurse the suffix in that
    case (matches §A20's "Recursion" example).
    """
    candidate = make_conflict_path(
        original_path=relative_path, kind="restored",
        device_name=device_name, when=when,
    )
    while (destination / candidate).exists():
        candidate = make_conflict_path(
            original_path=candidate, kind="restored",
            device_name=device_name, when=when,
        )
    return candidate


def _preflight_disk_for_plan(
    destination: Path,
    plan: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    total = sum(_int(version.get("logical_size")) for _, _, version in plan)
    required = int(total * 1.25)
    if required <= 0:
        return
    free = shutil.disk_usage(destination).free
    if free < required:
        raise VaultLocalDiskFullError(
            f"not enough free space for restore: required {required} bytes, "
            f"available {free} bytes at {destination}"
        )


def _download_one(
    *,
    vault: RestoreVault,
    relay: RestoreRelay,
    manifest: dict[str, Any],
    display_path: str,
    target: Path,
    cache_dir: Path,
) -> None:
    download_latest_file(
        vault=vault, relay=relay, manifest=manifest,
        path=display_path, destination=target,
        existing_policy="overwrite",
        chunk_cache_dir=cache_dir,
    )


def _emit(
    callback: Callable[[RestoreProgress], None] | None,
    phase: str,
    files_total: int,
    files_done: int,
    bytes_done: int,
    current_path: str,
) -> None:
    if callback is None:
        return
    callback(RestoreProgress(
        phase=phase, files_total=files_total, files_done=files_done,
        bytes_done=bytes_done, current_path=current_path,
    ))


__all__ = [
    "RestoreProgress",
    "RestoreResult",
    "restore_remote_folder",
    "restore_remote_folder_at_date",
]
