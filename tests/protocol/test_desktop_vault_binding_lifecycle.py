"""T12.4 — Pause / Resume per binding. T12.5 — Disconnect."""

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

from src.vault.binding.lifecycle import (  # noqa: E402
    DisconnectResult, PauseResult, ResumeResult,
    disconnect_binding, pause_binding, resume_binding,
)
from src.vault.binding.bindings import VaultBindingsStore, VaultLocalEntry  # noqa: E402
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"


class PauseResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_lifecycle_test_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.index = VaultLocalIndex(self.tmpdir / "config")
        self.store = VaultBindingsStore(self.index.db_path)
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_bound_two_way(self) -> str:
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound", sync_mode="two-way",
            last_synced_revision=42,
        )
        return binding.binding_id

    # ------------------------------------------------------------------
    # Pause
    # ------------------------------------------------------------------

    def test_pause_flips_state_keeps_sync_mode(self) -> None:
        bid = self._make_bound_two_way()
        # Some pending ops the user hasn't drained yet.
        self.store.coalesce_op(binding_id=bid, op_type="upload", relative_path="a.txt")
        self.store.coalesce_op(binding_id=bid, op_type="upload", relative_path="b.txt")

        result = pause_binding(self.store, bid)
        self.assertIsInstance(result, PauseResult)
        self.assertEqual(result.binding.state, "paused")
        self.assertEqual(result.binding.sync_mode, "two-way")  # §A12 preserved
        self.assertEqual(result.pending_ops_preserved, 2)

        # Pending ops still in queue.
        self.assertEqual(len(self.store.list_pending_ops(bid)), 2)

    def test_pause_idempotent_on_already_paused(self) -> None:
        bid = self._make_bound_two_way()
        pause_binding(self.store, bid)
        # Second pause is a no-op, doesn't raise.
        result = pause_binding(self.store, bid)
        self.assertEqual(result.binding.state, "paused")

    def test_pause_refuses_needs_preflight_state(self) -> None:
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        # Still in "needs-preflight"
        with self.assertRaises(ValueError):
            pause_binding(self.store, binding.binding_id)

    def test_pause_unknown_binding_raises(self) -> None:
        with self.assertRaises(KeyError):
            pause_binding(self.store, "rb_v1_nope")

    # ------------------------------------------------------------------
    # Paused binding does no traffic
    # ------------------------------------------------------------------

    def test_paused_binding_refuses_two_way_cycle(self) -> None:
        from src.vault.binding.twoway import run_two_way_cycle

        bid = self._make_bound_two_way()
        pause_binding(self.store, bid)
        binding = self.store.get_binding(bid)

        # The cycle refuses because state != "bound" — so no traffic
        # leaves the device while the user reviews changes.
        with self.assertRaises(ValueError):
            run_two_way_cycle(
                vault=_FakeVault(), relay=_FakeRelay(),
                store=self.store, binding=binding,
                author_device_id="0" * 32,
                device_name="Test",
            )

    def test_paused_binding_refuses_backup_only_cycle(self) -> None:
        from src.vault.binding.sync import run_backup_only_cycle

        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        # Bound, backup-only.
        self.store.update_binding_state(
            binding.binding_id, state="bound", sync_mode="backup-only",
        )
        pause_binding(self.store, binding.binding_id)
        binding = self.store.get_binding(binding.binding_id)
        with self.assertRaises(ValueError):
            run_backup_only_cycle(
                vault=_FakeVault(), relay=_FakeRelay(),
                store=self.store, binding=binding,
                author_device_id="0" * 32,
            )

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    def test_resume_flips_state_back_calls_flush(self) -> None:
        bid = self._make_bound_two_way()
        pause_binding(self.store, bid)

        flush_calls: list[str] = []
        def fake_flush(binding):
            flush_calls.append(binding.binding_id)
            return "flushed-ok"

        result = resume_binding(self.store, bid, flush=fake_flush)
        self.assertIsInstance(result, ResumeResult)
        self.assertEqual(result.binding.state, "bound")
        self.assertEqual(result.binding.sync_mode, "two-way")
        self.assertEqual(result.flushed, "flushed-ok")
        self.assertEqual(flush_calls, [bid])

    def test_resume_with_no_flush_just_transitions(self) -> None:
        bid = self._make_bound_two_way()
        pause_binding(self.store, bid)

        result = resume_binding(self.store, bid)
        self.assertEqual(result.binding.state, "bound")
        self.assertIsNone(result.flushed)

    def test_resume_drains_preserved_pending_ops(self) -> None:
        """Resume should let the cycle see the queued ops untouched."""
        bid = self._make_bound_two_way()
        self.store.coalesce_op(binding_id=bid, op_type="upload", relative_path="a.txt")
        self.store.coalesce_op(binding_id=bid, op_type="upload", relative_path="b.txt")
        pause_binding(self.store, bid)

        observed_pending: list[int] = []
        def fake_flush(binding):
            observed_pending.append(
                len(self.store.list_pending_ops(binding.binding_id))
            )
            return None

        resume_binding(self.store, bid, flush=fake_flush)
        # Flush saw both ops still in the queue.
        self.assertEqual(observed_pending, [2])

    def test_resume_already_bound_is_noop_but_calls_flush(self) -> None:
        bid = self._make_bound_two_way()
        called = [False]
        def fake_flush(binding):
            called[0] = True

        result = resume_binding(self.store, bid, flush=fake_flush)
        self.assertEqual(result.binding.state, "bound")
        # Flush still runs so a "Sync now" caller can rely on it.
        self.assertTrue(called[0])

    def test_resume_refuses_when_sync_mode_paused(self) -> None:
        bid = self._make_bound_two_way()
        # Force sync_mode to paused (legacy callers used this as the pause
        # signal); resume must refuse rather than silently re-enter the
        # paused state.
        self.store.update_binding_state(bid, state="paused", sync_mode="paused")
        with self.assertRaises(ValueError):
            resume_binding(self.store, bid)


class DisconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_disconnect_test_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.index = VaultLocalIndex(self.tmpdir / "config")
        self.store = VaultBindingsStore(self.index.db_path)
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_bound_with_state(self) -> str:
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound", sync_mode="two-way",
            last_synced_revision=42,
        )
        # Seed some local entries + pending ops + a real local file.
        for i in range(3):
            (self.local_root / f"file{i}.txt").write_bytes(b"x" * 16)
            self.store.upsert_local_entry(VaultLocalEntry(
                binding_id=binding.binding_id,
                relative_path=f"file{i}.txt",
                content_fingerprint="fp" * 8,
                size_bytes=16, mtime_ns=1_000_000_000,
                last_synced_revision=42,
            ))
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload", relative_path="pending.txt",
        )
        return binding.binding_id

    def test_disconnect_marks_unbound_preserves_local_entries(self) -> None:
        bid = self._make_bound_with_state()
        # Review §3.M5 — pass force=True so the existing pending op
        # (created in setup) is dropped without raising the new gate.
        result = disconnect_binding(self.store, bid, force=True)

        self.assertIsInstance(result, DisconnectResult)
        self.assertEqual(result.binding_id, bid)
        self.assertEqual(result.local_entries_preserved, 3)
        self.assertEqual(result.pending_ops_dropped, 1)

        # Binding row still present, but flipped to unbound.
        binding = self.store.get_binding(bid)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.state, "unbound")

        # Local entries survive (browse-mode reuse, fast preflight on reconnect).
        self.assertEqual(len(self.store.list_local_entries(bid)), 3)

        # Pending ops cleared so a stale watcher can't replay them.
        self.assertEqual(self.store.list_pending_ops(bid), [])

    def test_disconnect_leaves_local_filesystem_untouched(self) -> None:
        bid = self._make_bound_with_state()
        before = sorted(p.name for p in self.local_root.iterdir())
        disconnect_binding(self.store, bid, force=True)
        after = sorted(p.name for p in self.local_root.iterdir())
        self.assertEqual(before, after)

    def test_disconnect_disables_traffic(self) -> None:
        from src.vault.binding.twoway import run_two_way_cycle

        bid = self._make_bound_with_state()
        disconnect_binding(self.store, bid, force=True)
        binding = self.store.get_binding(bid)
        with self.assertRaises(ValueError):
            run_two_way_cycle(
                vault=_FakeVault(), relay=_FakeRelay(),
                store=self.store, binding=binding,
                author_device_id="0" * 32,
                device_name="Test",
            )

    def test_disconnect_idempotent_on_already_unbound(self) -> None:
        bid = self._make_bound_with_state()
        disconnect_binding(self.store, bid, force=True)
        result = disconnect_binding(self.store, bid)
        self.assertEqual(result.pending_ops_dropped, 0)
        # Local-entries count unchanged from the second pass.
        self.assertEqual(result.local_entries_preserved, 3)

    def test_disconnect_unknown_binding_raises(self) -> None:
        with self.assertRaises(KeyError):
            disconnect_binding(self.store, "rb_v1_nope")

    def test_disconnect_refuses_when_pending_ops_without_force(self) -> None:
        """Review §3.M5 — silent drop of pending ops is gone. Calling
        ``disconnect_binding`` without ``force=True`` while a pending
        op exists raises the typed ``VaultDisconnectHasPendingOpsError``
        and leaves the binding state untouched.
        """
        from src.vault.binding.lifecycle import VaultDisconnectHasPendingOpsError
        bid = self._make_bound_with_state()
        # Sanity: setup arranged one pending op.
        self.assertEqual(len(self.store.list_pending_ops(bid)), 1)
        with self.assertRaises(VaultDisconnectHasPendingOpsError) as ctx:
            disconnect_binding(self.store, bid)
        self.assertEqual(ctx.exception.pending_count, 1)
        self.assertEqual(ctx.exception.binding_id, bid)
        # Binding state untouched, pending op survives for the next cycle.
        self.assertEqual(self.store.get_binding(bid).state, "bound")
        self.assertEqual(len(self.store.list_pending_ops(bid)), 1)

    def test_disconnect_then_reconnect_revives_unbound_row_in_place(self) -> None:
        """Reconnecting after disconnect reuses the tombstone row.

        Disconnect leaves an ``unbound`` row so the preserved
        ``vault_local_entries`` survive for fast reconnect. Calling
        ``create_binding`` for the same ``(vault, folder, path)`` triple
        must flip the tombstone back to ``needs-preflight`` rather than
        hitting the schema's UNIQUE constraint.
        """
        bid = self._make_bound_with_state()
        disconnect_binding(self.store, bid, force=True)
        revived = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
            sync_mode="two-way",
        )
        self.assertEqual(revived.binding_id, bid)
        self.assertEqual(revived.state, "needs-preflight")
        self.assertEqual(revived.sync_mode, "two-way")


