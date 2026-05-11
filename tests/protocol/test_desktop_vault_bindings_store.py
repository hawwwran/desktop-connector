"""T10.1 — Local bindings + sync queue store."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.binding.bindings import (  # noqa: E402
    DEFAULT_SYNC_MODE,
    VALID_BINDING_STATES,
    VALID_OP_TYPES,
    VALID_SYNC_MODES,
    VaultBindingsStore,
    VaultLocalEntry,
    generate_binding_id,
)
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402


class VaultBindingsSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_bindings_test_"))
        # VaultLocalIndex creates the DB file with all CREATE TABLE statements,
        # so the migration is exercised end-to-end the same way it runs in
        # production.
        self.index = VaultLocalIndex(self.tmpdir)
        self.store = VaultBindingsStore(self.index.db_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_schema_creates_all_three_tables(self) -> None:
        import sqlite3
        with sqlite3.connect(self.index.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("vault_remote_folders_cache", tables)
        self.assertIn("vault_bindings", tables)
        self.assertIn("vault_local_entries", tables)
        self.assertIn("vault_pending_operations", tables)

    def test_default_sync_mode_is_backup_only(self) -> None:
        # §gaps §20: default for new bindings is Backup only.
        self.assertEqual(DEFAULT_SYNC_MODE, "backup-only")

    def test_validation_constants_match_spec(self) -> None:
        self.assertEqual(
            VALID_BINDING_STATES,
            frozenset({"needs-preflight", "bound", "paused", "unbound"}),
        )
        self.assertEqual(
            VALID_SYNC_MODES,
            frozenset({"backup-only", "two-way", "download-only", "paused"}),
        )
        self.assertEqual(
            VALID_OP_TYPES,
            frozenset({"upload", "delete", "rename"}),
        )

    def test_create_and_get_binding(self) -> None:
        record = self.store.create_binding(
            vault_id="ABCD2345WXYZ",
            remote_folder_id="rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
            local_path="/home/u/Documents/Vault",
        )
        self.assertEqual(record.state, "needs-preflight")
        self.assertEqual(record.sync_mode, DEFAULT_SYNC_MODE)
        self.assertTrue(record.binding_id.startswith("rb_v1_"))

        same = self.store.get_binding(record.binding_id)
        self.assertIsNotNone(same)
        self.assertEqual(same.vault_id, "ABCD2345WXYZ")
        self.assertEqual(same.local_path, "/home/u/Documents/Vault")

    def test_unique_constraint_on_vault_folder_local_path(self) -> None:
        self.store.create_binding(
            vault_id="ABCD2345WXYZ",
            remote_folder_id="rf_v1_a" * 5,
            local_path="/x",
        )
        with self.assertRaises(Exception):  # sqlite3.IntegrityError
            self.store.create_binding(
                vault_id="ABCD2345WXYZ",
                remote_folder_id="rf_v1_a" * 5,
                local_path="/x",
            )

    def test_state_independent_of_sync_mode(self) -> None:
        """§A12: binding state and sync mode are independent axes.

        Pausing a binding should preserve the user's sync_mode pick so
        resume restores the same mode.
        """
        record = self.store.create_binding(
            vault_id="V" + "X" * 11,
            remote_folder_id="rf_v1_b" * 5,
            local_path="/p",
            state="bound",
            sync_mode="two-way",
        )
        self.store.update_binding_state(record.binding_id, state="paused")
        loaded = self.store.get_binding(record.binding_id)
        self.assertEqual(loaded.state, "paused")
        self.assertEqual(loaded.sync_mode, "two-way")

    def test_local_entry_upsert_and_lookup(self) -> None:
        record = self.store.create_binding(
            vault_id="V" + "Y" * 11,
            remote_folder_id="rf_v1_c" * 5,
            local_path="/q",
        )
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=record.binding_id,
            relative_path="report.txt",
            content_fingerprint="abc",
            size_bytes=42,
            mtime_ns=1_700_000_000_000_000_000,
            last_synced_revision=7,
        ))
        # Update same path → upsert (no duplicate).
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=record.binding_id,
            relative_path="report.txt",
            content_fingerprint="def",
            size_bytes=99,
            mtime_ns=1_700_000_001_000_000_000,
            last_synced_revision=8,
        ))
        entries = self.store.list_local_entries(record.binding_id)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].content_fingerprint, "def")
        self.assertEqual(entries[0].last_synced_revision, 8)

    def test_pending_op_queue_fifo_and_coalesce(self) -> None:
        record = self.store.create_binding(
            vault_id="V" + "Z" * 11,
            remote_folder_id="rf_v1_d" * 5,
            local_path="/r",
        )
        op1 = self.store.enqueue_pending_op(
            binding_id=record.binding_id, op_type="upload", relative_path="a.txt", now=100,
        )
        op2 = self.store.enqueue_pending_op(
            binding_id=record.binding_id, op_type="upload", relative_path="b.txt", now=101,
        )
        # Coalesce: same (binding, op_type, path) → existing row's
        # enqueued_at is bumped, no new row inserted.
        coalesced = self.store.coalesce_op(
            binding_id=record.binding_id, op_type="upload", relative_path="a.txt", now=200,
        )
        self.assertEqual(coalesced.op_id, op1.op_id)
        self.assertEqual(coalesced.enqueued_at, 200)

        ops = self.store.list_pending_ops(record.binding_id)
        self.assertEqual([o.op_id for o in ops], [op1.op_id, op2.op_id])
        self.assertEqual(len(ops), 2)

        self.store.mark_op_failed(op1.op_id, "permission denied")
        ops = self.store.list_pending_ops(record.binding_id)
        self.assertEqual(ops[0].attempts, 1)
        self.assertIn("permission", ops[0].last_error)

        self.assertTrue(self.store.delete_pending_op(op1.op_id))
        self.assertFalse(self.store.delete_pending_op(op1.op_id))  # idempotent

    def test_delete_binding_cascades_entries_and_ops(self) -> None:
        record = self.store.create_binding(
            vault_id="V" + "Q" * 11,
            remote_folder_id="rf_v1_e" * 5,
            local_path="/c",
        )
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=record.binding_id, relative_path="x", content_fingerprint="z",
        ))
        self.store.enqueue_pending_op(
            binding_id=record.binding_id, op_type="upload", relative_path="x",
        )
        self.store.delete_binding(record.binding_id)
        self.assertEqual(self.store.list_local_entries(record.binding_id), [])
        self.assertEqual(self.store.list_pending_ops(record.binding_id), [])

    def test_coalesce_op_is_atomic_and_idempotent(self) -> None:
        """F-Y10: ON CONFLICT DO UPDATE collapses SELECT+INSERT into a
        single statement so two concurrent callers can't fork the queue.

        We verify the post-condition (one row, latest enqueued_at)
        rather than re-engineering a thread race here — the DB-level
        unique constraint is the load-bearing guarantee, exercised by
        the duplicate-attempt unique-violation test below.
        """
        record = self.store.create_binding(
            vault_id="V" + "T" * 11,
            remote_folder_id="rf_v1_y10" * 3,
            local_path="/y10",
        )
        first = self.store.coalesce_op(
            binding_id=record.binding_id,
            op_type="upload",
            relative_path="same.txt",
            now=100,
        )
        # Re-coalescing the same triple yields the same row id with
        # ``enqueued_at`` bumped — never a second row.
        second = self.store.coalesce_op(
            binding_id=record.binding_id,
            op_type="upload",
            relative_path="same.txt",
            now=200,
        )
        self.assertEqual(second.op_id, first.op_id)
        self.assertEqual(second.enqueued_at, 200)
        ops = self.store.list_pending_ops(record.binding_id)
        self.assertEqual(len(ops), 1)

    def test_pending_ops_unique_index_rejects_duplicates(self) -> None:
        """F-Y10: the DB-level UNIQUE constraint refuses two raw
        ``enqueue_pending_op`` calls for the same triple. This is the
        backstop ``coalesce_op`` relies on for atomicity.
        """
        import sqlite3
        record = self.store.create_binding(
            vault_id="V" + "U" * 11,
            remote_folder_id="rf_v1_y10b" * 2 + "z" * 4,
            local_path="/y10b",
        )
        self.store.enqueue_pending_op(
            binding_id=record.binding_id,
            op_type="upload",
            relative_path="dup.txt",
            now=10,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.enqueue_pending_op(
                binding_id=record.binding_id,
                op_type="upload",
                relative_path="dup.txt",
                now=11,
            )

    def test_pending_ops_pre_existing_duplicates_collapsed_on_init(self) -> None:
        """F-Y10 backfill: schema init collapses pre-existing duplicate
        rows from before the unique index landed. We synthesize the
        legacy state by writing duplicates with the unique index
        temporarily dropped, close the index, and re-init the schema —
        the dedupe step must keep the smallest op_id and bump its
        ``enqueued_at`` to the max of the duplicates.
        """
        import sqlite3

        record = self.store.create_binding(
            vault_id="V" + "V" * 11,
            remote_folder_id="rf_v1_y10c" * 2 + "y" * 4,
            local_path="/y10c",
        )
        with sqlite3.connect(self.index.db_path) as conn:
            conn.execute("DROP INDEX idx_vault_pending_operations_unique")
            for ts in (50, 75, 100):
                conn.execute(
                    "INSERT INTO vault_pending_operations "
                    "(binding_id, op_type, relative_path, enqueued_at) "
                    "VALUES (?, ?, ?, ?)",
                    (record.binding_id, "upload", "old.txt", ts),
                )
            conn.commit()
        # Re-init triggers the F-Y10 backfill block (private method;
        # this is the same call ``__init__`` makes to bring schema up
        # to date on every open).
        self.index._ensure_schema()
        ops = [
            o for o in self.store.list_pending_ops(record.binding_id)
            if o.relative_path == "old.txt"
        ]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].enqueued_at, 100)

    def test_coalesce_delete_drops_pending_upload_for_same_path(self) -> None:
        """F-Y11: enqueueing a delete supersedes any pending upload for
        the same path. The user deleted what we hadn't uploaded yet —
        running both ops in series would tombstone a now-missing file
        and then the explicit delete would no-op.
        """
        record = self.store.create_binding(
            vault_id="V" + "W" * 11,
            remote_folder_id="rf_v1_y11" * 3,
            local_path="/y11",
        )
        self.store.coalesce_op(
            binding_id=record.binding_id,
            op_type="upload",
            relative_path="doomed.txt",
            now=100,
        )
        self.store.coalesce_op(
            binding_id=record.binding_id,
            op_type="delete",
            relative_path="doomed.txt",
            now=200,
        )
        ops = self.store.list_pending_ops(record.binding_id)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].op_type, "delete")
        self.assertEqual(ops[0].relative_path, "doomed.txt")

    def test_coalesce_upload_drops_pending_delete_for_same_path(self) -> None:
        """F-Y11 — the symmetric case. User recreated the file before
        the queue drained the delete; the queued tombstone is stale.
        """
        record = self.store.create_binding(
            vault_id="V" + "X" * 11,
            remote_folder_id="rf_v1_y11b" * 2 + "y" * 4,
            local_path="/y11b",
        )
        self.store.coalesce_op(
            binding_id=record.binding_id,
            op_type="delete",
            relative_path="resurrected.txt",
            now=100,
        )
        self.store.coalesce_op(
            binding_id=record.binding_id,
            op_type="upload",
            relative_path="resurrected.txt",
            now=200,
        )
        ops = self.store.list_pending_ops(record.binding_id)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].op_type, "upload")

    def test_coalesce_does_not_drop_other_path_or_other_binding(self) -> None:
        """F-Y11: cross-type drops are scoped to (binding, path).
        A delete on ``a.txt`` must not touch an upload on ``b.txt``,
        nor an upload on ``a.txt`` under a different binding.
        """
        a = self.store.create_binding(
            vault_id="V" + "Y" * 11,
            remote_folder_id="rf_v1_y11c" * 2 + "y" * 4,
            local_path="/y11c-a",
        )
        b = self.store.create_binding(
            vault_id="V" + "Y" * 11,
            remote_folder_id="rf_v1_y11c2" * 2 + "x" * 2,
            local_path="/y11c-b",
        )
        self.store.coalesce_op(
            binding_id=a.binding_id, op_type="upload",
            relative_path="a.txt", now=10,
        )
        self.store.coalesce_op(
            binding_id=a.binding_id, op_type="upload",
            relative_path="b.txt", now=10,
        )
        self.store.coalesce_op(
            binding_id=b.binding_id, op_type="upload",
            relative_path="a.txt", now=10,
        )
        # Enqueue a delete for a.txt under binding A.
        self.store.coalesce_op(
            binding_id=a.binding_id, op_type="delete",
            relative_path="a.txt", now=20,
        )
        a_ops = sorted(
            self.store.list_pending_ops(a.binding_id),
            key=lambda o: o.relative_path,
        )
        b_ops = self.store.list_pending_ops(b.binding_id)
        self.assertEqual(
            [(o.relative_path, o.op_type) for o in a_ops],
            [("a.txt", "delete"), ("b.txt", "upload")],
        )
        self.assertEqual(
            [(o.relative_path, o.op_type) for o in b_ops],
            [("a.txt", "upload")],
        )

    def test_invalid_state_or_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_binding(
                vault_id="V" + "R" * 11,
                remote_folder_id="rf_v1_f" * 5,
                local_path="/d",
                state="invented",
            )
        with self.assertRaises(ValueError):
            self.store.create_binding(
                vault_id="V" + "R" * 11,
                remote_folder_id="rf_v1_g" * 5,
                local_path="/d",
                sync_mode="garbage",
            )


class GenerateBindingIdTests(unittest.TestCase):
    def test_format_matches_id_space_convention(self) -> None:
        bid = generate_binding_id()
        self.assertTrue(bid.startswith("rb_v1_"))
        self.assertEqual(len(bid), 30)
        suffix = bid[len("rb_v1_"):]
        self.assertEqual(len(suffix), 24)
        for ch in suffix:
            self.assertIn(ch, "abcdefghijklmnopqrstuvwxyz234567")

    def test_ids_are_unique_in_practice(self) -> None:
        ids = {generate_binding_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)


if __name__ == "__main__":
    unittest.main()
