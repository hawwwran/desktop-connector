"""Upload-flow exception classes + UI-facing 507 quota humanizer."""

from typing import Any

from ..relay_errors import VaultQuotaExceededError


class UploadConflictError(RuntimeError):
    """Raised when the chosen ``mode`` doesn't match the existing path state."""


class UploadSpecialFileSkipped(RuntimeError):
    """Raised when ``upload_file`` rejects a non-regular file (symlink/FIFO/etc).

    F-Y17 — a binding that contains a symlink would otherwise follow it
    and upload the symlink target as if it were the user's file. The
    sync engine catches this and treats the op as ``skipped``.
    """


class UploadFileTooLargeError(RuntimeError):
    """Raised when a single file exceeds ``MAX_FILE_BYTES_DEFAULT`` (F-D01)."""


def describe_quota_exceeded(error: VaultQuotaExceededError) -> dict[str, Any]:
    """Format a 507 ``vault_quota_exceeded`` for UI surfacing (T6.6).

    Returns ``{eviction_available: bool, used_bytes, quota_bytes, percent,
    heading, body, primary_action_label}``. The heading + body strings
    come straight from §D2:

    - Eviction-available variant offers to free space (the actual eviction
      pass lands in T7 — for T6.6 the button just sets up the prompt).
    - No-history variant is the §D2 step-4 terminal banner: sync stopped,
      no automatic recovery, user must export or migrate.
    """
    used = max(0, int(error.used_bytes or 0))
    quota = max(0, int(error.quota_bytes or 0))
    percent = (used * 100) // quota if quota else 100
    if error.eviction_available:
        heading = "Vault is full — make space?"
        body = (
            f"This vault is at {percent}% of its quota ({used} / {quota} bytes). "
            "Old historical versions can be purged to make room for the new upload. "
            "Eviction lands in T7; for now the upload pauses."
        )
        primary_action_label = "Make space"
    else:
        heading = "Vault is full and no backup history remains."
        body = (
            "Sync is stopped. Free space by deleting files, or export and "
            "migrate to a relay with more capacity."
        )
        primary_action_label = "Open vault settings"
    return {
        "eviction_available": bool(error.eviction_available),
        "used_bytes": used,
        "quota_bytes": quota,
        "percent": percent,
        "heading": heading,
        "body": body,
        "primary_action_label": primary_action_label,
    }
