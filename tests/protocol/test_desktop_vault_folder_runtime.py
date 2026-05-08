"""F-518 — VaultRuntime tests.

The runtime is the GTK-free seam the Folders tab dispatches through.
These tests verify:

- Each op acquires the lock around the vault-open scope.
- ``vault.close()`` runs on every exit path (success, op raises,
  open raises).
- The lock is released on every exit path so a future op isn't
  deadlocked behind a half-cleaned-up failure.
- The op kwargs match the Vault method signatures.
- The runtime reloads ``config`` so wizard-subprocess writes show
  up across boundaries.
- ``flush_and_sync_binding`` looks up the binding row inside the
  same vault-open scope (so a vanished row raises crisply rather
  than silently no-oping).

Behavioural — uses fakes for the opener + relay factory so the test
doesn't bring up real crypto / keyring / relay.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_folder_runtime import VaultRuntime  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"
AUTHOR = "device-author"


class FakeVault:
    """Records the calls the runtime makes against a vault."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.fetch_result: dict = {"remote_folders": []}
        self.add_result: dict = {"after_add": True}
        self.rename_result: dict = {"after_rename": True}

    def fetch_manifest(self, relay, *, local_index):
        self.calls.append(
            ("fetch_manifest", {"relay": relay, "local_index": local_index}),
        )
        return self.fetch_result

    def add_remote_folder(
        self, relay, *, display_name, ignore_patterns, author_device_id,
        local_index,
    ):
        self.calls.append((
            "add_remote_folder",
            {
                "relay": relay,
                "display_name": display_name,
                "ignore_patterns": ignore_patterns,
                "author_device_id": author_device_id,
                "local_index": local_index,
            },
        ))
        return self.add_result

    def rename_remote_folder(
        self, relay, *, remote_folder_id, new_display_name,
        author_device_id, local_index,
    ):
        self.calls.append((
            "rename_remote_folder",
            {
                "relay": relay,
                "remote_folder_id": remote_folder_id,
                "new_display_name": new_display_name,
                "author_device_id": author_device_id,
                "local_index": local_index,
            },
        ))
        return self.rename_result

    def close(self) -> None:
        self.closed = True


class FakeRelay:
    pass


class FakeConfig:
    def __init__(self) -> None:
        self.reload_count = 0

    def reload(self) -> None:
        self.reload_count += 1


class _RuntimeHarness:
    """Bundle a runtime with its fakes for ergonomic use in tests."""

    def __init__(
        self,
        *,
        opener_raises: BaseException | None = None,
        op_raises: BaseException | None = None,
    ) -> None:
        self.config_dir = Path("/tmp/test-not-touched")
        self.config = FakeConfig()
        self.local_index = MagicMock(name="LocalIndex")
        self.vault = FakeVault()
        self.relay = FakeRelay()
        self.opener_calls: list[tuple[Path, FakeConfig, str]] = []

        if opener_raises is not None:
            def opener(_cd, _cfg, _vid):
                raise opener_raises
        else:
            def opener(cd, cfg, vid):
                self.opener_calls.append((cd, cfg, vid))
                return self.vault

        if op_raises is not None:
            # Inject failure on whatever Vault method gets called next.
            for name in ("add_remote_folder", "rename_remote_folder",
                         "fetch_manifest"):
                def _raises(*_a, **_kw):
                    raise op_raises
                setattr(self.vault, name, _raises)

        self.runtime = VaultRuntime(
            config_dir=self.config_dir,
            config=self.config,
            vault_id=VAULT_ID,
            local_index=self.local_index,
            opener=opener,
            relay_factory=lambda _cfg: self.relay,
        )


# ------------------------------------------------------------------ unit tests


