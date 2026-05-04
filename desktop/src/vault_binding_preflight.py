"""Connect-local-folder preflight summary (T10.2 / §D15).

Pure data: counts what the user is about to bind without touching the
relay. The connect-folder dialog shows the result; tombstones get
their own informational line per §D15 ("Deleted files will not be
applied to your local folder during initial binding").
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class PreflightSummary:
    remote_folder_display_name: str
    current_files: int
    current_bytes: int
    deleted_files: int
    deleted_bytes: int
    earliest_recoverable_until: str        # RFC3339 of the soonest tombstone expiry, "" when none
    local_existing_files: int              # files already present locally (not in remote) → "extras"
    local_existing_bytes: int
    local_path_exists: bool
    local_path_writable: bool


def compute_preflight(
    *,
    manifest: dict[str, Any],
    remote_folder_id: str,
    local_root: Path | str,
    ignore_local_dotfiles: bool = True,
) -> PreflightSummary:
    """Build the §D15 preflight summary for a connect-folder confirmation.

    Counts driven from the (already-decrypted) manifest plaintext, so
    the function is pure-data — no relay calls. Local-root walking is
    cheap and bounded; if the path doesn't exist yet (the user picked
    a new directory), the local_existing_* counts are zero.
    """
    folder = _find_folder(manifest, remote_folder_id)
    display_name = (
        str(folder.get("display_name_enc", ""))
        if folder is not None else ""
    )

    current_files = 0
    current_bytes = 0
    deleted_files = 0
    deleted_bytes = 0
    earliest_recoverable: str = ""

    if folder is not None:
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "file")) != "file":
                continue
            latest = _latest_version(entry)
            size = int((latest or {}).get("logical_size", 0) or 0)
            if bool(entry.get("deleted")):
                deleted_files += 1
                deleted_bytes += size
                horizon = str(entry.get("recoverable_until") or "")
                if horizon and (not earliest_recoverable or horizon < earliest_recoverable):
                    earliest_recoverable = horizon
            else:
                current_files += 1
                current_bytes += size

    local_root = Path(local_root)
    local_existing_files = 0
    local_existing_bytes = 0
    local_path_exists = local_root.is_dir()
    local_path_writable = False
    if local_path_exists:
        local_path_writable = os.access(local_root, os.W_OK)
        for path in _walk_local(local_root, ignore_local_dotfiles):
            try:
                stat = path.stat()
            except OSError:
                continue
            local_existing_files += 1
            local_existing_bytes += int(stat.st_size)
    elif local_root.parent.exists():
        local_path_writable = os.access(local_root.parent, os.W_OK)

    return PreflightSummary(
        remote_folder_display_name=display_name,
        current_files=current_files,
        current_bytes=current_bytes,
        deleted_files=deleted_files,
        deleted_bytes=deleted_bytes,
        earliest_recoverable_until=earliest_recoverable,
        local_existing_files=local_existing_files,
        local_existing_bytes=local_existing_bytes,
        local_path_exists=local_path_exists,
        local_path_writable=local_path_writable,
    )


def render_preflight_text(summary: PreflightSummary) -> str:
    """§D15 wording: tombstones land on their own informational line."""
    lines: list[str] = []
    name = summary.remote_folder_display_name or "(this remote folder)"
    lines.append(
        f'Remote folder "{name}":\n'
        f"  {_format_bytes(summary.current_bytes)} across "
        f"{summary.current_files:,} current files."
    )
    if summary.deleted_files > 0:
        recover_clause = ""
        if summary.earliest_recoverable_until:
            recover_clause = (
                f" (earliest recoverable until {summary.earliest_recoverable_until})"
            )
        lines.append(
            f"  {summary.deleted_files:,} deleted files{recover_clause}."
        )
        lines.append(
            "  Deleted files will not be applied to your local folder "
            "during initial binding."
        )
    if summary.local_existing_files > 0:
        lines.append(
            f"\nLocal folder already contains "
            f"{summary.local_existing_files:,} file(s) "
            f"({_format_bytes(summary.local_existing_bytes)}) — they'll "
            "stay in place; the initial baseline downloads remote files "
            "alongside them."
        )
    if summary.local_path_exists and not summary.local_path_writable:
        lines.append("\nWarning: local path is not writable.")
    elif (not summary.local_path_exists) and not summary.local_path_writable:
        lines.append(
            "\nWarning: parent directory is not writable; the binding "
            "would fail when materializing the baseline."
        )
    return "\n".join(lines)


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


def _walk_local(root: Path, ignore_dotfiles: bool) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        if ignore_dotfiles:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if ignore_dotfiles and name.startswith("."):
                continue
            yield Path(dirpath) / name


def _format_bytes(value: int) -> str:
    size = max(0, int(value))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.1f} {unit}"


__all__ = [
    "PreflightSummary",
    "compute_preflight",
    "render_preflight_text",
]
