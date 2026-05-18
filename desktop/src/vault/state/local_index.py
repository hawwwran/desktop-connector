"""Vault local SQLite index.

T4.2 introduces the first local-index table: a decrypted snapshot of
remote folder metadata from the current manifest. The relay never sees
these values; this database is per desktop config directory.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..manifest import normalize_manifest_plaintext


DB_FILENAME = "vault-local-index.sqlite3"
DB_FILE_MODE = 0o600


class VaultLocalIndex:
    """SQLite-backed local Vault index."""

    def __init__(self, config_dir: Path | str):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config_dir / DB_FILENAME
        self._ensure_schema()

    def refresh_remote_folders_cache(self, manifest: dict[str, Any]) -> None:
        """Atomically replace cached remote-folder metadata for a vault."""
        normalized = normalize_manifest_plaintext(manifest)
        vault_id = str(normalized["vault_id"])
        revision = int(normalized["revision"])
        folders = [self._folder_to_row(vault_id, revision, f) for f in normalized["remote_folders"]]
        snapshot_updated_at = int(time.time())

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM vault_remote_folders_cache WHERE vault_id = ?",
                (vault_id,),
            )
            for row in folders:
                conn.execute(
                    """
                    INSERT INTO vault_remote_folders_cache (
                        vault_id,
                        remote_folder_id,
                        manifest_revision,
                        display_name_enc,
                        created_at,
                        created_by_device_id,
                        retention_policy_json,
                        ignore_patterns_json,
                        state,
                        snapshot_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["vault_id"],
                        row["remote_folder_id"],
                        row["manifest_revision"],
                        row["display_name_enc"],
                        row["created_at"],
                        row["created_by_device_id"],
                        row["retention_policy_json"],
                        row["ignore_patterns_json"],
                        row["state"],
                        snapshot_updated_at,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_manifest_revision_floor(self, vault_id: str) -> int:
        """Return the highest manifest revision this device has ever
        successfully decrypted for ``vault_id``.

        Zero means the device has never seen a manifest for this vault —
        a fresh install, a freshly restored device, or a vault that
        hasn't published its first revision yet. The §3.7 risk
        evaluation notes this as the only fundamental gap in rollback
        detection: a brand-new device with no floor cannot detect that
        the relay served an older state than someone else has already
        seen.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT highest_seen_revision FROM vault_manifest_floor WHERE vault_id = ?",
                (vault_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["highest_seen_revision"]) if row is not None else 0

    def bump_manifest_revision_floor(self, vault_id: str, revision: int) -> bool:
        """UPSERT the per-vault floor to ``max(stored, revision)``.

        Returns ``True`` if the stored floor moved, ``False`` if the
        stored floor was already ≥ ``revision`` (no-op). Callers are
        expected to have AEAD-verified the revision before bumping;
        the floor is the trust anchor for §3.7 rollback detection.
        """
        revision = int(revision)
        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT highest_seen_revision FROM vault_manifest_floor WHERE vault_id = ?",
                (vault_id,),
            ).fetchone()
            current = int(row["highest_seen_revision"]) if row is not None else 0
            if revision <= current:
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO vault_manifest_floor (vault_id, highest_seen_revision, updated_at)
                     VALUES (?, ?, ?)
                ON CONFLICT (vault_id) DO UPDATE SET
                     highest_seen_revision = excluded.highest_seen_revision,
                     updated_at = excluded.updated_at
                """,
                (vault_id, revision, now),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_manifest_rollback(
        self,
        vault_id: str,
        *,
        served_revision: int,
        floor_revision: int,
    ) -> None:
        """Latch a rollback detection. Idempotent — re-recording the
        same vault overwrites the previous detection with the latest
        served/floor pair, so the banner reflects the most recent
        observation.
        """
        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO vault_manifest_rollback_flag (
                    vault_id, served_revision, floor_revision, detected_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (vault_id) DO UPDATE SET
                     served_revision = excluded.served_revision,
                     floor_revision  = excluded.floor_revision,
                     detected_at     = excluded.detected_at
                """,
                (vault_id, int(served_revision), int(floor_revision), now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_manifest_rollback(self, vault_id: str) -> dict[str, Any] | None:
        """Return the latched rollback record for ``vault_id`` or
        ``None`` if no rollback is currently flagged.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT served_revision, floor_revision, detected_at
                  FROM vault_manifest_rollback_flag
                 WHERE vault_id = ?
                """,
                (vault_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "vault_id": vault_id,
            "served_revision": int(row["served_revision"]),
            "floor_revision": int(row["floor_revision"]),
            "detected_at": int(row["detected_at"]),
        }

    def clear_manifest_rollback(self, vault_id: str) -> None:
        """Drop the rollback flag for ``vault_id``. No-op if no flag
        is currently set.
        """
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM vault_manifest_rollback_flag WHERE vault_id = ?",
                (vault_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def list_remote_folders(self, vault_id: str) -> list[dict[str, Any]]:
        """Return cached remote folders for ``vault_id`` ordered by creation."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT vault_id,
                       remote_folder_id,
                       manifest_revision,
                       display_name_enc,
                       created_at,
                       created_by_device_id,
                       retention_policy_json,
                       ignore_patterns_json,
                       state,
                       snapshot_updated_at
                  FROM vault_remote_folders_cache
                 WHERE vault_id = ?
                 ORDER BY created_at ASC, remote_folder_id ASC
                """,
                (vault_id,),
            ).fetchall()
        finally:
            conn.close()

        return [self._row_to_folder(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Review §3.H8: WAL mode lets the watcher (writer) and the
        # sync cycle (reader) run concurrently. Pre-fix the default
        # rollback-journal mode meant every watcher write briefly
        # blocked sync reads, and a watcher burst could cause the
        # 3-second stability gate's stat reads to time out with
        # stale data. busy_timeout absorbs short contention; WAL is
        # the structural fix.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        self._tighten_permissions()
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_remote_folders_cache (
                    vault_id TEXT NOT NULL,
                    remote_folder_id TEXT NOT NULL,
                    manifest_revision INTEGER NOT NULL,
                    display_name_enc TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by_device_id TEXT NOT NULL,
                    retention_policy_json TEXT NOT NULL,
                    ignore_patterns_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    snapshot_updated_at INTEGER NOT NULL,
                    PRIMARY KEY (vault_id, remote_folder_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vault_remote_folders_cache_revision
                    ON vault_remote_folders_cache (vault_id, manifest_revision)
                """
            )
            # F-LT10: per-vault manifest revision floor. Trust anchor
            # for §3.7 rollback detection — bumped on every successful
            # AEAD-verified manifest decrypt; compared on the next
            # decrypt to refuse a relay-served downgrade.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_manifest_floor (
                    vault_id              TEXT PRIMARY KEY,
                    highest_seen_revision INTEGER NOT NULL,
                    updated_at            INTEGER NOT NULL
                )
                """
            )
            # F-LT10: latched rollback flag. Recorded inside
            # decrypt_manifest the instant a downgrade is detected;
            # read by the Vault Settings banner; auto-cleared on the
            # next successful decrypt that advances or matches the
            # floor (relay has resumed serving fresh state).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_manifest_rollback_flag (
                    vault_id        TEXT PRIMARY KEY,
                    served_revision INTEGER NOT NULL,
                    floor_revision  INTEGER NOT NULL,
                    detected_at     INTEGER NOT NULL
                )
                """
            )
            # T10.1 — local binding + sync state. Per §A12 the binding's
            # state and sync_mode are independent axes — the same row
            # carries both, plus the last manifest revision the sync
            # loop reconciled against. The schema is intentionally
            # close to a flat join table; richer per-folder config
            # (ignore patterns, etc.) lives in the encrypted manifest
            # so we don't need to duplicate it here.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_bindings (
                    binding_id            TEXT PRIMARY KEY,
                    vault_id              TEXT NOT NULL,
                    remote_folder_id      TEXT NOT NULL,
                    local_path            TEXT NOT NULL,
                    state                 TEXT NOT NULL,
                    sync_mode             TEXT NOT NULL,
                    last_synced_revision  INTEGER NOT NULL DEFAULT 0,
                    created_at            TEXT NOT NULL,
                    updated_at            TEXT NOT NULL,
                    UNIQUE (vault_id, remote_folder_id, local_path)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vault_bindings_vault_state
                    ON vault_bindings (vault_id, state)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_local_entries (
                    binding_id            TEXT NOT NULL,
                    relative_path         TEXT NOT NULL,
                    content_fingerprint   TEXT NOT NULL DEFAULT '',
                    size_bytes            INTEGER NOT NULL DEFAULT 0,
                    mtime_ns              INTEGER NOT NULL DEFAULT 0,
                    last_synced_revision  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (binding_id, relative_path),
                    FOREIGN KEY (binding_id) REFERENCES vault_bindings(binding_id)
                        ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vault_local_entries_revision
                    ON vault_local_entries (binding_id, last_synced_revision)
                """
            )
            # Pending ops queue: watcher (T10.4) enqueues here, sync loop
            # (T10.5) drains. ``op_id`` is autoincrement so the loop
            # always sees a stable FIFO order.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_pending_operations (
                    op_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding_id     TEXT NOT NULL,
                    op_type        TEXT NOT NULL,
                    relative_path  TEXT NOT NULL,
                    enqueued_at    INTEGER NOT NULL,
                    attempts       INTEGER NOT NULL DEFAULT 0,
                    last_error     TEXT,
                    FOREIGN KEY (binding_id) REFERENCES vault_bindings(binding_id)
                        ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vault_pending_operations_binding
                    ON vault_pending_operations (binding_id, enqueued_at)
                """
            )
            # F-Y10: collapse pre-existing duplicates before adding the
            # unique constraint. Keep the smallest op_id per
            # ``(binding_id, op_type, relative_path)`` triple (preserves
            # FIFO order); fold the latest ``enqueued_at`` of the
            # duplicates onto the survivor so the watcher's freshness
            # signal isn't lost. Then enforce uniqueness going forward
            # so concurrent watcher / sync calls cannot produce duplicate
            # rows for the same triple. Idempotent: re-running this
            # block on a clean DB is a no-op.
            conn.execute(
                """
                UPDATE vault_pending_operations
                SET enqueued_at = (
                    SELECT MAX(b.enqueued_at)
                      FROM vault_pending_operations b
                     WHERE b.binding_id = vault_pending_operations.binding_id
                       AND b.op_type = vault_pending_operations.op_type
                       AND b.relative_path = vault_pending_operations.relative_path
                )
                WHERE op_id IN (
                    SELECT MIN(op_id)
                      FROM vault_pending_operations
                     GROUP BY binding_id, op_type, relative_path
                )
                """
            )
            conn.execute(
                """
                DELETE FROM vault_pending_operations
                WHERE op_id NOT IN (
                    SELECT MIN(op_id)
                      FROM vault_pending_operations
                     GROUP BY binding_id, op_type, relative_path
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_vault_pending_operations_unique
                    ON vault_pending_operations (
                        binding_id, op_type, relative_path
                    )
                """
            )
            conn.commit()
        finally:
            conn.close()
        self._tighten_permissions()

    def _tighten_permissions(self) -> None:
        try:
            os.chmod(self.db_path, DB_FILE_MODE)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    @staticmethod
    def _folder_to_row(vault_id: str, revision: int, folder: dict[str, Any]) -> dict[str, Any]:
        return {
            "vault_id": vault_id,
            "remote_folder_id": str(folder["remote_folder_id"]),
            "manifest_revision": revision,
            "display_name_enc": str(folder["display_name_enc"]),
            "created_at": str(folder["created_at"]),
            "created_by_device_id": str(folder["created_by_device_id"]),
            "retention_policy_json": _canonical_json(folder["retention_policy"]),
            "ignore_patterns_json": _canonical_json(folder["ignore_patterns"]),
            "state": str(folder["state"]),
        }

    @staticmethod
    def _row_to_folder(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "vault_id": row["vault_id"],
            "remote_folder_id": row["remote_folder_id"],
            "manifest_revision": int(row["manifest_revision"]),
            "display_name_enc": row["display_name_enc"],
            "created_at": row["created_at"],
            "created_by_device_id": row["created_by_device_id"],
            "retention_policy": json.loads(row["retention_policy_json"]),
            "ignore_patterns": json.loads(row["ignore_patterns_json"]),
            "state": row["state"],
            "snapshot_updated_at": int(row["snapshot_updated_at"]),
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
