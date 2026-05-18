"""Conflict detection + filename rename for "Keep both" uploads."""

from datetime import datetime
from typing import Any

from ..manifest import find_file_entry_in_shard


def make_conflict_renamed_path(
    remote_path: str,
    device_name: str,
    *,
    kind: str = "uploaded",
    now: datetime | None = None,
) -> str:
    """A20-style conflict-rename for "Keep both" uploads.

    Thin wrapper over :func:`vault.conflict_naming.make_conflict_path`.
    Kept as a stable import for the existing T6.2 callers.
    """
    from ..conflict_naming import make_conflict_path
    return make_conflict_path(
        original_path=remote_path,
        kind=kind,
        when=now,
        device_name=device_name,
    )


def detect_path_conflict(
    manifest: dict[str, Any],
    remote_folder_id: str,
    remote_path: str,
) -> bool:
    """Return True if ``remote_path`` already has a non-deleted file entry.

    Walks the per-folder shard view inside the assembled unified
    manifest (each folder dict carries its own ``entries[]`` — same
    shape ``find_file_entry_in_shard`` consumes). Deleted entries are
    treated as no-conflict so re-uploading over a tombstone implicitly
    restores the file in T6.1.
    """
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        if folder.get("remote_folder_id") != remote_folder_id:
            continue
        entry = find_file_entry_in_shard(folder, remote_path)
        if entry is None:
            return False
        return not bool(entry.get("deleted"))
    return False
