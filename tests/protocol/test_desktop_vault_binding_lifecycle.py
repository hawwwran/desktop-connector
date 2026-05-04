"""T12.4 — Pause / Resume per binding."""

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

from src.vault_binding_lifecycle import (  # noqa: E402
    PauseResult, ResumeResult,
    pause_binding, resume_binding,
)
from src.vault_bindings import VaultBindingsStore  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402


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
        from src.vault_binding_twoway import run_two_way_cycle

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
        from src.vault_binding_sync import run_backup_only_cycle

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


# ---------------------------------------------------------------------------
# Cheap fakes — only needed to satisfy the cycle's signature; the cycle
# raises before it touches any of the fake's methods.
# ---------------------------------------------------------------------------


class _FakeVault:
    vault_id = VAULT_ID
    master_key = b"\x00" * 32
    vault_access_secret = "vault-secret"

    def fetch_manifest(self, relay, *, local_index=None):
        return {"revision": 0, "remote_folders": []}

    def publish_manifest(self, relay, manifest, *, local_index=None):
        return manifest


class _FakeRelay:
    def get_manifest(self, vault_id, vault_access_secret):
        return {"manifest_revision": 0, "manifest_ciphertext": b"", "manifest_hash": ""}


if __name__ == "__main__":
    unittest.main()
