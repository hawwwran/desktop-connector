"""Local binding + sync queue store (T10.1).

Sits on top of ``vault-local-index.sqlite3`` (created by
:class:`VaultLocalIndex`) and owns three tables:

- ``vault_bindings`` — one row per (vault_id, remote_folder_id, local_path).
  Carries the §A12 *binding state* and the §gaps §20 *sync mode* as
  independent axes.
- ``vault_local_entries`` — one row per local file under a binding.
  Tracks ``content_fingerprint`` + ``last_synced_revision`` so the sync
  loop (T10.5) knows what's already up.
- ``vault_pending_operations`` — FIFO queue from the watcher (T10.4)
  to the sync loop.

This module is the lowest layer — pure SQL + dataclasses. The sync
loop, watcher, and connect-folder UI sit on top of it.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal


BindingState = Literal[
    "needs-preflight",
    "bound",
    "paused",
    "unbound",
]
SyncMode = Literal[
    "backup-only",
    "two-way",
    "download-only",
    "paused",
]
OpType = Literal["upload", "delete", "rename"]


VALID_BINDING_STATES: frozenset[BindingState] = frozenset(
    {"needs-preflight", "bound", "paused", "unbound"}
)
VALID_SYNC_MODES: frozenset[SyncMode] = frozenset(
    {"backup-only", "two-way", "download-only", "paused"}
)
VALID_OP_TYPES: frozenset[OpType] = frozenset({"upload", "delete", "rename"})

DEFAULT_SYNC_MODE: SyncMode = "backup-only"  # §gaps §20


_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"


def generate_binding_id() -> str:
    """``rb_v1_<24 lowercase base32>`` — same id-space style as folder/file ids."""
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
    return "rb_v1_" + "".join(out[:24])


@dataclass
class VaultBinding:
    binding_id: str
    vault_id: str
    remote_folder_id: str
    local_path: str
    state: BindingState
    sync_mode: SyncMode
    last_synced_revision: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass
class VaultLocalEntry:
    binding_id: str
    relative_path: str
    content_fingerprint: str = ""
    size_bytes: int = 0
    mtime_ns: int = 0
    last_synced_revision: int = 0


@dataclass
class VaultPendingOperation:
    op_id: int
    binding_id: str
    op_type: OpType
    relative_path: str
    enqueued_at: int
    attempts: int = 0
    last_error: str | None = None


class VaultBindingsStore:
    """Bindings + local-entries + pending-ops storage on the per-device DB."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def create_binding(
        self,
        *,
        vault_id: str,
        remote_folder_id: str,
        local_path: str,
        state: BindingState = "needs-preflight",
        sync_mode: SyncMode = DEFAULT_SYNC_MODE,
        binding_id: str | None = None,
        now: str | None = None,
    ) -> VaultBinding:
        _validate_state(state)
        _validate_sync_mode(sync_mode)
        binding_id = binding_id or generate_binding_id()
        timestamp = now or _now_rfc3339()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vault_bindings (
                    binding_id, vault_id, remote_folder_id, local_path,
                    state, sync_mode, last_synced_revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    binding_id, vault_id, remote_folder_id, str(local_path),
                    state, sync_mode, timestamp, timestamp,
                ),
            )
            conn.commit()
        return VaultBinding(
            binding_id=binding_id,
            vault_id=vault_id,
            remote_folder_id=remote_folder_id,
            local_path=str(local_path),
            state=state,
            sync_mode=sync_mode,
            last_synced_revision=0,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def get_binding(self, binding_id: str) -> VaultBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vault_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        return _row_to_binding(row) if row else None

    def list_bindings(
        self,
        *,
        vault_id: str | None = None,
        state: BindingState | None = None,
    ) -> list[VaultBinding]:
        sql = "SELECT * FROM vault_bindings WHERE 1=1"
        params: list[Any] = []
        if vault_id is not None:
            sql += " AND vault_id = ?"
            params.append(vault_id)
        if state is not None:
            _validate_state(state)
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY created_at ASC, binding_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_binding(r) for r in rows]

    def update_binding_state(
        self,
        binding_id: str,
        *,
        state: BindingState | None = None,
        sync_mode: SyncMode | None = None,
        last_synced_revision: int | None = None,
        now: str | None = None,
    ) -> bool:
        if state is None and sync_mode is None and last_synced_revision is None:
            return False
        if state is not None:
            _validate_state(state)
        if sync_mode is not None:
            _validate_sync_mode(sync_mode)
        timestamp = now or _now_rfc3339()
        sets: list[str] = []
        params: list[Any] = []
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if sync_mode is not None:
            sets.append("sync_mode = ?")
            params.append(sync_mode)
        if last_synced_revision is not None:
            sets.append("last_synced_revision = ?")
            params.append(int(last_synced_revision))
        sets.append("updated_at = ?")
        params.append(timestamp)
        params.append(binding_id)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE vault_bindings SET "
                + ", ".join(sets)
                + " WHERE binding_id = ?",
                params,
            )
            conn.commit()
            return cursor.rowcount == 1

    def delete_binding(self, binding_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM vault_bindings WHERE binding_id = ?",
                (binding_id,),
            )
            conn.commit()
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Local entries
    # ------------------------------------------------------------------

    def upsert_local_entry(self, entry: VaultLocalEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vault_local_entries (
                    binding_id, relative_path, content_fingerprint,
                    size_bytes, mtime_ns, last_synced_revision
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_id, relative_path) DO UPDATE SET
                    content_fingerprint = excluded.content_fingerprint,
                    size_bytes = excluded.size_bytes,
                    mtime_ns = excluded.mtime_ns,
                    last_synced_revision = excluded.last_synced_revision
                """,
                (
                    entry.binding_id, entry.relative_path,
                    str(entry.content_fingerprint or ""),
                    int(entry.size_bytes or 0),
                    int(entry.mtime_ns or 0),
                    int(entry.last_synced_revision or 0),
                ),
            )
            conn.commit()

    def get_local_entry(self, binding_id: str, relative_path: str) -> VaultLocalEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vault_local_entries WHERE binding_id = ? AND relative_path = ?",
                (binding_id, relative_path),
            ).fetchone()
        return _row_to_local_entry(row) if row else None

    def list_local_entries(self, binding_id: str) -> list[VaultLocalEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vault_local_entries WHERE binding_id = ? ORDER BY relative_path",
                (binding_id,),
            ).fetchall()
        return [_row_to_local_entry(r) for r in rows]

    def delete_local_entry(self, binding_id: str, relative_path: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM vault_local_entries WHERE binding_id = ? AND relative_path = ?",
                (binding_id, relative_path),
            )
            conn.commit()
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Pending operations queue
    # ------------------------------------------------------------------

    def enqueue_pending_op(
        self,
        *,
        binding_id: str,
        op_type: OpType,
        relative_path: str,
        now: int | None = None,
    ) -> VaultPendingOperation:
        if op_type not in VALID_OP_TYPES:
            raise ValueError(f"unknown op_type: {op_type!r}")
        enqueued = int(now if now is not None else time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO vault_pending_operations (
                    binding_id, op_type, relative_path, enqueued_at
                ) VALUES (?, ?, ?, ?)
                """,
                (binding_id, op_type, relative_path, enqueued),
            )
            conn.commit()
            op_id = int(cursor.lastrowid or 0)
        return VaultPendingOperation(
            op_id=op_id,
            binding_id=binding_id,
            op_type=op_type,
            relative_path=relative_path,
            enqueued_at=enqueued,
            attempts=0,
            last_error=None,
        )

    def list_pending_ops(
        self, binding_id: str | None = None,
    ) -> list[VaultPendingOperation]:
        sql = "SELECT * FROM vault_pending_operations"
        params: list[Any] = []
        if binding_id is not None:
            sql += " WHERE binding_id = ?"
            params.append(binding_id)
        sql += " ORDER BY op_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_pending_op(r) for r in rows]

    def mark_op_failed(self, op_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE vault_pending_operations
                SET attempts = attempts + 1, last_error = ?
                WHERE op_id = ?
                """,
                (str(error)[:500], int(op_id)),
            )
            conn.commit()

    def delete_pending_op(self, op_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM vault_pending_operations WHERE op_id = ?",
                (int(op_id),),
            )
            conn.commit()
            return cursor.rowcount == 1

    def coalesce_op(
        self,
        *,
        binding_id: str,
        op_type: OpType,
        relative_path: str,
        now: int | None = None,
    ) -> VaultPendingOperation:
        """Insert if not already pending; otherwise refresh enqueued_at.

        The watcher fires more often than the sync loop drains, so the
        same path can show up many times in a short window. Coalescing
        at insert time keeps the queue O(distinct paths) instead of
        O(filesystem events).
        """
        existing = self._first_pending_op_for_path(binding_id, op_type, relative_path)
        if existing is None:
            return self.enqueue_pending_op(
                binding_id=binding_id,
                op_type=op_type,
                relative_path=relative_path,
                now=now,
            )
        enqueued = int(now if now is not None else time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE vault_pending_operations SET enqueued_at = ? WHERE op_id = ?",
                (enqueued, existing.op_id),
            )
            conn.commit()
        existing.enqueued_at = enqueued
        return existing

    def _first_pending_op_for_path(
        self, binding_id: str, op_type: OpType, relative_path: str,
    ) -> VaultPendingOperation | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM vault_pending_operations
                WHERE binding_id = ? AND op_type = ? AND relative_path = ?
                ORDER BY op_id ASC LIMIT 1
                """,
                (binding_id, op_type, relative_path),
            ).fetchone()
        return _row_to_pending_op(row) if row else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


