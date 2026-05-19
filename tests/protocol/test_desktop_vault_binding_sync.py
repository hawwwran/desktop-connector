"""T10.5 — Backup-only sync loop: pending ops → upload + tombstone publish."""

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

from src.vault import Vault  # noqa: E402
from src.vault.binding.sync import (  # noqa: E402
    SyncCycleResult,
    SyncOpOutcome,
    flush_and_sync_binding,
    format_sync_outcome_toast,
    run_backup_only_cycle,
)
from src.vault.binding.bindings import VaultBindingsStore, VaultLocalEntry  # noqa: E402
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)
from src.vault.upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import (  # noqa: E402
    FakeUploadRelay,
    seed_sharded_state,
)


VAULT_ACCESS_SECRET = "vault-secret"
OTHER_DEVICE = "f1e2d3c4b5a6918273645566778899aa"


class BackupOnlySyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_sync_test_"))
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

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    def _empty_remote(self) -> tuple[FakeUploadRelay, dict]:
        manifest = _empty_unified(created_at="2026-05-04T12:00:00.000Z")
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
        finally:
            vault.close()
        return relay, manifest

    def _seed_remote_file(
        self,
        relay: FakeUploadRelay,
        manifest: dict,
        *,
        path: str,
        content: bytes,
    ) -> dict:
        local = self.tmpdir / "seed" / path.replace("/", "_")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        vault = _vault()
        try:
            res = upload_file(
                vault=vault, relay=relay, manifest=manifest,
                local_path=local, remote_folder_id=DOCS_ID,
                remote_path=path, author_device_id=AUTHOR,
            )
        finally:
            vault.close()
        return assemble_unified_manifest(res.root, {res.remote_folder_id: res.shard})

    def _make_bound_binding(self, *, last_revision: int) -> "VaultBinding":
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id,
            state="bound",
            last_synced_revision=last_revision,
        )
        return self.store.get_binding(binding.binding_id)

    # ------------------------------------------------------------------
    # Acceptance: new local file → remote within one cycle
    # ------------------------------------------------------------------

    def test_upload_op_drains_to_remote_and_advances_last_synced_revision(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        # Local file appears under the binding.
        payload = b"new local file written by the user"
        (self.local_root / "alpha.txt").write_bytes(payload)
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="alpha.txt",
        )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 1)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.outcomes[0].status, "uploaded")

        # Remote head advanced.
        self.assertGreater(result.ended_at_revision, result.started_at_revision)
        rebound = self.store.get_binding(binding.binding_id)
        self.assertEqual(rebound.last_synced_revision, result.ended_at_revision)

        # Pending op is gone, local entry row is now stamped + has fingerprint.
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])
        entry = self.store.get_local_entry(binding.binding_id, "alpha.txt")
        self.assertIsNotNone(entry)
        self.assertNotEqual(entry.content_fingerprint, "")
        self.assertEqual(entry.size_bytes, len(payload))
        self.assertEqual(entry.last_synced_revision, result.ended_at_revision)

    # ------------------------------------------------------------------
    # Acceptance: remote-only changes don't appear locally
    # ------------------------------------------------------------------

    def test_remote_only_change_does_not_alter_local_files(self) -> None:
        relay, manifest = self._empty_remote()
        # Another device uploads remote-only.txt to the remote folder.
        manifest = self._seed_remote_file(
            relay, manifest, path="remote-only.txt", content=b"remote bytes",
        )
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        # No pending ops on our side; one cycle just refreshes the revision.
        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        # Backup-only must NOT materialize the remote-only file.
        self.assertFalse((self.local_root / "remote-only.txt").exists())
        self.assertEqual(result.outcomes, [])
        # Revision matches the server's current root revision (advanced by
        # the sharded seed publishes; the cycle records root_revision as
        # the binding's last_synced_revision).
        rebound = self.store.get_binding(binding.binding_id)
        self.assertEqual(
            rebound.last_synced_revision, int(relay.root_revision),
        )

    # ------------------------------------------------------------------
    # Acceptance: local delete tombstones the remote entry
    # ------------------------------------------------------------------

    def test_delete_op_tombstones_remote_and_clears_local_entry(self) -> None:
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="goner.txt", content=b"will be deleted",
        )
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))
        # Pretend baseline seeded the row.
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path="goner.txt",
            content_fingerprint="abc",
            size_bytes=15, mtime_ns=1_000_000_000,
            last_synced_revision=int(manifest["revision"]),
        ))
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="delete",
            relative_path="goner.txt",
        )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 1)
        self.assertEqual(result.outcomes[0].status, "deleted")
        # Local entry row + queue row both cleared.
        self.assertIsNone(self.store.get_local_entry(binding.binding_id, "goner.txt"))
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])

        # Remote shard now has the entry tombstoned.
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        target = next(e for e in shard["entries"] if e["path"] == "goner.txt")
        self.assertTrue(bool(target["deleted"]))

    # ------------------------------------------------------------------
    # Vanished local file before sync runs → upload op promoted to delete
    # ------------------------------------------------------------------

    def test_upload_op_for_missing_file_is_promoted_to_delete(self) -> None:
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="ghost.txt", content=b"will vanish",
        )
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path="ghost.txt",
            content_fingerprint="xyz",
            size_bytes=11, mtime_ns=2_000_000_000,
            last_synced_revision=int(manifest["revision"]),
        ))
        # Watcher saw a "modified" event but the file is now gone (atomic
        # rename overwrite).
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="ghost.txt",
        )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 1)
        self.assertEqual(result.outcomes[0].op_type, "upload")
        self.assertEqual(result.outcomes[0].status, "deleted")

    # ------------------------------------------------------------------
    # Idempotent re-upload = zero new chunks (relies on T6.1 fingerprint shortcut)
    # ------------------------------------------------------------------

    def test_re_uploading_identical_bytes_is_skipped(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        payload = b"same bytes as last cycle"
        (self.local_root / "stable.txt").write_bytes(payload)
        self.store.coalesce_op(
            binding_id=binding.binding_id, op_type="upload",
            relative_path="stable.txt",
        )

        vault = _vault()
        try:
            run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        # Re-enqueue the same path with the same bytes — fingerprint short-
        # circuit means no new chunks PUT.
        before = len(relay.put_calls)
        self.store.coalesce_op(
            binding_id=binding.binding_id, op_type="upload",
            relative_path="stable.txt",
        )
        binding = self.store.get_binding(binding.binding_id)
        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        self.assertEqual(len(relay.put_calls), before)
        self.assertEqual(result.outcomes[0].status, "skipped")

    # ------------------------------------------------------------------
    # Validation: paused / not-bound bindings refuse
    # ------------------------------------------------------------------

    def test_paused_binding_raises(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))
        self.store.update_binding_state(binding.binding_id, sync_mode="paused")
        binding = self.store.get_binding(binding.binding_id)
        vault = _vault()
        try:
            with self.assertRaises(ValueError):
                run_backup_only_cycle(
                    vault=vault, relay=relay,
                    store=self.store, binding=binding,
                    author_device_id=OTHER_DEVICE,
                )
        finally:
            vault.close()

    def test_needs_preflight_binding_raises(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        # Still in "needs-preflight" — sync must refuse.
        vault = _vault()
        try:
            with self.assertRaises(ValueError):
                run_backup_only_cycle(
                    vault=vault, relay=relay,
                    store=self.store, binding=binding,
                    author_device_id=OTHER_DEVICE,
                )
        finally:
            vault.close()

    # ------------------------------------------------------------------
    # Failure path: a failing upload leaves the op in queue with attempts++
    # ------------------------------------------------------------------

    def test_failed_upload_leaves_op_in_queue_with_error_recorded(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        # File gets written and registered, then we wedge the relay so PUT
        # raises a non-CAS error.
        (self.local_root / "boom.txt").write_bytes(b"x" * 64)
        self.store.coalesce_op(
            binding_id=binding.binding_id, op_type="upload",
            relative_path="boom.txt",
        )

        original_put = relay.put_chunk
        def _explode(*a, **kw):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("network down")
        relay.put_chunk = _explode  # type: ignore[assignment]

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()
            relay.put_chunk = original_put  # type: ignore[assignment]

        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.outcomes[0].status, "failed")
        # Op survived for retry.
        ops = self.store.list_pending_ops(binding.binding_id)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].attempts, 1)
        self.assertIsNotNone(ops[0].last_error)


