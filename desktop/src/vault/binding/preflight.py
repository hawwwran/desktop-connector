"""Connect-local-folder preflight summary (T10.2 / §D15).

Pure data: counts what the user is about to bind without touching the
relay. The connect-folder dialog shows the result; tombstones get
their own informational line per §D15 ("Deleted files will not be
applied to your local folder during initial binding").

A Phase 1 duration estimator is folded in alongside the §D15 counts.
Suite 0004 measured the bind cliff (rate decay 8.5 → 1.3 ops/s as the
encrypted manifest grew during a 10 000-file backup-only drain). The
estimator uses a linear-in-manifest-size fit of that data so the
Connect dialog can warn before the user kicks off a multi-hour
initial sync. Plan: ``docs/plans/vault-large-folder-perf.md``.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..ui.bytes_format import format_bytes_binary as _format_bytes


# Phase 1 estimator (re-fit 2026-05-16 after Phase 2 SO-2 + SO-3
# shipped). per_op_seconds is modelled as a linear function of
# ``manifest_entries_at_publish_time``:
#
#     per_op_seconds ≈ PER_OP_FLOOR_S + PER_OP_GROWTH_S_PER_ENTRY × N
#
# Empirical anchors from the 2026-05-16 live re-test against the
# post-SO-2+SO-3 cycle (clean dev twin, ``php -S``):
#
#   1k bind drain: 15.6 s for 1 000 ops from empty → 0.0156 s/op avg
#                  → per_op_avg = a + b × 500
#   10k bind drain: 1 216 s for 10 000 ops from empty → 0.122 s/op avg
#                   → per_op_avg = a + b × 5 000
#
# Solving the system: a ≈ 0.004, b ≈ 2.4e-5. Rounding to slightly
# conservative round numbers so the warning over-predicts rather than
# under-predicts on a faster relay (Apache mod_php). The 1k case
# stays below the warning threshold; trigger fires at ~3 000 files
# from an empty vault, which matches the original Phase 1 intent
# ("only pop the dialog for truly painful durations").
#
# Pre-SO-2+SO-3 anchors (kept for historical reference, see
# ``temp/automation-tests-results/0004/B7-large-folder/result.md``):
#   N≈1 200  → 0.12 s/op  (baseline rate 8.5 ops/s)
#   N≈11 000 → 0.77 s/op  (baseline rate 1.3 ops/s)
PER_OP_FLOOR_S = 0.005
PER_OP_GROWTH_S_PER_ENTRY = 0.000025
WARNING_THRESHOLD_S = 120.0   # 2 min — below this we don't pop the dialog.


def count_manifest_entries(manifest: dict[str, Any]) -> int:
    """Count every (file, version) pair in the manifest.

    The bind drain's per-op cost grows with **total** manifest size,
    not just the entries in the folder being bound. So the estimator
    sums versions across all remote folders.
    """
    total = 0
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            versions = entry.get("versions", []) or []
            total += sum(1 for v in versions if isinstance(v, dict))
    return total


def estimate_drain_seconds(
    *,
    start_manifest_entries: int,
    new_uploads: int,
) -> float:
    """Project wall-clock for an upload-only sync drain.

    Integral of ``PER_OP_FLOOR_S + PER_OP_GROWTH_S_PER_ENTRY × k`` over
    ``k = start..start+new_uploads``. Returns ``0.0`` when there's
    nothing to upload (e.g. download-only mode or empty folder).
    """
    if new_uploads <= 0:
        return 0.0
    if start_manifest_entries < 0:
        start_manifest_entries = 0
    floor = PER_OP_FLOOR_S * new_uploads
    growth = PER_OP_GROWTH_S_PER_ENTRY * (
        new_uploads * start_manifest_entries
        + (new_uploads * (new_uploads + 1)) / 2
    )
    return floor + growth


def format_duration(seconds: float) -> str:
    """Render an estimator output for the Connect dialog UI."""
    if seconds < 1.0:
        return "less than a second"
    if seconds < 60.0:
        return f"about {int(round(seconds))} second(s)"
    if seconds < 3600.0:
        minutes = int(round(seconds / 60.0))
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds / 3600.0
    if hours < 10.0:
        return f"about {hours:.1f} hours"
    return f"about {int(round(hours))} hours"


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
    # Phase 1 duration estimate. ``projected_upload_drain_seconds``
    # is the worst-case backup-only drain time (uploading every
    # local file). ``bind_warning_threshold_hit`` is True when that
    # estimate is ≥ ``WARNING_THRESHOLD_S`` and the caller should
    # show the slow-bind confirm dialog. ``starting_manifest_entries``
    # is exposed for tests / diagnostics.
    projected_upload_drain_seconds: float = 0.0
    bind_warning_threshold_hit: bool = False
    starting_manifest_entries: int = 0


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

    starting_manifest_entries = count_manifest_entries(manifest)
    projected_upload_drain_seconds = estimate_drain_seconds(
        start_manifest_entries=starting_manifest_entries,
        new_uploads=local_existing_files,
    )
    bind_warning_threshold_hit = (
        projected_upload_drain_seconds >= WARNING_THRESHOLD_S
    )

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
        projected_upload_drain_seconds=projected_upload_drain_seconds,
        bind_warning_threshold_hit=bind_warning_threshold_hit,
        starting_manifest_entries=starting_manifest_entries,
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
    if summary.projected_upload_drain_seconds > 0:
        lines.append(
            f"\nInitial upload (if backup-only or two-way): "
            f"{format_duration(summary.projected_upload_drain_seconds)}."
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


__all__ = [
    "PER_OP_FLOOR_S",
    "PER_OP_GROWTH_S_PER_ENTRY",
    "PreflightSummary",
    "WARNING_THRESHOLD_S",
    "compute_preflight",
    "count_manifest_entries",
    "estimate_drain_seconds",
    "format_duration",
    "render_preflight_text",
]
