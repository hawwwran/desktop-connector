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

from src.vault_bindings import (  # noqa: E402
    DEFAULT_SYNC_MODE,
    VALID_BINDING_STATES,
    VALID_OP_TYPES,
    VALID_SYNC_MODES,
    VaultBindingsStore,
    VaultLocalEntry,
    generate_binding_id,
)
from src.vault_cache import VaultLocalIndex  # noqa: E402


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
