"""F-Y15 — pure-function tests for the Folders-tab lifecycle dispatchers.

The GTK builder in ``vault_folders_tab.py`` delegates click handlers to
``vault_folder_actions.dispatch_{pause,resume,disconnect}``. Those
dispatchers are GTK-free and synchronous, so they unit-test cleanly
against a real :class:`VaultBindingsStore` + the lifecycle helpers.

Coverage:

- pause path forwards the cancellation registry and returns a toast
- resume path runs an optional flush closure and surfaces its toast
- disconnect path requires ``confirm()`` and respects the user gate
- error paths are humanized into the second tuple slot
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

from src.vault_binding_lifecycle import BindingCancellationRegistry  # noqa: E402
from src.vault_bindings import VaultBindingsStore  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_folder_actions import (  # noqa: E402
    dispatch_disconnect,
    dispatch_pause,
    dispatch_resume,
)


VAULT_ID = "ABCD2345WXYZ"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"


class _ActionsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_folder_actions_"))
        self.config_dir = self.tmpdir / "config"
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.index = VaultLocalIndex(self.config_dir)
        self.store = VaultBindingsStore(self.index.db_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _bound(self, *, sync_mode: str = "backup-only") -> str:
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound",
            sync_mode=sync_mode, last_synced_revision=1,
        )
        return binding.binding_id


class PauseDispatcherTests(_ActionsTestBase):
    def test_pause_returns_toast_and_flips_state(self) -> None:
        binding_id = self._bound()
        toast, err = dispatch_pause(
            store=self.store, binding_id=binding_id,
        )
        self.assertIsNone(err)
        self.assertIn("Paused", toast or "")
        self.assertEqual(self.store.get_binding(binding_id).state, "paused")

    def test_pause_cancels_inflight_cycle_via_registry(self) -> None:
        binding_id = self._bound()
        registry = BindingCancellationRegistry()
        event = registry.register(binding_id)
        self.assertFalse(event.is_set())

        toast, err = dispatch_pause(
            store=self.store, binding_id=binding_id, cancellation=registry,
        )
        self.assertIsNone(err)
        self.assertIsNotNone(toast)
        self.assertTrue(event.is_set())

    def test_pause_pending_ops_count_in_toast(self) -> None:
        binding_id = self._bound()
        for path in ("a.txt", "b.txt"):
            self.store.coalesce_op(
                binding_id=binding_id, op_type="upload", relative_path=path,
            )
        toast, err = dispatch_pause(store=self.store, binding_id=binding_id)
        self.assertIsNone(err)
        self.assertIn("2 pending op(s)", toast or "")

    def test_pause_unknown_binding_returns_error(self) -> None:
        toast, err = dispatch_pause(
            store=self.store, binding_id="rb_v1_unknown",
        )
        self.assertIsNone(toast)
        self.assertTrue(err and err.startswith("Pause failed"))


class ResumeDispatcherTests(_ActionsTestBase):
    def test_resume_without_flush_returns_toast(self) -> None:
        binding_id = self._bound()
        dispatch_pause(store=self.store, binding_id=binding_id)

        toast, err = dispatch_resume(store=self.store, binding_id=binding_id)
        self.assertIsNone(err)
        self.assertEqual(toast, "Resumed.")
        self.assertEqual(self.store.get_binding(binding_id).state, "bound")

    def test_resume_runs_flush_and_renders_its_toast(self) -> None:
        binding_id = self._bound()
        dispatch_pause(store=self.store, binding_id=binding_id)

        from src.vault_binding_sync import SyncCycleResult
        captured: dict[str, Any] = {}

        def flush(binding: Any) -> SyncCycleResult:
            captured["binding"] = binding
            return SyncCycleResult(
                binding_id=binding.binding_id,
                started_at_revision=1, ended_at_revision=1,
                outcomes=[], cancelled=False,
            )

        toast, err = dispatch_resume(
            store=self.store, binding_id=binding_id, flush=flush,
        )
        self.assertIsNone(err)
        self.assertIn("Resumed.", toast or "")
        self.assertEqual(
            captured.get("binding").binding_id, binding_id,  # type: ignore[union-attr]
        )

    def test_resume_unknown_binding_returns_error(self) -> None:
        toast, err = dispatch_resume(
            store=self.store, binding_id="rb_v1_unknown",
        )
        self.assertIsNone(toast)
        self.assertTrue(err and err.startswith("Resume failed"))


class DisconnectDispatcherTests(_ActionsTestBase):
    def test_disconnect_requires_confirm(self) -> None:
        binding_id = self._bound()

        toast, err = dispatch_disconnect(
            store=self.store, binding_id=binding_id,
            confirm=lambda: False,
        )
        self.assertIsNone(err)
        self.assertEqual(toast, "Disconnect cancelled.")
        self.assertEqual(self.store.get_binding(binding_id).state, "bound")

    def test_disconnect_proceeds_when_confirmed(self) -> None:
        binding_id = self._bound()
        for path in ("a.txt", "b.txt"):
            self.store.coalesce_op(
                binding_id=binding_id, op_type="upload", relative_path=path,
            )

        toast, err = dispatch_disconnect(
            store=self.store, binding_id=binding_id,
            confirm=lambda: True,
        )
        self.assertIsNone(err)
        self.assertIn("Disconnected", toast or "")
        self.assertIn("2 pending op(s) dropped", toast or "")
        self.assertEqual(self.store.get_binding(binding_id).state, "unbound")

    def test_disconnect_cancels_inflight_cycle_via_registry(self) -> None:
        binding_id = self._bound()
        registry = BindingCancellationRegistry()
        event = registry.register(binding_id)

        dispatch_disconnect(
            store=self.store, binding_id=binding_id,
            confirm=lambda: True, cancellation=registry,
        )
        self.assertTrue(event.is_set())

    def test_disconnect_unknown_binding_returns_error(self) -> None:
        toast, err = dispatch_disconnect(
            store=self.store, binding_id="rb_v1_unknown",
            confirm=lambda: True,
        )
        self.assertIsNone(toast)
        self.assertTrue(err and err.startswith("Disconnect failed"))


# Allow Any import without polluting the head of the module.
from typing import Any  # noqa: E402


if __name__ == "__main__":
    unittest.main()
