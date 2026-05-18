"""Crash-recovery marker for the access-secret rotation wizard (§5.H3 / B1).

The rotation flow has a narrow but real bricking window: between the
relay accepting ``POST /api/vaults/{id}/access-secret/rotate`` (server
now holds the new secret) and the local keyring grant being updated
(local cache still holds the OLD secret), a SIGKILL / OOM / GTK crash
leaves the device in a state where every vault op returns 401. For a
single-device user with no peer (§5.C2) to re-grant access, this is
unrecoverable.

This module persists a marker file across the unsafe window so a
re-launched wizard can detect the in-progress rotation and finish
saving the new secret to the keyring:

1. **Before** ``rotate_access_secret`` POSTs to the server, the
   wizard writes ``vault_rotation_in_progress_<vault_id>.json``
   containing the new secret (mode 0600).
2. The POST happens. If it fails, the marker is deleted (rotation
   never committed; the old secret is still good).
3. ``store.save(new_grant)`` updates the local keyring.
4. The marker is deleted.

On wizard re-launch the recovery flow probes the relay with the
device's currently-cached secret:

- 200 → secret still works → POST never committed; discard marker.
- 401 → server has the new secret → save marker's ``new_secret`` to
  keyring; discard marker.
- network error → preserve marker, ask user to retry.

The marker is the only data that crosses the unsafe window. It's
written atomically with ``mode=0o600`` so a passive disk-image
attacker (a backup or recovery image) doesn't widen the secret's
exposure window beyond what the keyring already permits.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..atomic import atomic_write_file


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RotationMarker:
    """In-flight rotation record persisted across the bricking window."""

    vault_id: str          # 12-char canonical undashed
    new_secret: str        # URL-safe token from generate_new_secret
    started_at: str        # RFC3339 timestamp, diagnostic only


def marker_path(config_dir: Path, vault_id_undashed: str) -> Path:
    safe = "".join(c for c in vault_id_undashed if c.isalnum())
    return Path(config_dir) / f"vault_rotation_in_progress_{safe}.json"


def write_marker(
    config_dir: Path, marker: RotationMarker,
) -> Path:
    """Atomically persist the marker at mode 0600.

    The mode lock matches the recovery-kit's file mode (see
    ``recovery_kit.py::write_recovery_kit_file``) and matches the
    same threat model: physical custody plus an additional secret
    is what authorizes use; the file alone is not enough.
    """
    path = marker_path(config_dir, marker.vault_id)
    payload = {
        "vault_id":   marker.vault_id,
        "new_secret": marker.new_secret,
        "started_at": marker.started_at,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    atomic_write_file(path, encoded, mode=0o600)
    return path


def read_marker(
    config_dir: Path, vault_id_undashed: str,
) -> RotationMarker | None:
    path = marker_path(config_dir, vault_id_undashed)
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.rotate.marker_read_failed path=%s error=%s; treating as absent",
            path, exc,
        )
        return None
    try:
        return RotationMarker(
            vault_id=str(obj["vault_id"]),
            new_secret=str(obj["new_secret"]),
            started_at=str(obj.get("started_at") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_marker(config_dir: Path, vault_id_undashed: str) -> None:
    """Delete the marker. Idempotent — missing file is success."""
    path = marker_path(config_dir, vault_id_undashed)
    try:
        path.unlink()
    except FileNotFoundError:
        return


ProbeResult = Literal["secret_works", "secret_invalid", "network_error"]


def probe_relay_with_secret(
    relay, vault_id_undashed: str, candidate_secret: str,
) -> ProbeResult:
    """Hit a vault-auth'd endpoint to test whether ``candidate_secret``
    still authenticates against the relay.

    Returns:

    - ``"secret_works"`` if the relay returned 200 (secret is valid).
    - ``"secret_invalid"`` if the relay returned 401 (secret rejected
      — typically meaning the post-rotation secret is the active one).
    - ``"network_error"`` for anything else (timeout, connection
      refused, 5xx). The caller must NOT discard the marker on this
      result — try again later.

    Uses ``GET /api/vaults/{id}/header`` because it requires vault
    auth, returns quickly, and doesn't mutate state.
    """
    try:
        resp = relay._conn.request(  # noqa: SLF001 — intentional low-level probe
            "GET",
            f"/api/vaults/{vault_id_undashed}/header",
            headers={"X-Vault-Authorization": f"Bearer {candidate_secret}"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.rotate.probe_unreachable error=%s", exc,
        )
        return "network_error"
    if resp is None:
        return "network_error"
    if resp.status_code == 200:
        return "secret_works"
    if resp.status_code == 401:
        return "secret_invalid"
    log.warning(
        "vault.rotate.probe_unexpected status=%d", resp.status_code,
    )
    return "network_error"


__all__ = [
    "ProbeResult",
    "RotationMarker",
    "clear_marker",
    "marker_path",
    "probe_relay_with_secret",
    "read_marker",
    "write_marker",
]