class FetchManifestPerOpTests(unittest.TestCase):
    """SO-2: a backup-only / two-way cycle must not re-fetch the manifest
    after every successful op.

    Pre-SO-2 each successful op was followed by ``vault.fetch_manifest``,
    which on a 10k-file initial bind ships the full encrypted manifest
    envelope ~once per file = O(N²) bytes total (see
    ``docs/plans/vault-large-folder-perf.md``). The fix is to thread the
    manifest dict ``publish_manifest`` already returned through the
    cycle. The re-fetch stays on the failure path because a CAS conflict
    legitimately means "the world moved, refresh."
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_sync_so2_"))
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

    def _empty_remote(self) -> "CountingRelay":
        manifest = _empty_unified(created_at="2026-05-16T12:00:00.000Z")
        relay = CountingRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
        finally:
            vault.close()
        # Discard any get_root calls from setup so the assertions
        # below count *cycle-driven* fetches only.
        relay.state_fetch_count = 0
        return relay

    def _make_bound_binding(self, *, last_revision: int) -> "VaultBinding":
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id,
            state="bound",
            last_synced_revision=last_revision,
        )
        return self.store.get_binding(binding.binding_id)

    def test_backup_only_cycle_does_not_fetch_per_successful_op(self) -> None:
        relay = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(relay.root_revision))

        # Five tiny files all under one binding.
        for i in range(5):
            path = f"f{i}.txt"
            (self.local_root / path).write_bytes(f"payload-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        # Sanity: every op succeeded.
        self.assertEqual(result.succeeded_count, 5)
        self.assertEqual(result.failed_count, 0)
        # SO-2: exactly one fetch — the initial head load — regardless
        # of how many ops the cycle drained. Pre-fix this was 1 + N
        # (one initial fetch + one per successful publish). The single
        # remaining fetch happens because the caller didn't pass a
        # ``manifest`` argument; passing one would zero this out.
        self.assertEqual(
            relay.state_fetch_count, 1,
            "expected exactly one cycle-driven fetch_manifest "
            f"(initial head); saw {relay.state_fetch_count}. "
            "Per-op manifest GET regressed — see "
            "docs/plans/vault-large-folder-perf.md SO-2.",
        )

    def test_backup_only_cycle_does_one_root_fetch_per_cycle(self) -> None:
        # Phase H step 2: the ``manifest=`` kwarg is preserved for caller
        # compatibility but the sharded cycle ignores it and fetches the
        # root + shard pair fresh. The SO-2 invariant the test pins is
        # "exactly one state fetch per cycle, not per op" — i.e. count
        # equals 1 regardless of how many ops the cycle drains.
        relay = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(relay.root_revision))

        for i in range(3):
            path = f"g{i}.txt"
            (self.local_root / path).write_bytes(b"x" * (10 + i))
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            root = vault.fetch_root_manifest(relay)
            shards = {
                pointer["remote_folder_id"]: vault.fetch_folder_shard(
                    relay, pointer["remote_folder_id"],
                )
                for pointer in root.get("remote_folders", [])
                if pointer.get("shard_hash")
            }
            head = assemble_unified_manifest(root, shards)
            relay.state_fetch_count = 0  # baseline after this priming fetch
            run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                manifest=head,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        self.assertEqual(
            relay.state_fetch_count, 1,
            "expected exactly one cycle-driven state fetch (root) "
            "regardless of op count; per-op refetch regressed — saw "
            f"{relay.state_fetch_count}",
        )

    def test_backup_only_cycle_refetches_after_failed_op(self) -> None:
        relay = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(relay.root_revision))

        (self.local_root / "boom.txt").write_bytes(b"x" * 64)
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="boom.txt",
        )

        # Wedge the relay so the chunk PUT fails (non-CAS).
        original_put = relay.put_chunk
        def _explode(*a, **kw):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("network down")
        relay.put_chunk = _explode  # type: ignore[assignment]

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()
            relay.put_chunk = original_put  # type: ignore[assignment]

        self.assertEqual(result.failed_count, 1)
        # 2 fetches expected: 1 initial + 1 post-failure refresh (F-Y07
        # path is preserved for "world changed" recovery).
        self.assertEqual(
            relay.state_fetch_count, 2,
            "expected 1 initial + 1 post-failure fetch_manifest; "
            f"saw {relay.state_fetch_count}",
        )


class SyncNowToastTests(unittest.TestCase):
    """T10.6: format_sync_outcome_toast → user-facing one-liner."""

    def _make(self, outcomes: list[SyncOpOutcome], *, started: int = 5, ended: int = 5) -> SyncCycleResult:
        return SyncCycleResult(
            binding_id="rb_v1_x",
            started_at_revision=started,
            ended_at_revision=ended,
            outcomes=outcomes,
        )

    def test_empty_queue_already_caught_up(self) -> None:
        toast = format_sync_outcome_toast(self._make([], started=7, ended=7))
        self.assertEqual(toast, "Sync now: nothing to do.")

    def test_empty_queue_but_remote_advanced(self) -> None:
        toast = format_sync_outcome_toast(self._make([], started=7, ended=9))
        self.assertIn("caught up at revision 9", toast)

    def test_mixed_outcomes_render_in_order(self) -> None:
        outcomes = [
            SyncOpOutcome(op_id=1, op_type="upload", relative_path="a", status="uploaded"),
            SyncOpOutcome(op_id=2, op_type="upload", relative_path="b", status="uploaded"),
            SyncOpOutcome(op_id=3, op_type="delete", relative_path="c", status="deleted"),
            SyncOpOutcome(op_id=4, op_type="upload", relative_path="d", status="skipped"),
            SyncOpOutcome(op_id=5, op_type="upload", relative_path="e", status="failed", error="boom"),
        ]
        toast = format_sync_outcome_toast(self._make(outcomes))
        self.assertIn("2 uploaded", toast)
        self.assertIn("1 deleted", toast)
        self.assertIn("1 skipped", toast)
        self.assertIn("1 failed", toast)


class FlushAndSyncTests(unittest.TestCase):
    """T10.6: manual "Sync now" button entrypoint."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_flushsync_test_"))
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

    def test_flush_calls_watcher_tick_then_runs_cycle(self) -> None:
        # Build empty remote + bound binding.
        manifest = _empty_unified(created_at="2026-05-04T12:00:00.000Z")
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
        finally:
            vault.close()

        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound",
            last_synced_revision=int(manifest["revision"]),
        )
        binding = self.store.get_binding(binding.binding_id)

        # Stubbed watcher whose tick() flushes a pending event into the
        # store — exactly what the real coordinator does.
        local_path = self.local_root / "fresh.txt"
        local_path.write_bytes(b"freshly observed local edit")

        store = self.store
        binding_id = binding.binding_id
        ticks: list[int] = []

        class FakeCoordinator:
            def tick(self_inner) -> int:
                ticks.append(1)
                store.coalesce_op(
                    binding_id=binding_id, op_type="upload",
                    relative_path="fresh.txt",
                )
                return 1

        vault = _vault()
        try:
            result = flush_and_sync_binding(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=OTHER_DEVICE,
                watcher_coordinator=FakeCoordinator(),
            )
        finally:
            vault.close()

        self.assertEqual(ticks, [1])           # watcher.tick() ran
        self.assertEqual(result.succeeded_count, 1)  # cycle drained the op
        self.assertEqual(result.outcomes[0].status, "uploaded")
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])

    def test_flush_swallows_watcher_errors(self) -> None:
        """A broken watcher must not block the manual sync."""
        manifest = _empty_unified(created_at="2026-05-04T12:00:00.000Z")
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
        finally:
            vault.close()
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound",
            last_synced_revision=int(manifest["revision"]),
        )
        binding = self.store.get_binding(binding.binding_id)

        class BrokenCoordinator:
            def tick(self) -> int:
                raise RuntimeError("watchdog crashed")

        vault = _vault()
        try:
            result = flush_and_sync_binding(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                watcher_coordinator=BrokenCoordinator(),
            )
        finally:
            vault.close()

        # Cycle still ran; nothing in queue → outcomes empty.
        self.assertEqual(result.outcomes, [])

    def test_flush_without_watcher_runs_cycle_directly(self) -> None:
        manifest = _empty_unified(created_at="2026-05-04T12:00:00.000Z")
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
        finally:
            vault.close()
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id, state="bound",
            last_synced_revision=int(manifest["revision"]),
        )
        binding = self.store.get_binding(binding.binding_id)

        # Op already enqueued externally (watcher off).
        (self.local_root / "manual.txt").write_bytes(b"queued by user")
        self.store.coalesce_op(
            binding_id=binding.binding_id, op_type="upload",
            relative_path="manual.txt",
        )

        vault = _vault()
        try:
            result = flush_and_sync_binding(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 1)
        self.assertIn("uploaded", format_sync_outcome_toast(result))


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1, manifest_revision=1,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )


