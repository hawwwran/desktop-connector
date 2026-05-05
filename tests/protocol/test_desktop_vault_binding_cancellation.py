"""F-Y08 — cooperative cancellation across the sync cycle.

Covers:

- ``BindingCancellationRegistry`` register / cancel / clear semantics.
- Chunk-level bail in ``upload_file`` raises ``SyncCancelledError``;
  the upload session is still saved per chunk so a future resume
  picks up.
- Op-level bail in ``run_backup_only_cycle`` and ``run_two_way_cycle``
  stops the queue drain; remaining ops stay enqueued.
- Lifecycle integration: ``pause_binding`` and ``disconnect_binding``
  both flip the registered event so an in-flight cycle observes the
  bail at its next checkpoint.
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

from src.vault import Vault  # noqa: E402
from src.vault_binding_lifecycle import (  # noqa: E402
    BindingCancellationRegistry,
    SyncCancelledError,
    disconnect_binding,
    pause_binding,
)
from src.vault_binding_sync import (  # noqa: E402
    SyncCycleResult,
    run_backup_only_cycle,
)
from src.vault_binding_twoway import run_two_way_cycle  # noqa: E402
from src.vault_bindings import VaultBindingsStore  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_manifest import make_manifest, make_remote_folder  # noqa: E402
from src.vault_upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import FakeUploadRelay  # noqa: E402


VAULT_ACCESS_SECRET = "vault-secret"


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1, manifest_revision=1,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )


class BindingCancellationRegistryTests(unittest.TestCase):
    """Pure unit tests for the registry — no I/O, no fakes."""

    def test_register_returns_clear_event(self) -> None:
        registry = BindingCancellationRegistry()
        event = registry.register("rb_v1_alpha")
        self.assertFalse(event.is_set())
        self.assertTrue(registry.is_registered("rb_v1_alpha"))

    def test_cancel_sets_registered_event(self) -> None:
        registry = BindingCancellationRegistry()
        event = registry.register("rb_v1_alpha")
        self.assertTrue(registry.cancel("rb_v1_alpha"))
        self.assertTrue(event.is_set())

    def test_cancel_unknown_binding_returns_false(self) -> None:
        registry = BindingCancellationRegistry()
        self.assertFalse(registry.cancel("rb_v1_unknown"))

    def test_clear_drops_event_so_next_register_is_fresh(self) -> None:
        registry = BindingCancellationRegistry()
        first = registry.register("rb_v1_alpha")
        registry.cancel("rb_v1_alpha")
        self.assertTrue(first.is_set())

        registry.clear("rb_v1_alpha")
        self.assertFalse(registry.is_registered("rb_v1_alpha"))

        # Re-registering yields a fresh, unset event.
        second = registry.register("rb_v1_alpha")
        self.assertFalse(second.is_set())
        self.assertIsNot(second, first)


class UploadFileCancellationTests(unittest.TestCase):
    """``upload_file`` raises ``SyncCancelledError`` mid-chunk-loop."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_cancel_upload_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seeded_relay(self) -> tuple[FakeUploadRelay, dict]:
        manifest = make_manifest(
            vault_id=VAULT_ID, revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z", author_device_id=AUTHOR,
            remote_folders=[make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            )],
        )
        relay = FakeUploadRelay(manifest=manifest)
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
        finally:
            vault.close()
        return relay, manifest

    def _multichunk_payload(self, chunks: int) -> bytes:
        # 2 MiB per chunk; fill with deterministic bytes so the
        # chunk_id is non-degenerate.
        from src.vault_upload import CHUNK_SIZE
        chunk_bytes = bytes(range(256)) * (CHUNK_SIZE // 256)
        return chunk_bytes * chunks + b"trailer"  # forces a small last chunk

    def test_should_continue_false_raises_sync_cancelled(self) -> None:
        relay, manifest = self._seeded_relay()
        local = self.tmpdir / "big.bin"
        local.write_bytes(self._multichunk_payload(3))

        # Cancel before the very first chunk PUT.
        vault = _vault()
        try:
            with self.assertRaises(SyncCancelledError):
                upload_file(
                    vault=vault, relay=relay, manifest=manifest,
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path="big.bin", author_device_id=AUTHOR,
                    should_continue=lambda: False,
                )
        finally:
            vault.close()

        # Nothing was uploaded.
        self.assertEqual(relay.put_calls, [])

    def test_cancel_after_first_chunk_leaves_partial_progress(self) -> None:
        relay, manifest = self._seeded_relay()
        local = self.tmpdir / "big.bin"
        local.write_bytes(self._multichunk_payload(4))

        # Allow the first chunk through, then bail.
        ticks = {"n": 0}
        def gate() -> bool:
            ticks["n"] += 1
            return ticks["n"] <= 1   # first probe is BEFORE chunk 0

        vault = _vault()
        try:
            with self.assertRaises(SyncCancelledError):
                upload_file(
                    vault=vault, relay=relay, manifest=manifest,
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path="big.bin", author_device_id=AUTHOR,
                    should_continue=gate,
                )
        finally:
            vault.close()

        # Exactly one chunk PUT before the bail; the manifest was NOT
        # published (the version is still absent from the head).
        self.assertEqual(len(relay.put_calls), 1)
        head = relay.current_manifest
        from src.vault_manifest import find_file_entry
        self.assertIsNone(find_file_entry(head, DOCS_ID, "big.bin"))


class BackupOnlyCycleCancellationTests(unittest.TestCase):
    """``run_backup_only_cycle`` stops at the next op when cancelled."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_cancel_cycle_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.config_dir = self.tmpdir / "config"
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.index = VaultLocalIndex(self.config_dir)
        self.store = VaultBindingsStore(self.index.db_path)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _empty_remote(self) -> tuple[FakeUploadRelay, dict]:
        manifest = make_manifest(
            vault_id=VAULT_ID, revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z", author_device_id=AUTHOR,
            remote_folders=[make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            )],
        )
        relay = FakeUploadRelay(manifest=manifest)
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
        finally:
            vault.close()
        return relay, manifest

    def _make_bound_binding(self, last_revision: int):
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound",
            last_synced_revision=last_revision,
        )
        return self.store.get_binding(binding.binding_id)

    def test_should_continue_false_before_first_op_processes_nothing(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(int(manifest["revision"]))
        (self.local_root / "alpha.txt").write_bytes(b"a")
        (self.local_root / "beta.txt").write_bytes(b"b")
        for path in ("alpha.txt", "beta.txt"):
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload", relative_path=path,
            )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=AUTHOR,
                should_continue=lambda: False,
            )
        finally:
            vault.close()

        self.assertTrue(result.cancelled)
        self.assertEqual(result.outcomes, [])
        # Both ops still queued.
        self.assertEqual(len(self.store.list_pending_ops(binding.binding_id)), 2)

    def test_cancel_after_first_op_leaves_remaining_in_queue(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(int(manifest["revision"]))
        for path in ("alpha.txt", "beta.txt", "gamma.txt"):
            (self.local_root / path).write_bytes(path.encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload", relative_path=path,
            )

        ticks = {"n": 0}
        def gate() -> bool:
            ticks["n"] += 1
            # The driver checks before each op + the chunk loop in
            # upload_file may also probe. Allow the first op to fully
            # complete (tiny single-chunk file → ~3 probes), then bail.
            return ticks["n"] <= 6

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=AUTHOR,
                should_continue=gate,
            )
        finally:
            vault.close()

        self.assertTrue(result.cancelled)
        # At least one but fewer than three ops finished — proves the
        # bail interrupted mid-queue.
        finished = sum(
            1 for o in result.outcomes
            if o.status in ("uploaded", "skipped")
        )
        self.assertGreaterEqual(finished, 1)
        self.assertLess(finished, 3)
        remaining = len(self.store.list_pending_ops(binding.binding_id))
        self.assertEqual(remaining, 3 - finished)

    def test_default_no_cancellation_runs_full_queue(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(int(manifest["revision"]))
        (self.local_root / "alpha.txt").write_bytes(b"a")
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload", relative_path="alpha.txt",
        )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=AUTHOR,
                # no should_continue → infinite True
            )
        finally:
            vault.close()

        self.assertFalse(result.cancelled)
        self.assertEqual(result.succeeded_count, 1)


class TwoWayCycleCancellationTests(unittest.TestCase):
    """Cancel between Phase A and Phase B in ``run_two_way_cycle``."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_cancel_twoway_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.config_dir = self.tmpdir / "config"
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.index = VaultLocalIndex(self.config_dir)
        self.store = VaultBindingsStore(self.index.db_path)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _empty_remote(self) -> tuple[FakeUploadRelay, dict]:
        manifest = make_manifest(
            vault_id=VAULT_ID, revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z", author_device_id=AUTHOR,
            remote_folders=[make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            )],
        )
        relay = FakeUploadRelay(manifest=manifest)
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
        finally:
            vault.close()
        return relay, manifest

    def _make_two_way_binding(self, last_revision: int):
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound",
            sync_mode="two-way",
            last_synced_revision=last_revision,
        )
        return self.store.get_binding(binding.binding_id)

    def test_pre_iteration_cancel_emits_cancelled_result(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_two_way_binding(int(manifest["revision"]))

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=AUTHOR,
                device_name="this device",
                should_continue=lambda: False,
            )
        finally:
            vault.close()

        self.assertTrue(result.cancelled)
        self.assertEqual(result.outcomes, [])


