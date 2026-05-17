"""Review §3.C2 / §3.C3 — watcher runtime + scan-level safety regressions.

Covers two distinct findings against the binding sync engine:

- §3.C2: ``VaultWatcherRuntime._on_tripped`` routes the pause through
  the shared :class:`BindingCancellationRegistry` so an in-flight
  backup-only / two-way cycle observes the bail signal at its next
  checkpoint instead of bleeding tombstones to completion.
- §3.C3: ``scan.scan_for_local_changes`` lstat's the leaf (rather
  than stat-following symlinks) and does not enqueue an upload op
  for a symlink pointing at material outside the binding root.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.binding.bindings import VaultBindingsStore  # noqa: E402
from src.vault.binding.lifecycle import BindingCancellationRegistry  # noqa: E402
from src.vault.binding.runtime_watchers import VaultWatcherRuntime  # noqa: E402
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402


VAULT_ID = "AAAAAAAAAAAA"
DOCS_ID = "rf_v1_" + "a" * 24


class WatcherRuntimeCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_watcher_runtime_test_"))
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

    def _make_bound_binding(self) -> str:
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound", sync_mode="two-way",
            last_synced_revision=1,
        )
        return binding.binding_id

    def test_on_tripped_cancels_inflight_cycle_then_pauses(self) -> None:
        """A detector trip must cancel the in-flight cycle BEFORE the
        DB state flip — so the cycle's should_continue picks up the
        bail signal at its next checkpoint, instead of running the
        already-queued op batch (~50 tombstones) to completion.
        """
        binding_id = self._make_bound_binding()
        registry = BindingCancellationRegistry()
        runtime = VaultWatcherRuntime(
            vault_id=VAULT_ID,
            store=self.store,
            cancellation_registry=registry,
        )
        # Pretend the watcher started a binding runtime — the field
        # only exists so the trip path can be idempotent.
        runtime.bindings[binding_id] = _BindingStub(binding_id)

        # Simulate an in-flight sync cycle: register, derive
        # should_continue, then call _on_tripped from another thread
        # (mimicking the watcher observer's callback path).
        cancel_event = registry.register(binding_id)
        should_continue = lambda: not cancel_event.is_set()
        self.assertTrue(should_continue(), "cycle starts un-cancelled")

        runtime._on_tripped(binding_id)

        # Cycle's should_continue must observe the bail signal.
        self.assertFalse(
            should_continue(),
            "_on_tripped must cancel the in-flight cycle event",
        )
        # Binding row flipped to paused.
        binding = self.store.get_binding(binding_id)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.state, "paused")
        # paused_for_ransomware latched so subsequent trips no-op.
        self.assertTrue(runtime.bindings[binding_id].paused_for_ransomware)

    def test_on_tripped_idempotent_when_already_paused(self) -> None:
        """A second trip on an already-paused binding is a no-op (does
        not re-pause, does not re-cancel)."""
        binding_id = self._make_bound_binding()
        registry = BindingCancellationRegistry()
        runtime = VaultWatcherRuntime(
            vault_id=VAULT_ID,
            store=self.store,
            cancellation_registry=registry,
        )
        runtime.bindings[binding_id] = _BindingStub(binding_id)

        # First trip.
        registry.register(binding_id)
        runtime._on_tripped(binding_id)

        # Re-register a fresh event for a hypothetical follow-up cycle.
        cancel_event = registry.register(binding_id)
        runtime._on_tripped(binding_id)
        # Latched paused_for_ransomware short-circuits before the
        # cancel/pause path runs.
        self.assertFalse(
            cancel_event.is_set(),
            "follow-up trip on already-paused binding must not re-cancel",
        )


class ScanSymlinkSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_scan_symlink_test_"))
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

    def test_scan_skips_symlinks_does_not_enqueue_upload(self) -> None:
        """Review §3.C3: a symlink dropped into the binding root must
        NOT be enqueued for upload. Pre-fix the symlink's target
        contents were uploaded under the symlink's binding-relative
        name — an exfiltration vector via a Documents-bound vault.
        """
        from src.vault.binding.scan import scan_for_local_changes

        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound", sync_mode="backup-only",
        )
        bound = self.store.get_binding(binding.binding_id)

        # Drop a real file (must be enqueued) and a symlink to outside
        # the binding root (must be skipped + log a special_file_skipped
        # event).
        (self.local_root / "real.txt").write_bytes(b"x" * 32)
        outside = self.tmpdir / "outside_secret"
        outside.write_bytes(b"sensitive")
        os.symlink(outside, self.local_root / "link.txt")

        enqueued = scan_for_local_changes(store=self.store, binding=bound)
        self.assertEqual(enqueued, 1, "only the real file should be enqueued")

        ops = self.store.list_pending_ops(bound.binding_id)
        op_paths = sorted(o.relative_path for o in ops)
        self.assertEqual(op_paths, ["real.txt"])


class _BindingStub:
    """Minimal stand-in for ``_BindingRuntime`` — _on_tripped only
    reads .paused_for_ransomware and never invokes the coordinator
    / detector / observer_handle fields."""

    def __init__(self, binding_id: str) -> None:
        self.binding_id = binding_id
        self.coordinator = None
        self.detector = None
        self.observer_handle = None
        self.paused_for_ransomware = False


if __name__ == "__main__":
    unittest.main()