class VaultRuntimeOpenSerializedTests(unittest.TestCase):
    """Lock + open + close lifecycle, isolated from any vault op."""

    def test_lock_and_open_close_in_order(self) -> None:
        h = _RuntimeHarness()
        with h.runtime._open_serialized() as vault:
            self.assertIs(vault, h.vault)
            self.assertFalse(vault.closed)
        self.assertTrue(h.vault.closed)

    def test_config_reloaded_on_each_open(self) -> None:
        h = _RuntimeHarness()
        with h.runtime._open_serialized():
            pass
        with h.runtime._open_serialized():
            pass
        self.assertEqual(h.config.reload_count, 2)

    def test_opener_failure_releases_lock(self) -> None:
        h = _RuntimeHarness(opener_raises=RuntimeError("grant load failed"))
        with self.assertRaises(RuntimeError):
            with h.runtime._open_serialized():
                self.fail("should not reach the body")
        # If the lock leaked, the next acquire would deadlock — bound
        # the assertion with a timeout so a regression fails the test
        # instead of hanging the suite.
        self.assertTrue(h.runtime._lock.acquire(timeout=1.0))
        h.runtime._lock.release()

    def test_op_failure_still_closes_vault_and_releases_lock(self) -> None:
        h = _RuntimeHarness()
        with self.assertRaises(ValueError):
            with h.runtime._open_serialized() as vault:
                self.assertIs(vault, h.vault)
                raise ValueError("op blew up")
        self.assertTrue(h.vault.closed)
        self.assertTrue(h.runtime._lock.acquire(timeout=1.0))
        h.runtime._lock.release()

    def test_serialization_excludes_concurrent_workers(self) -> None:
        # Two threads racing into the runtime must not see overlapping
        # vault opens — that's the whole point of F-517's lock.
        h = _RuntimeHarness()
        in_scope = []
        gate = threading.Event()

        def worker(label: str) -> None:
            with h.runtime._open_serialized():
                in_scope.append(label)
                gate.wait(timeout=0.5)
                in_scope.append(f"{label}-out")

        t1 = threading.Thread(target=worker, args=("A",), daemon=True)
        t1.start()
        # Give t1 time to enter the lock.
        time.sleep(0.05)
        t2 = threading.Thread(target=worker, args=("B",), daemon=True)
        t2.start()
        time.sleep(0.05)
        # Only A is inside; B is blocked on the lock.
        self.assertEqual(in_scope, ["A"])
        gate.set()
        t1.join(timeout=1.0)
        t2.join(timeout=1.0)
        # A entered → A exited → B entered → B exited. Order is
        # deterministic because the lock serializes the body.
        self.assertEqual(in_scope, ["A", "A-out", "B", "B-out"])


class VaultRuntimeFetchManifestTests(unittest.TestCase):
    def test_fetch_manifest_calls_through_with_local_index(self) -> None:
        h = _RuntimeHarness()
        result = h.runtime.fetch_manifest()
        self.assertIs(result, h.vault.fetch_result)
        [(name, kwargs)] = h.vault.calls
        self.assertEqual(name, "fetch_manifest")
        self.assertIs(kwargs["relay"], h.relay)
        self.assertIs(kwargs["local_index"], h.local_index)
        self.assertTrue(h.vault.closed)


class VaultRuntimeAddFolderTests(unittest.TestCase):
    def test_add_remote_folder_passes_kwargs_through(self) -> None:
        h = _RuntimeHarness()
        result = h.runtime.add_remote_folder(
            display_name="Documents",
            ignore_patterns=["*.tmp"],
            author_device_id=AUTHOR,
        )
        self.assertIs(result, h.vault.add_result)
        [(name, kwargs)] = h.vault.calls
        self.assertEqual(name, "add_remote_folder")
        self.assertEqual(kwargs["display_name"], "Documents")
        self.assertEqual(kwargs["ignore_patterns"], ["*.tmp"])
        self.assertEqual(kwargs["author_device_id"], AUTHOR)
        self.assertIs(kwargs["local_index"], h.local_index)
        self.assertIs(kwargs["relay"], h.relay)
        self.assertTrue(h.vault.closed)

    def test_op_exception_propagates_after_close(self) -> None:
        h = _RuntimeHarness(op_raises=RuntimeError("boom"))
        with self.assertRaisesRegex(RuntimeError, "boom"):
            h.runtime.add_remote_folder(
                display_name="Documents",
                ignore_patterns=[],
                author_device_id=AUTHOR,
            )
        self.assertTrue(h.vault.closed)


class VaultRuntimeRenameFolderTests(unittest.TestCase):
    def test_rename_remote_folder_passes_kwargs_through(self) -> None:
        h = _RuntimeHarness()
        result = h.runtime.rename_remote_folder(
            remote_folder_id="rf-1",
            new_display_name="Renamed",
            author_device_id=AUTHOR,
        )
        self.assertIs(result, h.vault.rename_result)
        [(name, kwargs)] = h.vault.calls
        self.assertEqual(name, "rename_remote_folder")
        self.assertEqual(kwargs["remote_folder_id"], "rf-1")
        self.assertEqual(kwargs["new_display_name"], "Renamed")
        self.assertEqual(kwargs["author_device_id"], AUTHOR)
        self.assertIs(kwargs["local_index"], h.local_index)
        self.assertTrue(h.vault.closed)