class CountingRelay(FakeUploadRelay):
    """FakeUploadRelay variant that exposes a counter on every
    cycle-driven state fetch.

    Used by :class:`FetchManifestPerOpTests` to pin SO-2 — every extra
    GET on the success path is a regression and shows up here. The
    sharded cycle reads the root + this binding's shard once per
    cycle; this counter increments on every ``get_root`` so the SO-2
    pinning ("one state fetch per cycle, not per op") still holds.
    """

    def __init__(self) -> None:
        super().__init__()
        self.state_fetch_count = 0

    def get_root(self, vault_id, vault_access_secret):
        self.state_fetch_count += 1
        return super().get_root(vault_id, vault_access_secret)


def _empty_unified(*, created_at: str) -> dict:
    """Build an empty single-folder unified manifest via sharded primitives."""
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=1, parent_root_revision=0,
        created_at=created_at,
        author_device_id=AUTHOR,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at=created_at,
                created_by_device_id=AUTHOR,
            ),
        ],
    )
    shard = make_folder_shard(
        vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
        shard_revision=1, parent_shard_revision=0,
        created_at=created_at,
        author_device_id=AUTHOR,
        entries=[],
    )
    return assemble_unified_manifest(root, {DOCS_ID: shard})


if __name__ == "__main__":
    unittest.main()
