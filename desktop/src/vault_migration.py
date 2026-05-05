"""Vault relay-migration state machine + persistence (T9.1).

Per §H2:

    idle → started → copying → verified → committed → idle (on new relay)
             ↑                                ↓
             └──────────  rollback ───────────┘ (from started/copying/verified)

The state file lives at ``<config_dir>/vault_migration.json`` and is
rewritten in place before every transition so a crash mid-migration
recovers to a known state on next launch (§H2 recovery table).

This module is pure logic — no relay calls, no GTK. The orchestration
layer (T9.3+) reads/writes the state file via these helpers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .vault_atomic import atomic_write_file


MigrationState = Literal[
    "idle",
    "started",
    "copying",
    "verified",
    "committed",
]

ALLOWED_TRANSITIONS: dict[MigrationState, set[MigrationState]] = {
    "idle":      {"started"},
    "started":   {"copying", "idle"},   # idle = rollback
    "copying":   {"verified", "idle"},  # idle = rollback
    "verified":  {"committed", "idle"}, # idle = rollback
    "committed": {"idle"},              # idle = post-commit cleanup (relay swap done)
}

# §H2 retention: source-relay rollback option is offered for 7 days
# after commit, then ``previous_relay_url`` is dropped.
PREVIOUS_RELAY_GRACE_DAYS = 7


class MigrationTransitionError(RuntimeError):
    """Raised when a caller asks for an illegal state move."""


@dataclass
class MigrationRecord:
    """In-memory mirror of ``vault_migration.json``."""

    vault_id: str
    state: MigrationState
    source_relay_url: str
    target_relay_url: str
    started_at: str
    verified_at: str | None = None
    committed_at: str | None = None
    previous_relay_url: str | None = None
    migration_token: str | None = None

    def to_json(self) -> dict:
        return {
            "vault_id": self.vault_id,
            "state": self.state,
            "source_relay_url": self.source_relay_url,
            "target_relay_url": self.target_relay_url,
            "started_at": self.started_at,
            "verified_at": self.verified_at,
            "committed_at": self.committed_at,
            "previous_relay_url": self.previous_relay_url,
            "migration_token": self.migration_token,
        }

    @classmethod
    def from_json(cls, data: dict) -> "MigrationRecord":
        state = str(data.get("state", "idle"))
        if state not in ALLOWED_TRANSITIONS:
            raise ValueError(f"unknown migration state: {state!r}")
        return cls(
            vault_id=str(data["vault_id"]),
            state=state,  # type: ignore[arg-type]
            source_relay_url=str(data["source_relay_url"]),
            target_relay_url=str(data["target_relay_url"]),
            started_at=str(data["started_at"]),
            verified_at=data.get("verified_at"),
            committed_at=data.get("committed_at"),
            previous_relay_url=data.get("previous_relay_url"),
            migration_token=data.get("migration_token"),
        )


def default_state_path(config_dir: Path) -> Path:
    return Path(config_dir) / "vault_migration.json"


def load_state(config_dir: Path) -> MigrationRecord | None:
    """Return the persisted record, or ``None`` if the device is idle."""
    path = default_state_path(config_dir)
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return MigrationRecord.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def save_state(record: MigrationRecord, config_dir: Path) -> None:
    """Atomic write of the migration record JSON.

    Always called *before* the corresponding network op so a crash
    after the call but before the op's response can be recovered (§H2
    "state persisted before every transition").
    """
    path = default_state_path(config_dir)
    payload = json.dumps(record.to_json(), separators=(",", ":")).encode("utf-8")
    atomic_write_file(path, payload)


def clear_state(config_dir: Path) -> None:
    """Remove the state file — equivalent to "back to idle"."""
    path = default_state_path(config_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def transition(
    record: MigrationRecord,
    *,
    to: MigrationState,
    now: str | None = None,
) -> MigrationRecord:
    """Return a new record with ``state = to`` (or raise on illegal move).

    Stamps ``verified_at`` / ``committed_at`` automatically when those
    states are entered. ``rollback`` is expressed as ``to="idle"`` from
    a non-idle state.
    """
    if to not in ALLOWED_TRANSITIONS.get(record.state, set()):
        raise MigrationTransitionError(
            f"illegal transition: {record.state} → {to}"
        )
    timestamp = now or _now_rfc3339()
    new_record = MigrationRecord(
        vault_id=record.vault_id,
        state=to,
        source_relay_url=record.source_relay_url,
        target_relay_url=record.target_relay_url,
        started_at=record.started_at,
        verified_at=record.verified_at,
        committed_at=record.committed_at,
        previous_relay_url=record.previous_relay_url,
        migration_token=record.migration_token,
    )
    if to == "verified":
        new_record.verified_at = timestamp
    elif to == "committed":
        new_record.committed_at = timestamp
        # The source URL becomes the "previous relay" only after commit.
        if not new_record.previous_relay_url:
            new_record.previous_relay_url = record.source_relay_url
    return new_record


@dataclass(frozen=True)
class CrashRecovery:
    """What the next-launch code should do given a persisted state."""

    action: Literal[
        "resume_copy",
        "prompt_switch_rollback_resume_verify",
        "switch_to_target",
        "drop_previous_relay",
        "noop",
    ]
    detail: str = ""


def crash_recovery_action(record: MigrationRecord, *, now: str | None = None) -> CrashRecovery:
    """Map a persisted state to the next-launch recovery action (§H2)."""
    if record.state in ("started", "copying"):
        return CrashRecovery(
            action="resume_copy",
            detail="batch-HEAD on target, upload only the missing chunks",
        )
    if record.state == "verified":
        return CrashRecovery(
            action="prompt_switch_rollback_resume_verify",
            detail="user picks: switch / rollback / resume verify",
        )
    if record.state == "committed":
        # Within the grace period the device should switch to the target
        # and keep the previous_relay_url around for "Switch back to
        # previous relay" in Settings.
        if record.committed_at and _within_grace(record.committed_at, now=now):
            return CrashRecovery(
                action="switch_to_target",
                detail="active relay URL flips; previous_relay_url retained for 7d",
            )
        return CrashRecovery(
            action="drop_previous_relay",
            detail="grace period elapsed; clear previous_relay_url",
        )
    return CrashRecovery(action="noop")


def previous_relay_expired(record: MigrationRecord, *, now: str | None = None) -> bool:
    """True iff ``committed_at`` is older than the §H2 7-day grace period."""
    if record.committed_at is None:
        return False
    return not _within_grace(record.committed_at, now=now)


def _within_grace(committed_at: str, *, now: str | None) -> bool:
    when_committed = _parse_iso(committed_at)
    when_now = _parse_iso(now) if now else datetime.now(timezone.utc)
    if when_committed is None or when_now is None:
        return True  # fail-safe: assume still within grace if we can't parse
    return (when_now - when_committed).days < PREVIOUS_RELAY_GRACE_DAYS


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        when = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CrashRecovery",
    "MigrationRecord",
    "MigrationState",
    "MigrationTransitionError",
    "PREVIOUS_RELAY_GRACE_DAYS",
    "clear_state",
    "crash_recovery_action",
    "default_state_path",
    "load_state",
    "previous_relay_expired",
    "save_state",
    "transition",
]
