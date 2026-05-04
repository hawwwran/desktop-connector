"""Client-side hard-purge scheduling (T14.3) + Toggle-OFF interaction (T14.5).

Pairs with the server's vault_gc_jobs (kind='scheduled_purge') table.
The client persistence file at
``<config_dir>/vault_pending_purges.json`` lets the desktop survive
restart between Schedule and Execute, and gives the §A17 toggle-OFF
hook a single source of truth to clear.

File schema:

    {
      "<vault_id_dashed>": {
        "job_id": "jb_v1_<24base32>",
        "vault_id": "<dashed>",
        "scope": "folder" | "vault",
        "scope_target": "<remote_folder_id>" | null,
        "scheduled_for_epoch": 1777986400,
        "scheduled_at_epoch":  1777900000,
        "scheduled_by_device_id": "<device_id>",
        "delay_seconds": 86400
      },
      ...
    }

A vault may carry at most one pending purge at a time; scheduling a
new one while another is pending raises
:class:`VaultPurgeAlreadyScheduledError`. The §A17 toggle-OFF path
should call :func:`clear_all_for_vault` so the next-on toggle starts
clean.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


log = logging.getLogger(__name__)


DEFAULT_DELAY_SECONDS = 24 * 60 * 60  # T14.3: 24-hour default
PENDING_FILE_NAME = "vault_pending_purges.json"


PurgeScope = Literal["folder", "vault"]


class VaultPurgeError(ValueError):
    """Generic schedule/cancel input failure."""


class VaultPurgeAlreadyScheduledError(VaultPurgeError):
    """Raised when the vault already has a pending purge."""


@dataclass(frozen=True)
class PendingPurge:
    job_id: str
    vault_id: str
    scope: PurgeScope
    scope_target: str | None         # remote_folder_id when scope == "folder"
    scheduled_for_epoch: int
    scheduled_at_epoch: int
    scheduled_by_device_id: str
    delay_seconds: int

    def is_due(self, *, now: float | None = None) -> bool:
        wall = time.time() if now is None else float(now)
        return wall >= float(self.scheduled_for_epoch)


_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"


def generate_job_id() -> str:
    """``jb_v1_<24 lowercase base32>`` — matches server's job_id regex."""
    raw = secrets.token_bytes(15)
    out = []
    bits = 0
    buf = 0
    for byte in raw:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32_LOWER[(buf >> bits) & 0x1f])
    return "jb_v1_" + "".join(out[:24])


def pending_file_path(config_dir: Path) -> Path:
    return Path(config_dir) / PENDING_FILE_NAME


def schedule_purge(
    config_dir: Path,
    *,
    vault_id_dashed: str,
    scope: PurgeScope,
    scope_target: str | None,
    scheduled_by_device_id: str,
    delay_seconds: int = DEFAULT_DELAY_SECONDS,
    now: float | None = None,
    job_id: str | None = None,
) -> PendingPurge:
    """Persist a freshly-scheduled hard-purge for ``vault_id_dashed``.

    Returns the :class:`PendingPurge` record. Raises
    :class:`VaultPurgeAlreadyScheduledError` if another purge is
    already pending — the user must cancel first (or wait for it to
    fire).
    """
    if scope not in ("folder", "vault"):
        raise VaultPurgeError(f"unknown scope: {scope!r}")
    if scope == "folder" and not scope_target:
        raise VaultPurgeError("scope='folder' requires scope_target=remote_folder_id")
    if scope == "vault" and scope_target is not None:
        raise VaultPurgeError("scope='vault' must have scope_target=None")
    if delay_seconds < 0:
        raise VaultPurgeError("delay_seconds must be non-negative")

    now_t = int(time.time() if now is None else now)
    record = PendingPurge(
        job_id=job_id or generate_job_id(),
        vault_id=str(vault_id_dashed),
        scope=scope,
        scope_target=scope_target,
        scheduled_for_epoch=now_t + int(delay_seconds),
        scheduled_at_epoch=now_t,
        scheduled_by_device_id=str(scheduled_by_device_id),
        delay_seconds=int(delay_seconds),
    )

    state = _read_state(config_dir)
    if vault_id_dashed in state:
        existing = _record_from_dict(state[vault_id_dashed])
        raise VaultPurgeAlreadyScheduledError(
            f"vault {vault_id_dashed!r} already has a pending purge "
            f"(job_id={existing.job_id}, scheduled_for_epoch={existing.scheduled_for_epoch})"
        )
    state[vault_id_dashed] = asdict(record)
    _write_state(config_dir, state)
    log.info(
        "vault.purge.scheduled vault=%s job_id=%s scope=%s scheduled_for=%d",
        vault_id_dashed, record.job_id, scope, record.scheduled_for_epoch,
    )
    return record


def get_pending_purge(
    config_dir: Path, vault_id_dashed: str,
) -> PendingPurge | None:
    state = _read_state(config_dir)
    raw = state.get(vault_id_dashed)
    if raw is None:
        return None
    return _record_from_dict(raw)


def list_pending_purges(config_dir: Path) -> list[PendingPurge]:
    state = _read_state(config_dir)
    return [_record_from_dict(v) for v in state.values()]


def cancel_purge(
    config_dir: Path, vault_id_dashed: str,
) -> PendingPurge | None:
    """Remove the pending purge for ``vault_id_dashed``. Returns the
    cleared record, or ``None`` if there was nothing to cancel."""
    state = _read_state(config_dir)
    raw = state.pop(vault_id_dashed, None)
    if raw is None:
        return None
    _write_state(config_dir, state)
    record = _record_from_dict(raw)
    log.info(
        "vault.purge.cancelled vault=%s job_id=%s",
        vault_id_dashed, record.job_id,
    )
    return record


def clear_all_for_vault(
    config_dir: Path, vault_id_dashed: str,
) -> PendingPurge | None:
    """T14.5: alias for cancel_purge with explicit toggle-OFF semantics."""
    return cancel_purge(config_dir, vault_id_dashed)


def _read_state(config_dir: Path) -> dict[str, Any]:
    path = pending_file_path(config_dir)
    if not path.is_file():
        return {}
    try:
        obj = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.purge.state_read_failed path=%s error=%s; treating as empty",
            path, exc,
        )
        return {}
    if not isinstance(obj, dict):
        return {}
    return obj


def _write_state(config_dir: Path, state: dict[str, Any]) -> None:
    path = pending_file_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
    tmp.replace(path)


def _record_from_dict(raw: dict[str, Any]) -> PendingPurge:
    return PendingPurge(
        job_id=str(raw["job_id"]),
        vault_id=str(raw["vault_id"]),
        scope=str(raw["scope"]),  # type: ignore[arg-type]
        scope_target=raw.get("scope_target"),
        scheduled_for_epoch=int(raw["scheduled_for_epoch"]),
        scheduled_at_epoch=int(raw["scheduled_at_epoch"]),
        scheduled_by_device_id=str(raw["scheduled_by_device_id"]),
        delay_seconds=int(raw["delay_seconds"]),
    )


__all__ = [
    "DEFAULT_DELAY_SECONDS",
    "PENDING_FILE_NAME",
    "PendingPurge",
    "PurgeScope",
    "VaultPurgeAlreadyScheduledError",
    "VaultPurgeError",
    "cancel_purge",
    "clear_all_for_vault",
    "generate_job_id",
    "get_pending_purge",
    "list_pending_purges",
    "pending_file_path",
    "schedule_purge",
]
