"""Typed client for the QR-grant join-request endpoints (§5.C2).

Companion to :mod:`vault.grant.client` (device-grant management) and
:mod:`vault.grant.qr` (URL + verification-code primitives). Wraps the
five join-request endpoints under
``/api/vaults/{id}/join-requests`` with dataclasses + typed errors so
the admin "Grant device" dialog and the claimant subprocess
(``windows_vault_join.py``) don't parse raw JSON.

Backed by :class:`desktop.src.vault.binding.runtime.VaultHttpRelay`'s
``create_join_request`` / ``get_join_request`` / ``claim_join_request``
/ ``approve_join_request`` / ``reject_join_request`` methods.

State machine the relay enforces (server controller comments + spec
§8 row table):

    pending ──claim──> claimed ──approve──> approved
       │                  │                    │
       │                  └─delete─> rejected ─┘   (admin DELETE)
       │                  └─ttl────> expired
       └─ttl────> expired

The wrappers map server status codes to typed errors so callers can
branch on outcome without inspecting HTTP details directly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from ..relay_errors import VaultRelayError


# ----- typed payload shapes -----------------------------------------


@dataclass(frozen=True)
class JoinRequest:
    """One join-request row from the relay.

    Mirrors ``VaultGrantsController::joinRequestPayload``. The
    ``wrapped_vault_grant`` is populated only on the claimant's poll
    after ``state="approved"``; admin polls never see it.
    """

    join_request_id: str
    vault_id_dashed: str
    state: str                          # pending|claimed|approved|rejected|expired
    ephemeral_admin_pubkey: bytes       # 32 bytes
    expires_at: str
    created_at: str
    claimed_at: str | None
    approved_at: str | None
    rejected_at: str | None
    claimant_device_id: str | None
    claimant_pubkey: bytes | None       # 32 bytes when claimed; None before
    device_name: str | None
    approved_role: str | None
    granted_by_device_id: str | None
    wrapped_vault_grant: bytes | None   # decoded AEAD envelope when approved


# ----- typed errors --------------------------------------------------


class JoinRequestError(RuntimeError):
    """Base for join-request typed errors."""


class JoinRequestAuthError(JoinRequestError):
    """401/403 on a join-request call.

    Surfaces as "Admin role required" in the admin dialog or as a
    re-pair suggestion if the device's own auth has expired.
    """


class JoinRequestNotFoundError(JoinRequestError):
    """Server returned 404 ``vault_join_request_state``.

    Either the id doesn't exist or the join-request was deleted while
    we were polling. Treated as terminal in the claimant flow.
    """


class JoinRequestStateError(JoinRequestError):
    """Server returned 409 ``vault_join_request_state``.

    Two callers raced for the same join-request and lost (claim on
    already-claimed, approve on already-approved with diverging
    material). Caller can re-fetch the row to see the current state.
    """


class JoinRequestRateLimitedError(JoinRequestError):
    """Server returned 429 ``vault_rate_limited``.

    F-S08: max 5 pending join-requests per vault. Admin must wait for
    the TTL window to drain (15 min) or reject existing rows.
    """


# ----- typed wrappers -----------------------------------------------


def create_join_request(
    relay, vault_id: str, vault_access_secret: str,
    *, ephemeral_admin_pubkey: bytes,
) -> JoinRequest:
    """Admin-side: ask the relay to mint a fresh join-request.

    Returns the row with ``state="pending"`` and the admin pubkey
    echoed back. The admin renders the QR + verification-code waiting
    screen from this response.
    """
    try:
        data = relay.create_join_request(
            vault_id, vault_access_secret,
            ephemeral_admin_pubkey=ephemeral_admin_pubkey,
        )
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to create join-request")
        raise  # pragma: no cover

    return _parse_join_request(data)


def get_join_request(
    relay, vault_id: str, req_id: str,
    *, vault_access_secret: str | None = None,
) -> JoinRequest:
    """Poll for state transitions.

    Admin path passes ``vault_access_secret``; claimant path omits it
    (device auth alone is enough — the server pins the claimant by
    the device id already recorded on the row).
    """
    try:
        data = relay.get_join_request(
            vault_id, req_id, vault_access_secret=vault_access_secret,
        )
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to poll join-request")
        raise  # pragma: no cover

    return _parse_join_request(data)


def claim_join_request(
    relay, vault_id: str, req_id: str,
    *, claimant_pubkey: bytes, device_name: str,
) -> JoinRequest:
    """Claimant-side: take ownership of the pending join-request.

    Idempotent for the same claimant (F-S13): a repeat claim with
    identical pubkey + device_name returns the existing row. Any
    drift surfaces as :class:`JoinRequestStateError` (409) so the
    operator notices a real cross-claim collision.
    """
    try:
        data = relay.claim_join_request(
            vault_id, req_id,
            claimant_pubkey=claimant_pubkey, device_name=device_name,
        )
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to claim join-request")
        raise  # pragma: no cover

    return _parse_join_request(data)


def approve_join_request(
    relay, vault_id: str, vault_access_secret: str, req_id: str,
    *, approved_role: str, wrapped_vault_grant: bytes,
) -> JoinRequest:
    """Admin-side: approve a claimed join-request.

    Server inserts the corresponding device-grant row so the new
    device's first relay call passes the role gate. Idempotent (F-S13)
    on byte-identical re-submission from the same admin.
    """
    try:
        data = relay.approve_join_request(
            vault_id, vault_access_secret, req_id,
            approved_role=approved_role,
            wrapped_vault_grant=wrapped_vault_grant,
        )
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to approve join-request")
        raise  # pragma: no cover

    return _parse_join_request(data)


def reject_join_request(
    relay, vault_id: str, vault_access_secret: str, req_id: str,
) -> None:
    """Admin-side: delete the pending / claimed join-request.

    Wraps the relay's idempotent DELETE — calling on an already-rejected
    row returns 204. Used both for explicit "Reject" clicks and for
    wizard-cancel cleanup so abandoned rows don't sit on the relay
    consuming the per-vault pending budget.
    """
    try:
        relay.reject_join_request(vault_id, vault_access_secret, req_id)
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to reject join-request")
        raise  # pragma: no cover


# ----- private helpers ---------------------------------------------


def _parse_join_request(row: dict[str, Any]) -> JoinRequest:
    ephemeral = _decode_b64(row.get("ephemeral_admin_pubkey"))
    if ephemeral is None or len(ephemeral) != 32:
        raise JoinRequestError(
            "join-request payload missing or malformed ephemeral_admin_pubkey",
        )
    claimant_pubkey = _decode_b64(row.get("claimant_pubkey"))
    if claimant_pubkey is not None and len(claimant_pubkey) != 32:
        raise JoinRequestError(
            "join-request payload has a claimant_pubkey that isn't 32 bytes",
        )
    wrapped = _decode_b64(row.get("wrapped_vault_grant"))

    return JoinRequest(
        join_request_id=str(row.get("join_request_id") or ""),
        vault_id_dashed=str(row.get("vault_id") or ""),
        state=str(row.get("state") or ""),
        ephemeral_admin_pubkey=ephemeral,
        expires_at=str(row.get("expires_at") or ""),
        created_at=str(row.get("created_at") or ""),
        claimed_at=_str_or_none(row.get("claimed_at")),
        approved_at=_str_or_none(row.get("approved_at")),
        rejected_at=_str_or_none(row.get("rejected_at")),
        claimant_device_id=_str_or_none(row.get("claimant_device_id")),
        claimant_pubkey=claimant_pubkey,
        device_name=_str_or_none(row.get("device_name")),
        approved_role=_str_or_none(row.get("approved_role")),
        granted_by_device_id=_str_or_none(row.get("granted_by_device_id")),
        wrapped_vault_grant=wrapped,
    )


def _decode_b64(value: Any) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value)
    except Exception as exc:  # noqa: BLE001
        raise JoinRequestError(f"base64 decode failed: {exc}") from exc


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _raise_typed(exc: VaultRelayError, *, default_message: str) -> None:
    """Map a :class:`VaultRelayError` into a join-request typed error.

    - HTTP 401/403 → :class:`JoinRequestAuthError`
    - HTTP 404 + code ``vault_join_request_state`` →
      :class:`JoinRequestNotFoundError`
    - HTTP 409 + code ``vault_join_request_state`` →
      :class:`JoinRequestStateError`
    - HTTP 429 → :class:`JoinRequestRateLimitedError`

    Anything else re-raises the original :class:`VaultRelayError`.
    """
    status = int(getattr(exc, "status_code", 0) or 0)
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or "")

    if status in (401, 403):
        raise JoinRequestAuthError(message or default_message) from exc
    if status == 404 and code == "vault_join_request_state":
        raise JoinRequestNotFoundError(message or default_message) from exc
    if status == 409 and code == "vault_join_request_state":
        raise JoinRequestStateError(message or default_message) from exc
    if status == 429:
        raise JoinRequestRateLimitedError(message or default_message) from exc
    raise exc


__all__ = [
    "JoinRequest",
    "JoinRequestAuthError",
    "JoinRequestError",
    "JoinRequestNotFoundError",
    "JoinRequestRateLimitedError",
    "JoinRequestStateError",
    "approve_join_request",
    "claim_join_request",
    "create_join_request",
    "get_join_request",
    "reject_join_request",
]