def _row_to_binding(row: sqlite3.Row) -> VaultBinding:
    return VaultBinding(
        binding_id=row["binding_id"],
        vault_id=row["vault_id"],
        remote_folder_id=row["remote_folder_id"],
        local_path=row["local_path"],
        state=row["state"],
        sync_mode=row["sync_mode"],
        last_synced_revision=int(row["last_synced_revision"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_local_entry(row: sqlite3.Row) -> VaultLocalEntry:
    return VaultLocalEntry(
        binding_id=row["binding_id"],
        relative_path=row["relative_path"],
        content_fingerprint=row["content_fingerprint"],
        size_bytes=int(row["size_bytes"]),
        mtime_ns=int(row["mtime_ns"]),
        last_synced_revision=int(row["last_synced_revision"]),
    )


def _row_to_pending_op(row: sqlite3.Row) -> VaultPendingOperation:
    return VaultPendingOperation(
        op_id=int(row["op_id"]),
        binding_id=row["binding_id"],
        op_type=row["op_type"],
        relative_path=row["relative_path"],
        enqueued_at=int(row["enqueued_at"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
    )


def _validate_state(state: str) -> None:
    if state not in VALID_BINDING_STATES:
        raise ValueError(
            f"invalid binding state: {state!r} "
            f"(expected one of {sorted(VALID_BINDING_STATES)})"
        )


def _validate_sync_mode(mode: str) -> None:
    if mode not in VALID_SYNC_MODES:
        raise ValueError(
            f"invalid sync mode: {mode!r} "
            f"(expected one of {sorted(VALID_SYNC_MODES)})"
        )


def _now_rfc3339() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "BindingState",
    "DEFAULT_SYNC_MODE",
    "OpType",
    "SyncMode",
    "VALID_BINDING_STATES",
    "VALID_OP_TYPES",
    "VALID_SYNC_MODES",
    "VaultBinding",
    "VaultBindingsStore",
    "VaultLocalEntry",
    "VaultPendingOperation",
    "generate_binding_id",
]
