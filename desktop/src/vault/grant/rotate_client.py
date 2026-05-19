"""Typed client for vault access-secret rotation (§5.H3).

Wraps the relay's ``POST /api/vaults/{id}/access-secret/rotate``
endpoint (already shipped as T13.6 server-side) with a typed
dataclass + error types so the rotation wizard
(``windows_vault_rotate.py``) doesn't parse raw JSON or thread error
codes through GTK closures.

The crypto primitives — secret generation, SHA-256 digest, wire
body shape — live in :mod:`vault.grant.access_rotation`. This module
is the HTTP adapter only.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from ..relay_errors import VaultRelayError


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RotationResponse:
    """Successful rotation response.

    Mirrors the server payload in
    ``VaultGrantsController::rotateAccessSecret``. ``rotated_at`` is
    the RFC3339 timestamp the server stamped; the wizard surfaces
    this on the success screen and persists it for the Settings →
    Recovery "Last rotated" line.
    """

    vault_id_dashed: str
    rotated_at: str


class RotationError(RuntimeError):
    """Base for typed rotation errors."""


class RotationAuthError(RotationError):
    """401/403 on a rotate call.

    Either the device's auth header is stale (re-pair required) or
    the device lacks ``role=admin`` on the target vault. The wizard
    surfaces "Admin role required to rotate the access secret"
    rather than a raw HTTP code.
    """


class RotationRateLimitedError(RotationError):
    """429 — server-side rate limit on rotation.

    Sub-question (3) in the build plan: a 1-rotation-per-24h cap
    keeps an accidental double-rotation from locking everyone out.
    Wizard tells the operator to wait + try again after the
    Retry-After window.
    """


class RotationNotFoundError(RotationError):
    """404 ``vault_not_found`` — the vault row no longer exists.

    Almost certainly means the device's local cache is stale; the
    wizard suggests reopening Vault Settings to refresh.
    """


def rotate_access_secret(
    relay, vault_id: str, old_vault_access_secret: str, new_secret: str,
    *, triggered_by_revoke_grant_id: str | None = None,
) -> RotationResponse:
    """Rotate the vault's access secret on the relay.

    Computes the SHA-256 digest of ``new_secret`` locally and posts
    it to the relay; the plaintext never leaves the device. Returns
    the typed :class:`RotationResponse` carrying the server-side
    ``rotated_at`` timestamp.

    Raises:
        :class:`RotationAuthError` for 401/403
        :class:`RotationRateLimitedError` for 429
        :class:`RotationNotFoundError` for 404
        :class:`VaultRelayError` for anything else
    """
    from .access_rotation import secret_to_wire_hash

    wire = secret_to_wire_hash(new_secret)
    new_hash = base64.b64decode(wire["new_vault_access_token_hash"])

    try:
        data = relay.rotate_access_secret(
            vault_id, old_vault_access_secret,
            new_vault_access_token_hash=new_hash,
            triggered_by_revoke_grant_id=triggered_by_revoke_grant_id,
        )
    except VaultRelayError as exc:
        _raise_typed(exc, default_message="failed to rotate access secret")
        raise  # pragma: no cover

    response = RotationResponse(
        vault_id_dashed=str(data.get("vault_id") or ""),
        rotated_at=str(data.get("rotated_at") or ""),
    )
    # Collateral fix per docs/plans/activity-timeline.md: the rotation
    # wizard had no log emission today; the consumer side
    # (state/activity._EVENT_TYPE_LABELS) already labels this event
    # "Access secret rotated", so until a follow-up Phase 3.1 publishes
    # an op-log entry, this is the only audit trail.
    log.info(
        "vault.rotation.completed vault=%s rotated_at=%s%s",
        response.vault_id_dashed, response.rotated_at,
        f" triggered_by_revoke_grant_id={triggered_by_revoke_grant_id}"
        if triggered_by_revoke_grant_id else "",
    )
    return response


def _raise_typed(exc: VaultRelayError, *, default_message: str) -> None:
    """Map a :class:`VaultRelayError` into a rotation typed error.

    - HTTP 401/403 → :class:`RotationAuthError`
    - HTTP 404 + code ``vault_not_found`` → :class:`RotationNotFoundError`
    - HTTP 429 → :class:`RotationRateLimitedError`

    Anything else re-raises the original :class:`VaultRelayError`
    so the caller surfaces the relay's message verbatim.
    """
    status = int(getattr(exc, "status_code", 0) or 0)
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or "")

    if status in (401, 403):
        raise RotationAuthError(message or default_message) from exc
    if status == 404 and code == "vault_not_found":
        raise RotationNotFoundError(message or default_message) from exc
    if status == 429:
        raise RotationRateLimitedError(message or default_message) from exc
    raise exc


__all__ = [
    "RotationAuthError",
    "RotationError",
    "RotationNotFoundError",
    "RotationRateLimitedError",
    "RotationResponse",
    "rotate_access_secret",
]