class LifecycleRegistryIntegrationTests(unittest.TestCase):
    """Lifecycle helpers + registry coordinate to abort an in-flight cycle."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_cancel_lifecycle_"))
        self.config_dir = self.tmpdir / "config"
        self.index = VaultLocalIndex(self.config_dir)
        self.store = VaultBindingsStore(self.index.db_path)
        self.local_root = self.tmpdir / "binding"
        self.local_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _bound_binding(self):
        binding = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound",
            sync_mode="backup-only",
            last_synced_revision=1,
        )
        return self.store.get_binding(binding.binding_id)

    def test_pause_with_registry_signals_inflight_cycle(self) -> None:
        binding = self._bound_binding()
        registry = BindingCancellationRegistry()
        event = registry.register(binding.binding_id)

        # Caller derives should_continue from the event; before pause
        # it must report True.
        self.assertTrue(not event.is_set())
        self.assertTrue((lambda: not event.is_set())())

        result = pause_binding(
            self.store, binding.binding_id, cancellation=registry,
        )

        # State transition lands AND the in-flight gate now reports False.
        self.assertEqual(result.binding.state, "paused")
        self.assertTrue(event.is_set())
        self.assertFalse((lambda: not event.is_set())())

    def test_disconnect_with_registry_signals_inflight_cycle(self) -> None:
        binding = self._bound_binding()
        registry = BindingCancellationRegistry()
        event = registry.register(binding.binding_id)

        disconnect_binding(
            self.store, binding.binding_id, cancellation=registry,
        )

        rebound = self.store.get_binding(binding.binding_id)
        self.assertEqual(rebound.state, "unbound")
        self.assertTrue(event.is_set())

    def test_pause_no_registry_does_not_raise(self) -> None:
        # Backwards-compatible: registry is optional.
        binding = self._bound_binding()
        result = pause_binding(self.store, binding.binding_id)
        self.assertEqual(result.binding.state, "paused")


if __name__ == "__main__":
    unittest.main()