class VaultRuntimeFlushAndSyncBindingTests(unittest.TestCase):
    """The flush op needs a real ``VaultBindingsStore`` so the runtime
    can look up the binding row inside its locked vault scope. Use a
    real store on a temp sqlite path.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vault_runtime_flush_"))
        # The runtime needs ``local_index.db_path`` to point at a real
        # sqlite path (or any unique file). Fake an index that just
        # exposes that property; the underlying VaultBindingsStore
        # builds its schema lazily.
        self.db_path = self.tmp / "bindings.db"
        self.local_index = MagicMock()
        self.local_index.db_path = self.db_path

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_binding_raises_runtime_error(self) -> None:
        # The runtime must raise (not silently no-op) when the binding
        # row is gone; the tab surfaces the error to the user.
        # Materialize the bindings schema via the real VaultLocalIndex
        # so the runtime's row lookup doesn't fail on "no such table".
        # Schema is owned by VaultLocalIndex (not by the store), so
        # construction here is what creates the tables.
        from src.vault_local_index import VaultLocalIndex
        index = VaultLocalIndex(self.tmp)
        h = _RuntimeHarness()
        h.runtime._local_index = index
        with self.assertRaisesRegex(RuntimeError, "binding not found"):
            h.runtime.flush_and_sync_binding(
                binding_id="ghost",
                author_device_id=AUTHOR,
                device_name="laptop",
                should_continue=lambda: True,
            )
        # Vault was opened and closed despite the missing binding.
        self.assertTrue(h.vault.closed)


class VaultRuntimeSourcePins(unittest.TestCase):
    """F-518 source pins — keep the runtime's structural shape from
    drifting back into the tab's worker bodies.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # Post-#6: the tab is a ``vault_folders/`` package; concatenate
        # the submodules so existing pinned substrings keep matching.
        package_dir = Path(REPO_ROOT, "desktop/src/vault_folders")
        cls.tab_source = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted(package_dir.glob("*.py"))
        )
        cls.runtime_source = Path(
            REPO_ROOT, "desktop/src/vault_folder_runtime.py",
        ).read_text(encoding="utf-8")

    def test_tab_imports_runtime(self) -> None:
        # Post-#6: package siblings reach up to ``..vault_folder_runtime``.
        self.assertIn(
            "from ..vault_folder_runtime import VaultRuntime",
            self.tab_source,
        )

    def test_tab_constructs_runtime_once(self) -> None:
        # Single construction at the top of the builder; per-worker
        # construction would defeat the per-tab lock.
        self.assertEqual(
            self.tab_source.count("runtime = VaultRuntime("),
            1,
        )

    def test_tab_does_not_open_vault_directly(self) -> None:
        # The tab should never reach into open_local_vault_from_grant
        # or create_vault_relay — the runtime owns both.
        self.assertNotIn(
            "open_local_vault_from_grant(",
            self.tab_source,
        )
        self.assertNotIn(
            "create_vault_relay(config)",
            self.tab_source,
        )

    def test_tab_does_not_call_raw_vault_methods(self) -> None:
        # All Vault.<method> calls were moved into the runtime.
        for needle in (
            "vault.add_remote_folder(",
            "vault.rename_remote_folder(",
            "vault.fetch_manifest(",
        ):
            self.assertNotIn(
                needle, self.tab_source,
                msg=f"tab regressed to a direct Vault call: {needle!r}",
            )

    def test_tab_does_not_define_its_own_open_vault_serialized(self) -> None:
        self.assertNotIn(
            "_open_vault_serialized", self.tab_source,
        )
        self.assertNotIn(
            "_vault_lock = threading.Lock()", self.tab_source,
        )

    def test_tab_dispatches_through_runtime_methods(self) -> None:
        # F-LT12: rename folded into a single Configure dialog that
        # dispatches through ``update_remote_folder_settings`` (which
        # can carry a name change AND ignore-pattern edits in one CAS).
        # The legacy ``rename_remote_folder`` runtime method still
        # exists for programmatic callers but the tab no longer invokes
        # it directly — see ``test_desktop_vault_folders_rename_source``
        # for the Configure-dialog pin.
        for needle in (
            "runtime.fetch_manifest()",
            "runtime.add_remote_folder(",
            "runtime.update_remote_folder_settings(",
            "runtime.flush_and_sync_binding(",
            "runtime.run_initial_baseline(",
        ):
            self.assertIn(
                needle, self.tab_source,
                msg=f"tab missing runtime call: {needle!r}",
            )

    def test_runtime_opens_with_open_local_vault_from_grant(self) -> None:
        # The runtime's default opener delegates to the production helper.
        self.assertIn(
            "open_local_vault_from_grant",
            self.runtime_source,
        )

    def test_runtime_uses_create_vault_relay_by_default(self) -> None:
        self.assertIn(
            "create_vault_relay",
            self.runtime_source,
        )


if __name__ == "__main__":
    unittest.main()