# ---------------------------------------------------------------------------
# Cheap fakes — only needed to satisfy the cycle's signature; the cycle
# raises before it touches any of the fake's methods.
# ---------------------------------------------------------------------------


class _FakeVault:
    """Minimal vault stub. The lifecycle tests that hand it in expect the
    sync entry point to bail before touching any vault methods (state !=
    bound), so the stub doesn't need to implement them.
    """
    vault_id = VAULT_ID
    master_key = b"\x00" * 32
    vault_access_secret = "vault-secret"


class _FakeRelay:
    """Companion stub for :class:`_FakeVault`."""
    pass


class CatalogConsistencyTests(unittest.TestCase):
    """F-Y24 polish — the noop log lines (binding_pause_noop /
    binding_resume_noop / binding_disconnect_noop) used to emit a
    trailing ``already_paused`` / ``already_bound`` / ``already_unbound``
    token beyond the documented ``binding`` field set. The event name
    already encodes the noop fact, so the suffix was redundant — and a
    silent divergence from the diagnostics catalog. These tests pin the
    field set so a future contributor noticing the line and "fixing" it
    by re-adding the suffix trips the assertion.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_lifecycle_logs_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.index = VaultLocalIndex(self.tmpdir / "config")
        self.store = VaultBindingsStore(self.index.db_path)
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_binding(self, *, state: str, sync_mode: str = "two-way"):
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state=state, sync_mode=sync_mode,
            last_synced_revision=0,
        )
        return binding.binding_id

    def test_pause_noop_log_line_has_only_binding_field(self) -> None:
        bid = self._make_binding(state="paused")
        with self.assertLogs("src.vault.binding.lifecycle", level="INFO") as cm:
            pause_binding(self.store, bid)
        noop = [ln for ln in cm.output if "binding_pause_noop" in ln]
        self.assertEqual(len(noop), 1, cm.output)
        self.assertNotIn("already_paused", noop[0])

    def test_resume_noop_log_line_has_only_binding_field(self) -> None:
        bid = self._make_binding(state="bound")
        with self.assertLogs("src.vault.binding.lifecycle", level="INFO") as cm:
            resume_binding(self.store, bid, flush=None)
        noop = [ln for ln in cm.output if "binding_resume_noop" in ln]
        self.assertEqual(len(noop), 1, cm.output)
        self.assertNotIn("already_bound", noop[0])

    def test_disconnect_noop_log_line_has_only_binding_field(self) -> None:
        bid = self._make_binding(state="unbound")
        with self.assertLogs("src.vault.binding.lifecycle", level="INFO") as cm:
            disconnect_binding(self.store, bid, force=True)
        noop = [ln for ln in cm.output if "binding_disconnect_noop" in ln]
        self.assertEqual(len(noop), 1, cm.output)
        self.assertNotIn("already_unbound", noop[0])


class DisconnectAuditTrailTests(unittest.TestCase):
    """F-Y30 polish — disconnect drops every pending op silently before
    landing the summary line. For a user with 200 queued uploads losing
    the per-op audit trail eliminates any forensic recourse. Per-op log
    lines now bracket the deletion loop, capped at
    ``DISCONNECT_AUDIT_LOG_CAP`` to keep volume sane on pathological
    queues.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_disconnect_audit_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.index = VaultLocalIndex(self.tmpdir / "config")
        self.store = VaultBindingsStore(self.index.db_path)
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_bound_with_pending(self, *, n_ops: int) -> str:
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound", sync_mode="two-way",
        )
        for i in range(n_ops):
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=f"pending-{i:03d}.txt",
            )
        return binding.binding_id

    def test_each_pending_op_logs_an_audit_line(self) -> None:
        bid = self._make_bound_with_pending(n_ops=3)
        with self.assertLogs("src.vault.binding.lifecycle", level="INFO") as cm:
            disconnect_binding(self.store, bid, force=True)

        audit = [
            ln for ln in cm.output
            if "binding_disconnect_dropping_op" in ln
            and "binding_disconnect_dropping_op_truncated" not in ln
        ]
        self.assertEqual(len(audit), 3, cm.output)
        for i in range(3):
            self.assertTrue(
                any(f"pending-{i:03d}.txt" in ln for ln in audit),
                f"missing per-op audit line for pending-{i:03d}.txt: {audit}",
            )
        # The summary line still records the full count.
        summary = [ln for ln in cm.output if "binding_disconnected " in ln]
        self.assertEqual(len(summary), 1, cm.output)
        self.assertIn("pending_ops_dropped=3", summary[0])

    def test_audit_log_caps_at_DISCONNECT_AUDIT_LOG_CAP(self) -> None:
        from src.vault.binding.lifecycle import DISCONNECT_AUDIT_LOG_CAP
        n = DISCONNECT_AUDIT_LOG_CAP + 5
        bid = self._make_bound_with_pending(n_ops=n)
        with self.assertLogs("src.vault.binding.lifecycle", level="INFO") as cm:
            disconnect_binding(self.store, bid, force=True)

        audit = [
            ln for ln in cm.output
            if "binding_disconnect_dropping_op " in ln
        ]
        truncated = [
            ln for ln in cm.output
            if "binding_disconnect_dropping_op_truncated" in ln
        ]
        self.assertEqual(len(audit), DISCONNECT_AUDIT_LOG_CAP, cm.output)
        self.assertEqual(len(truncated), 1, cm.output)
        # Summary line still has the full count.
        summary = [ln for ln in cm.output if "binding_disconnected " in ln]
        self.assertIn(f"pending_ops_dropped={n}", summary[0])


if __name__ == "__main__":
    unittest.main()
