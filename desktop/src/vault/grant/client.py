"""Typed device-grant management client (§6.H2).

Wraps the vault-scoped HTTP endpoints under
``/api/vaults/{id}/device-grants`` with named dataclasses and error
types so the Devices tab in `windows_vault/tab_devices.py` doesn't
have to parse raw JSON or thread error codes through GTK closures.

Backed by :class:`desktop.src.vault.binding.runtime.VaultHttpRelay`'s
``list_device_grants`` / ``revoke_device_grant`` methods, which carry
the existing connection + auth-header conventions.

Both endpoints require ``role=admin`` on the relay; the typed
:class:`DeviceGrantsAuthError` lets the UI distinguish "wrong role"
from "auth header expired" without inspecting HTTP status codes
directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..relay_errors import VaultRelayError


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceGrant:
    """One row from ``GET /api/vaults/{id}/device-grants``.

    Mirrors the server payload in
    ``VaultGrantsController::listDeviceGrants``. ``is_caller`` is the
    server's "this row is the calling device" flag — the UI uses it to
    grey out the Revoke button on the operator's own row (server also
    rejects self-revoke with HTTP 400, but pre-disabling avoids the
    obvious foot-gun click).
    """

    grant_id: str
    device_id: str
    device_name: str | None
    role: str
    granted_by: str
    granted_via: str
    granted_at: str
    revoked_at: str | None
    revoked_by: str | None
    last_seen_at: str | None
    is_revoked: bool
    is_caller: bool


@dataclass(frozen=True)
class RevokeResult:
    """Response from ``DELETE /api/vaults/{id}/device-grants/{device_id}``.

    ``already_revoked=True`` when the row was already in the revoked
    state — the server is idempotent, so the UI treats this the same
    as a fresh revoke (refresh the list, no error).
    """

    vault_id: str
    device_id: str
    revoked_at: str
    already_revoked: bool


# ---- typed error shapes --------------------------------------------


class DeviceGrantsError(RuntimeError):
    """Base for typed device-grants HTTP errors."""


class DeviceGrantsAuthError(DeviceGrantsError):
    """401/403 on a device-grants request.

    The user's device either isn't authenticated to this vault
    (401) or lacks the admin role required for grant management
    (403). UI surfaces this as "Admin role required" or a re-pair
    suggestion.
    """


class DeviceGrantNotFoundError(DeviceGrantsError):
    """Target device has no grant on this vault (HTTP 404)."""


class CannotRevokeSelfError(DeviceGrantsError):
    """Server rejected an attempt to revoke the caller's own grant.

    Server emits HTTP 400 ``vault_invalid_request`` with the message
    "admin cannot revoke their own grant; transfer admin role first."
    The Devices tab pre-disables the Revoke button on the caller's
    row, so this error is a backstop for race conditions or
    out-of-band callers.
    """


# ---- typed wrappers around VaultHttpRelay's raw methods -------------


def list_device_grants(relay, vault_id: str, vault_access_secret: str) -> list[DeviceGrant]:
    """List every grant on the vault as typed dataclasses.

    Raises :class:`DeviceGrantsAuthError` on 401/403, propagates
    everything else as :class:`VaultRelayError` for the tab's
    generic error surface.
    """
    try:
        data = relay.list_device_grants(vault_id, vault_access_secret)
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to list device grants")
        raise  # pragma: no cover — _raise_typed always raises

    grants_raw = data.get("grants")
    if not isinstance(grants_raw, list):
        raise DeviceGrantsError(
            "Relay returned a device-grants list without a 'grants' array.",
        )
    return [_parse_device_grant(row) for row in grants_raw if isinstance(row, dict)]


def revoke_device_grant(
    relay, vault_id: str, vault_access_secret: str, target_device_id: str,
) -> RevokeResult:
    """Revoke ``target_device_id``'s grant on the vault.

    Idempotent: revoking an already-revoked grant returns
    ``RevokeResult(already_revoked=True)``. Raises
    :class:`CannotRevokeSelfError` for the self-revoke server gate,
    :class:`DeviceGrantNotFoundError` for unknown device,
    :class:`DeviceGrantsAuthError` for 401/403.
    """
    try:
        data = relay.revoke_device_grant(
            vault_id, vault_access_secret, target_device_id,
        )
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to revoke device grant")
        raise  # pragma: no cover

    result = RevokeResult(
        vault_id=str(data.get("vault_id") or ""),
        device_id=str(data.get("device_id") or ""),
        revoked_at=str(data.get("revoked_at") or ""),
        already_revoked=bool(data.get("already_revoked", False)),
    )
    # Collateral fix per docs/plans/activity-timeline.md: the revoke
    # flow had no log emission. Idempotent re-revoke logs are tagged so
    # an operator can tell "user clicked Revoke again" from a fresh
    # revocation.
    log.info(
        "vault.revoke.completed vault=%s device_id=%s revoked_at=%s already_revoked=%s",
        result.vault_id, result.device_id[:12] if result.device_id else "?",
        result.revoked_at, str(result.already_revoked).lower(),
    )
    return result


# ---- private helpers ------------------------------------------------


def _parse_device_grant(row: dict[str, Any]) -> DeviceGrant:
    return DeviceGrant(
        grant_id=str(row.get("grant_id") or ""),
        device_id=str(row.get("device_id") or ""),
        device_name=(
            str(row["device_name"]) if row.get("device_name") is not None else None
        ),
        role=str(row.get("role") or ""),
        granted_by=str(row.get("granted_by") or ""),
        granted_via=str(row.get("granted_via") or ""),
        granted_at=str(row.get("granted_at") or ""),
        revoked_at=(
            str(row["revoked_at"]) if row.get("revoked_at") is not None else None
        ),
        revoked_by=(
            str(row["revoked_by"]) if row.get("revoked_by") is not None else None
        ),
        last_seen_at=(
            str(row["last_seen_at"]) if row.get("last_seen_at") is not None else None
        ),
        is_revoked=bool(row.get("is_revoked", False)),
        is_caller=bool(row.get("is_caller", False)),
    )


def _raise_typed(exc: VaultRelayError, *, default_message: str) -> None:
    """Map a :class:`VaultRelayError` into a device-grants typed error.

    The server distinguishes:

    - HTTP 401/403 → :class:`DeviceGrantsAuthError`
    - HTTP 400 + code ``vault_invalid_request`` + message includes
      "own grant" → :class:`CannotRevokeSelfError`
    - HTTP 404 + code ``vault_join_request_state`` →
      :class:`DeviceGrantNotFoundError`

    Anything else re-raises the original :class:`VaultRelayError`
    so the caller can surface the relay's own error message.
    """
    status = int(getattr(exc, "status_code", 0) or 0)
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or "")

    if status in (401, 403):
        raise DeviceGrantsAuthError(message or default_message) from exc
    if status == 400 and code == "vault_invalid_request" and "own grant" in message.lower():
        raise CannotRevokeSelfError(message) from exc
    if status == 404 and code == "vault_join_request_state":
        raise DeviceGrantNotFoundError(message or default_message) from exc
    raise exc


__all__ = [
    "CannotRevokeSelfError",
    "DeviceGrant",
    "DeviceGrantNotFoundError",
    "DeviceGrantsAuthError",
    "DeviceGrantsError",
    "RevokeResult",
    "list_device_grants",
    "revoke_device_grant",
]
