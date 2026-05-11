"""User-facing translation for relay errors (F-U06).

The ``vault_v1`` envelope codes (``vault_v1.cas_conflict`` etc.) are
machine-readable contracts; surfacing them verbatim in a toast or
banner leaks implementation detail to a non-technical user. This
module owns the translation table.

Callers should pass any exception thrown by ``vault_runtime`` /
``vault_upload`` / ``vault_download`` through :func:`humanize` before
displaying it.
"""

from __future__ import annotations

from typing import Any


_HUMAN_BY_CODE: dict[str, str] = {
    "vault_v1.cas_conflict":                 "Another device just changed the vault. Try again.",
    "vault_manifest_conflict":               "Another device just changed the vault. Try again.",
    "vault_v1.quota_exceeded":               "Vault is full. Free up space (run an eviction pass) and retry.",
    "vault_quota_exceeded":                  "Vault is full. Free up space (run an eviction pass) and retry.",
    "vault_v1.chunk_missing":                "A piece of the file is missing on the relay. Re-uploading should fix it.",
    "vault_chunk_missing":                   "A piece of the file is missing on the relay. Re-uploading should fix it.",
    "vault_v1.format_version_unsupported":   "This vault uses a newer format. Update Desktop Connector to continue.",
    "vault_format_version_unsupported":      "This vault uses a newer format. Update Desktop Connector to continue.",
    "vault_v1.access_denied":                "Your access to this vault was revoked.",
    "vault_access_denied":                   "Your access to this vault was revoked.",
    "vault_v1.auth_failed":                  "The vault password is wrong. Re-enter it to continue.",
    "vault_auth_failed":                     "The vault password is wrong. Re-enter it to continue.",
    "vault_v1.purge_not_allowed":            "Hard-purge requires an admin device.",
    "vault_purge_not_allowed":               "Hard-purge requires an admin device.",
    "vault_v1.payload_too_large":            "That payload is bigger than the vault accepts.",
    "vault_payload_too_large":               "That payload is bigger than the vault accepts.",
    "vault_v1.rate_limited":                 "Too many vault requests. Wait a moment and try again.",
    "vault_rate_limited":                    "Too many vault requests. Wait a moment and try again.",
    "vault_v1.storage_unavailable":          "The relay's storage is temporarily unavailable. Try again shortly.",
    "vault_storage_unavailable":             "The relay's storage is temporarily unavailable. Try again shortly.",
    "vault_v1.migration_in_progress":        "This vault is in the middle of a relay migration. Try again after it completes.",
    "vault_migration_in_progress":           "This vault is in the middle of a relay migration. Try again after it completes.",
    "vault_v1.export_passphrase_invalid":    "Wrong passphrase, or this bundle is for a different vault.",
    "vault_export_passphrase_invalid":       "Wrong passphrase, or this bundle is for a different vault.",
    "vault_v1.export_tampered":              "This export bundle has been modified and can't be trusted.",
    "vault_export_tampered":                 "This export bundle has been modified and can't be trusted.",
    "vault_v1.header_tampered":              "The vault header looks corrupt. Run a full integrity check.",
    "vault_header_tampered":                 "The vault header looks corrupt. Run a full integrity check.",
    "vault_v1.manifest_tampered":            "The vault manifest looks corrupt. Run a full integrity check.",
    "vault_manifest_tampered":                "The vault manifest looks corrupt. Run a full integrity check.",
}


def humanize(exc: BaseException | str | None) -> str:
    """Translate a vault relay error into user-facing text.

    Accepts an exception, a ``"code: message"`` string, or ``None``.
    Falls back to the original message when the code isn't recognised
    so a future code surfaces sensibly even before this table is
    updated.
    """
    if exc is None:
        return ""
    code = ""
    message = ""
    if isinstance(exc, BaseException):
        code = str(getattr(exc, "code", "") or "")
        message = str(getattr(exc, "message", "") or "")
        if not code and not message:
            return _scrub(str(exc))
    elif isinstance(exc, str):
        # Try to peel off "code: message" shape.
        text = exc.strip()
        if ":" in text:
            head, _, tail = text.partition(":")
            head = head.strip()
            if head.startswith("vault_") or head.startswith("vault_v1."):
                code = head
                message = tail.strip()
            else:
                message = text
        else:
            message = text
    else:
        return ""

    human = _HUMAN_BY_CODE.get(code)
    if human:
        return human
    if message:
        return _scrub(message)
    if code:
        # Unknown code — never expose it raw.
        return "The vault server reported an error. Try again later."
    return ""


def _scrub(text: str) -> str:
    """Strip vault_v1. prefix from accidentally-leaked codes."""
    cleaned = text.replace("vault_v1.", "").strip()
    return cleaned[:200] if cleaned else ""


__all__ = ["humanize"]
