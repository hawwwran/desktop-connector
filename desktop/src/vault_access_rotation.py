"""Client-side access-secret rotation (T13.6 / §A5).

The relay endpoint is ``POST /api/vaults/{id}/access-secret/rotate``;
this module owns the *client* side: generate a new high-entropy
secret, derive the wire-shape body, post it, and persist the
"tell other devices" reminder banner state per §gaps §14.

Per T0 §A14 this is the *only* rotation in v1. Clients trigger it
explicitly via Settings → Security → Rotate access secret, OR via
the "Revoke and rotate" combo on the Devices tab (T13.5). After
rotation:

- The local cached secret is replaced — subsequent vault ops use
  the new bearer.
- A reminder file is written so the home screen shows a banner
  asking the user to share the new secret with paired devices for
  the next 7 days. The banner auto-clears on day 8 OR when every
  paired device's last_seen_at exceeds the rotation timestamp.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vault_atomic import atomic_write_file


log = logging.getLogger(__name__)


REMINDER_TTL_SECONDS = 7 * 24 * 60 * 60  # §A5: 7-day banner
SECRET_BYTES = 32                        # 256-bit entropy


@dataclass(frozen=True)
class RotationResult:
    new_secret: str
    new_secret_hash_hex: str
    rotated_at_epoch: int


@dataclass(frozen=True)
class ReminderState:
    rotated_at_epoch: int
    paired_device_ids: tuple[str, ...]
    acknowledged_device_ids: tuple[str, ...] = ()

    def is_active(self, *, now: float | None = None) -> bool:
        """True iff the banner should still be visible."""
        wall = time.time() if now is None else float(now)
        if wall - self.rotated_at_epoch >= REMINDER_TTL_SECONDS:
            return False
        outstanding = set(self.paired_device_ids) - set(self.acknowledged_device_ids)
        return bool(outstanding)


def generate_new_secret(*, byte_length: int = SECRET_BYTES) -> str:
    """Return a fresh URL-safe high-entropy secret string."""
    if byte_length < 16:
        raise ValueError("byte_length must be >= 16 for adequate entropy")
    return secrets.token_urlsafe(byte_length)


def secret_to_wire_hash(secret: str) -> dict[str, Any]:
    """Compute the relay body for ``POST .../access-secret/rotate``.

    Returns a dict with the exact field name + base64 shape the server
    expects. Hashing keeps the plaintext local — the relay never sees
    the secret bytes, only their digest.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return {
        "new_vault_access_token_hash": base64.b64encode(digest).decode("ascii"),
    }


def _compute_secret_hex_for_tests(secret: str) -> str:
    """Hex form of the SHA-256 digest. Tests-only — never log this."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    return hashlib.sha256(secret.encode("utf-8")).digest().hex()


def rotation_request_body(
    secret: str,
    *,
    triggered_by_revoke_grant_id: str | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``POST .../access-secret/rotate``.

    The optional ``triggered_by_revoke_grant_id`` ties the rotation to a
    preceding revoke (the §T13.5 combo) for the audit trail.
    """
    body = {
        "new_vault_access_token_hash":
            secret_to_wire_hash(secret)["new_vault_access_token_hash"],
    }
    if triggered_by_revoke_grant_id is not None:
        body["triggered_by_revoke_grant_id"] = str(triggered_by_revoke_grant_id)
    return body


# ---------------------------------------------------------------------------
# Reminder persistence — single file under <config_dir>/vault_rotation_<id>.json
# ---------------------------------------------------------------------------


def reminder_path(config_dir: Path, vault_id: str) -> Path:
    safe = "".join(c for c in vault_id if c.isalnum() or c == "-")
    return Path(config_dir) / f"vault_rotation_{safe}.json"


def write_reminder(
    config_dir: Path, vault_id: str, state: ReminderState,
) -> Path:
    path = reminder_path(config_dir, vault_id)
    payload = {
        "rotated_at_epoch": int(state.rotated_at_epoch),
        "paired_device_ids": list(state.paired_device_ids),
        "acknowledged_device_ids": list(state.acknowledged_device_ids),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    atomic_write_file(path, encoded)
    return path


def read_reminder(
    config_dir: Path, vault_id: str,
) -> ReminderState | None:
    path = reminder_path(config_dir, vault_id)
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.security.reminder_read_failed path=%s error=%s", path, exc,
        )
        return None
    return ReminderState(
        rotated_at_epoch=int(obj.get("rotated_at_epoch", 0)),
        paired_device_ids=tuple(obj.get("paired_device_ids", []) or []),
        acknowledged_device_ids=tuple(obj.get("acknowledged_device_ids", []) or []),
    )


def acknowledge_device(
    state: ReminderState, device_id: str,
) -> ReminderState:
    """Return a copy with ``device_id`` added to acknowledged_device_ids."""
    if device_id in state.acknowledged_device_ids:
        return state
    return ReminderState(
        rotated_at_epoch=state.rotated_at_epoch,
        paired_device_ids=state.paired_device_ids,
        acknowledged_device_ids=state.acknowledged_device_ids + (device_id,),
    )


def clear_reminder(config_dir: Path, vault_id: str) -> None:
    path = reminder_path(config_dir, vault_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "REMINDER_TTL_SECONDS",
    "ReminderState",
    "RotationResult",
    "SECRET_BYTES",
    "acknowledge_device",
    "clear_reminder",
    "generate_new_secret",
    "read_reminder",
    "reminder_path",
    "rotation_request_body",
    "secret_to_wire_hash",
    "write_reminder",
]
