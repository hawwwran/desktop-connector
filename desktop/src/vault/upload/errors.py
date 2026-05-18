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
    """Format a 507 ``vault_quota_exceeded`` for UI routing.

    Returns ``{alarm: bool, eviction_available: bool, used_bytes,
    quota_bytes, percent, heading, body, primary_action_label}``.

    Three outcomes, ordered by triage priority:

    - **``alarm=True``** (``used > quota``): relay reports stored
      bytes exceed the cap. Under normal operation the server denies
      overflow at init, so observing this = the relay's quota shrank
      below previously-stored bytes (or tampering). Caller suspends
      uploads and prompts for passphrase before any destructive
      cleanup. ADR ``2026-05-18 — Eviction policy``.
    - **``eviction_available=True``** and ``alarm=False``: normal
      "doesn't fit this upload" case. Caller runs the silent
      auto-purge — no dialog, status text only — to free exactly
      enough for the failing upload, then retries init.
    - **``eviction_available=False``**: no destructive material left.
      Terminal "vault full, no backup history remains" banner; user
      must export or migrate.

    Heading / body / primary_action_label are populated only for the
    paths that surface a dialog or banner (alarm + no-history). The
    silent auto-purge path uses them for diagnostics / status text
    only.
    """
    used = max(0, int(error.used_bytes or 0))
    quota = max(0, int(error.quota_bytes or 0))
    percent = (used * 100) // quota if quota else 100
    alarm = quota > 0 and used > quota
    eviction_available = bool(error.eviction_available)

    if alarm:
        heading = "Vault quota was reduced — approve cleanup"
        body = (
            f"The relay reports the vault is now over capacity "
            f"(used {used} bytes, quota {quota} bytes). This can happen if "
            "the relay quota was reduced. Type your passphrase to authorize a "
            "one-time cleanup that brings stored data back under quota."
        )
        primary_action_label = "Approve cleanup"
    elif eviction_available:
        heading = "Vault is full — making space"
        body = (
            f"This vault is at {percent}% of its quota ({used} / {quota} bytes). "
            "Reclaiming space from the oldest historical versions to fit the upload."
        )
        primary_action_label = "Continue"
    else:
        heading = "Vault is full and no backup history remains."
        body = (
            "Sync is stopped. Free space by deleting files, or export and "
            "migrate to a relay with more capacity."
        )
        primary_action_label = "Open vault settings"
    return {
        "alarm": alarm,
        "eviction_available": eviction_available,
        "used_bytes": used,
        "quota_bytes": quota,
        "percent": percent,
        "heading": heading,
        "body": body,
        "primary_action_label": primary_action_label,
    }
