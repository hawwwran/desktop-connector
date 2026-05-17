"""SO-3: batched manifest publish in ``run_backup_only_cycle``.

The plan: ``docs/plans/vault-large-folder-perf.md`` §Phase 2 SO-3.

Pre-SO-3 each pending op got its own CAS-published manifest revision —
ten thousand small files = ten thousand publishes, each shipping the
whole encrypted manifest (≈O(N²) bytes per cycle). The fix is a
per-binding batch: encrypt + PUT chunks N at a time, then publish one
manifest revision that carries all the version-adds / tombstones.

These tests pin the four worth-protecting shapes:

1. Clean batch — N ops publish in one CAS round.
2. CAS conflict mid-batch — first publish 409s, retry rebases the
   batch on the new server head, second publish succeeds.
3. Mixed upload + delete batch — version-adds and tombstones share
   the same publish.
4. Kill-mid-batch resume — the batch publish raises; chunks survive
   on the relay; the next cycle HEAD-and-skips and finishes.
"""

from __future__ import annotations

import base64
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
from src.vault.binding.bindings import VaultBindingsStore, VaultLocalEntry  # noqa: E402
from src.vault.binding.sync import (  # noqa: E402
    PUBLISH_BATCH_SIZE,
    run_backup_only_cycle,
)
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    find_file_entry,
    make_manifest,
    make_remote_folder,
    normalize_manifest_path,
)
from src.vault.relay_errors import VaultCASConflictError  # noqa: E402
from src.vault.upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import (  # noqa: E402
    FakeUploadRelay,
    seed_sharded_state_from_manifest,
)


VAULT_ACCESS_SECRET = "vault-secret"
OTHER_DEVICE = "f1e2d3c4b5a6918273645566778899aa"


class _BatchTestBase(unittest.TestCase):
    """Shared scaffolding for the four SO-3 acceptance tests."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_sync_so3_"))
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

    def _empty_remote(self) -> tuple["BatchProbeRelay", dict]:
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-16T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-16T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                ),
            ],
        )
        relay = BatchProbeRelay(manifest=manifest)
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
        finally:
            vault.close()
        # Discard the setup publish so the per-test counters start clean.
        relay.put_manifest_attempt_count = 0
        relay.published_manifests = []
        relay.published_shards = []
        relay.published_roots = []
        relay.shard_with_root_puts = 0
        return relay, manifest

    def _seed_remote_file(
        self, relay: FakeUploadRelay, manifest: dict, *,
        path: str, content: bytes,
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
            seed_sharded_state_from_manifest(vault, relay, res.manifest)
        finally:
            vault.close()
        return res.manifest

    def _make_bound_binding(self, *, last_revision: int):
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


class CleanBatchTests(_BatchTestBase):
    def test_K_uploads_publish_in_one_cas_round(self) -> None:
        """5 ops with batch_size=5 → exactly one CAS publish."""
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        paths = [f"a{i}.txt" for i in range(5)]
        for path in paths:
            (self.local_root / path).write_bytes(f"payload-{path}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=5,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 5)
        self.assertEqual(result.failed_count, 0)
        for outcome in result.outcomes:
            self.assertEqual(outcome.status, "uploaded")

        # SO-3: exactly one publish covers all five mutations. Pre-SO-3
        # this would be five.
        self.assertEqual(
            relay.put_manifest_attempt_count, 1,
            "expected exactly one CAS publish for a 5-op batch; "
            f"saw {relay.put_manifest_attempt_count}",
        )
        self.assertEqual(len(relay.published_shards), 1)

        # All five paths landed remotely under one new revision.
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        remote_paths = sorted(
            str(e.get("path", "")) for e in shard["entries"] or []
            if isinstance(e, dict) and not bool(e.get("deleted"))
        )
        self.assertEqual(remote_paths, paths)

        # Queue drained; local-entries stamped.
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])
        for path in paths:
            entry = self.store.get_local_entry(binding.binding_id, path)
            self.assertIsNotNone(entry)
            self.assertNotEqual(entry.content_fingerprint, "")

    def test_smaller_batch_size_splits_into_multiple_publishes(self) -> None:
        """7 ops with batch_size=3 → 3 publishes (3 + 3 + 1 cycle-end flush)."""
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        for i in range(7):
            path = f"b{i}.txt"
            (self.local_root / path).write_bytes(f"data-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=3,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 7)
        # 3 + 3 + 1 (cycle-end flush of the partial batch)
        self.assertEqual(relay.put_manifest_attempt_count, 3)
        self.assertEqual(len(relay.published_shards), 3)


class CASConflictMidBatchTests(_BatchTestBase):
    def test_batch_retries_after_one_cas_conflict(self) -> None:
        """The first publish 409s with the inline server head; the
        retry re-applies the batch on the new head and the second
        publish succeeds. All ops finish uploaded.
        """
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        paths = [f"c{i}.txt" for i in range(3)]
        for path in paths:
            (self.local_root / path).write_bytes(f"conflict-{path}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        # Inject one CAS conflict on the next put_manifest attempt; the
        # retry rebases and publishes for real.
        relay.cas_conflicts_to_inject = 1

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=3,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 3)
        self.assertEqual(result.failed_count, 0)
        for outcome in result.outcomes:
            self.assertEqual(outcome.status, "uploaded")

        # 2 attempts: 1 conflict + 1 success. 1 publish recorded.
        self.assertEqual(relay.put_manifest_attempt_count, 2)
        self.assertEqual(len(relay.published_shards), 1)
        self.assertEqual(relay.cas_conflicts_to_inject, 0)

        # All three paths landed at the post-retry revision.
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        remote_paths = sorted(
            str(e.get("path", "")) for e in shard["entries"] or []
            if isinstance(e, dict) and not bool(e.get("deleted"))
        )
        self.assertEqual(remote_paths, paths)


class MixedUploadAndDeleteTests(_BatchTestBase):
    def test_batch_publishes_uploads_and_tombstones_together(self) -> None:
        """Seed two remote files, then enqueue two new uploads + one
        delete of an existing file. One batch publish carries all
        three mutations.
        """
        relay, manifest = self._empty_remote()
        # Seed two existing remote files.
        manifest = self._seed_remote_file(
            relay, manifest, path="keep.txt", content=b"keep me",
        )
        manifest = self._seed_remote_file(
            relay, manifest, path="goner.txt", content=b"please go",
        )
        relay.put_manifest_attempt_count = 0
        relay.published_manifests = []
        relay.published_shards = []
        relay.published_roots = []
        relay.shard_with_root_puts = 0

        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))
        # Pretend baseline stamped local-entries for both seeded files.
        for path in ("keep.txt", "goner.txt"):
            self.store.upsert_local_entry(VaultLocalEntry(
                binding_id=binding.binding_id,
                relative_path=path,
                content_fingerprint="seed",
                size_bytes=8, mtime_ns=1_000_000_000,
                last_synced_revision=int(manifest["revision"]),
            ))

        # New uploads.
        for i in range(2):
            path = f"new{i}.txt"
            (self.local_root / path).write_bytes(f"new-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )
        # Delete the existing file.
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="delete",
            relative_path="goner.txt",
        )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
            )
        finally:
            vault.close()

        # 2 uploaded + 1 deleted in a single publish.
        statuses = sorted(o.status for o in result.outcomes)
        self.assertEqual(statuses, ["deleted", "uploaded", "uploaded"])
        self.assertEqual(relay.put_manifest_attempt_count, 1)
        self.assertEqual(len(relay.published_shards), 1)

        # Verify the post-publish shard carries:
        #   - keep.txt: alive
        #   - goner.txt: tombstoned
        #   - new0.txt, new1.txt: alive
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        alive = {
            str(e["path"]) for e in shard["entries"] or []
            if isinstance(e, dict) and not bool(e.get("deleted"))
        }
        tombstoned = {
            str(e["path"]) for e in shard["entries"] or []
            if isinstance(e, dict) and bool(e.get("deleted"))
        }
        self.assertEqual(alive, {"keep.txt", "new0.txt", "new1.txt"})
        self.assertEqual(tombstoned, {"goner.txt"})

        # Queue drained; goner's local-entry reaped.
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])
        self.assertIsNone(self.store.get_local_entry(binding.binding_id, "goner.txt"))
        self.assertIsNotNone(self.store.get_local_entry(binding.binding_id, "new0.txt"))


class KillMidBatchResumeTests(_BatchTestBase):
    def test_chunks_survive_kill_and_next_cycle_finishes_via_dedupe(self) -> None:
        """Simulate a process death between chunk-PUTs and the batch
        publish: the relay raises on put_manifest. Chunks already
        landed survive; the pending-ops queue survives. The next
        cycle re-encrypts the same files (cheap for 1-chunk files),
        HEAD-and-skips on the relay (no new PUTs), then publishes.
        """
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        for i in range(3):
            path = f"k{i}.txt"
            (self.local_root / path).write_bytes(f"kill-test-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        # First cycle: relay accepts chunk PUTs but explodes on publish.
        original_put = relay.put_shard_with_root
        def _kill_on_publish(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("simulated kill mid-publish")
        relay.put_shard_with_root = _kill_on_publish  # type: ignore[assignment]

        vault = _vault()
        try:
            result_run1 = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
            )
        finally:
            vault.close()

        # The batch failed wholesale: all three ops report failed.
        self.assertEqual(result_run1.failed_count, 3)
        self.assertEqual(result_run1.succeeded_count, 0)
        for outcome in result_run1.outcomes:
            self.assertEqual(outcome.status, "failed")
        # Chunks landed on the relay even though the publish blew up.
        chunks_after_kill = set(relay.chunks)
        self.assertEqual(len(chunks_after_kill), 3)
        # Pending ops survived for retry (mark_op_failed bumps attempts
        # but doesn't dequeue).
        remaining = self.store.list_pending_ops(binding.binding_id)
        self.assertEqual(len(remaining), 3)
        for op in remaining:
            self.assertEqual(op.attempts, 1)

        # Restore the healthy publish path.
        relay.put_shard_with_root = original_put  # type: ignore[assignment]
        puts_before_run2 = list(relay.put_calls)

        # Second cycle: should HEAD-and-skip the existing chunks and
        # publish in one batch.
        binding = self.store.get_binding(binding.binding_id)
        vault = _vault()
        try:
            result_run2 = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
            )
        finally:
            vault.close()

        # Run 2 cleared everything.
        self.assertEqual(result_run2.succeeded_count, 3)
        self.assertEqual(result_run2.failed_count, 0)
        for outcome in result_run2.outcomes:
            self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])

        # No fresh chunk PUTs happened — dedupe worked.
        new_puts = relay.put_calls[len(puts_before_run2):]
        self.assertEqual(
            new_puts, [],
            "kill-mid-batch resume re-uploaded chunks; HEAD-and-skip "
            "dedupe broke",
        )
        # Chunk set on the relay is unchanged.
        self.assertEqual(set(relay.chunks), chunks_after_kill)
        # One publish covered the three resumed ops.
        self.assertEqual(len(relay.published_shards), 1)


class CycleEndCancelFlushTests(_BatchTestBase):
    """F-Y08 carve-out for SO-3: when cancellation is still active at
    cycle-end, the partial batch publishes with the CAS retry budget
    set to zero (single attempt, no retry storm). A conflict drops
    the batch; success commits it. The chunks are PUT either way, so
    the next cycle's prep re-uses the dedupe stub.
    """

    def test_cancel_with_uncontended_publish_commits_partial_batch(self) -> None:
        """No CAS contention — single attempt succeeds, partial batch
        commits, all batched ops report uploaded."""
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        for path in ("a.txt", "b.txt", "c.txt"):
            (self.local_root / path).write_bytes(path.encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        ticks = {"n": 0}
        def gate() -> bool:
            ticks["n"] += 1
            # Allow ops 1+2 to prep, then deny op 3. Each op takes
            # 3 should_continue calls (outer pre-op + chunk + pre-
            # publish). 2 ops × 3 + 1 outer = 7 ticks. Ticks <= 6
            # pass; tick 7 (outer at op 3 start) bails.
            return ticks["n"] <= 6

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
                should_continue=gate,
            )
        finally:
            vault.close()

        self.assertTrue(result.cancelled)
        # Single publish attempt at cycle-end — the two prepped ops
        # commit. The third never entered prep.
        uploaded = [o for o in result.outcomes if o.status == "uploaded"]
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(relay.put_manifest_attempt_count, 1)
        # Op 3 stays queued for the next cycle.
        remaining = self.store.list_pending_ops(binding.binding_id)
        self.assertEqual(len(remaining), 1)

    def test_cancel_with_cas_conflict_drops_batch_no_retry_storm(self) -> None:
        """CAS conflict on the cancel-flush publish — single attempt,
        no retries, batch dropped, all ops survive in the queue."""
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        for path in ("c1.txt", "c2.txt", "c3.txt"):
            (self.local_root / path).write_bytes(path.encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        # Healthy cycles use CAS_MAX_RETRIES retries (5 + 1 final = 6
        # attempts). A cancelled cycle should attempt exactly once.
        # Inject many conflicts; the count is the bail-out probe.
        relay.cas_conflicts_to_inject = 99

        ticks = {"n": 0}
        def gate() -> bool:
            ticks["n"] += 1
            return ticks["n"] <= 6

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
                should_continue=gate,
            )
        finally:
            vault.close()

        self.assertTrue(result.cancelled)
        # Single publish attempt — proves we didn't burn the retry
        # budget after the user clicked Pause (F-Y08).
        self.assertEqual(
            relay.put_manifest_attempt_count, 1,
            "cancel-flush published more than once — retry storm "
            "leaked through F-Y08 gate",
        )
        # Conflict → batch failed; all batched ops surface as failed
        # (their pending-op rows survive for the next cycle).
        failed = [o for o in result.outcomes if o.status == "failed"]
        self.assertGreaterEqual(len(failed), 1)
        # The 99 injected conflicts proves no retry — we consumed 1.
        self.assertEqual(relay.cas_conflicts_to_inject, 98)


class SkippedIdenticalInsideBatchTests(_BatchTestBase):
    """SO-3 short-circuit: when a batched upload's bytes already match
    the remote's latest version, prep returns ``skipped_identical=True``
    immediately. The cycle records the outcome + stamps local-entry +
    drops the pending op without growing the batch.
    """

    def test_identical_bytes_skip_does_not_grow_batch(self) -> None:
        relay, manifest = self._empty_remote()
        # Seed the remote at one path with content X.
        manifest = self._seed_remote_file(
            relay, manifest, path="same.txt", content=b"already-here",
        )
        relay.put_manifest_attempt_count = 0
        relay.published_manifests = []
        relay.published_shards = []
        relay.published_roots = []
        relay.shard_with_root_puts = 0

        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))
        # Stage a local file with identical content + queue an upload
        # op. Prep should detect the match via content_fingerprint.
        (self.local_root / "same.txt").write_bytes(b"already-here")
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="same.txt",
        )
        # A second op with new content, to confirm we still publish
        # when there's *something* to put in the batch.
        (self.local_root / "new.txt").write_bytes(b"new content")
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="new.txt",
        )

        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
            )
        finally:
            vault.close()

        # same.txt: skipped (identical bytes); new.txt: uploaded.
        statuses = sorted(o.status for o in result.outcomes)
        self.assertEqual(statuses, ["skipped", "uploaded"])
        # Exactly one publish (covers new.txt only — same.txt didn't
        # enter the batch).
        self.assertEqual(relay.put_manifest_attempt_count, 1)
        # Both pending ops dequeued; both local entries stamped.
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])
        same_entry = self.store.get_local_entry(binding.binding_id, "same.txt")
        new_entry = self.store.get_local_entry(binding.binding_id, "new.txt")
        self.assertIsNotNone(same_entry)
        self.assertIsNotNone(new_entry)


class StubReuseDirectTests(_BatchTestBase):
    """SO-3 dedupe stub: a kill-mid-batch retry must reuse the prior
    ``version_id`` so the chunk_ids match. The kill-mid-batch test
    asserts dedupe indirectly via ``new_puts == []``; this test reads
    the persisted stub directly to confirm the cycle's prep made the
    same id-allocation decision twice.
    """

    def test_stub_persists_same_version_id_across_kill(self) -> None:
        import json

        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        (self.local_root / "stable.txt").write_bytes(b"stable-content")
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="stable.txt",
        )

        original_put = relay.put_shard_with_root
        def _kill(*a, **kw):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("simulated kill")
        relay.put_shard_with_root = _kill  # type: ignore[assignment]

        vault = _vault()
        try:
            run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
            )
        finally:
            vault.close()

        # The stub should have been written by prep and survived the
        # failed publish. Read it from disk.
        from src.vault.upload.batch_session import default_batch_cache_dir
        from src.vault.upload.session import default_upload_resume_dir
        stub_dir = default_batch_cache_dir(default_upload_resume_dir())
        stubs = sorted(stub_dir.glob("*.json"))
        self.assertEqual(len(stubs), 1, "expected exactly one stub after kill")
        first_stub = json.loads(stubs[0].read_text())
        first_version_id = first_stub["version_id"]
        first_entry_id = first_stub["entry_id"]
        self.assertTrue(first_version_id.startswith("fv_v1_"))
        self.assertTrue(first_entry_id.startswith("fe_v1_"))

        # Restore the relay; re-run prep against the same file. The
        # stub mechanism must hand back the same ids.
        relay.put_shard_with_root = original_put  # type: ignore[assignment]
        binding = self.store.get_binding(binding.binding_id)
        vault = _vault()
        try:
            result = run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 1)
        # The successful publish should have cleared the stub.
        self.assertEqual(list(stub_dir.glob("*.json")), [])

        # The published shard's stable.txt version_id should be the
        # same one the prior cycle's stub recorded.
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        target = next(e for e in shard["entries"] if e["path"] == "stable.txt")
        published_version_id = target["versions"][-1]["version_id"]
        self.assertEqual(
            published_version_id, first_version_id,
            "stub mechanism didn't preserve version_id across kill — "
            "chunk dedupe would have broken on the retry",
        )
        published_entry_id = target["entry_id"]
        self.assertEqual(
            published_entry_id, first_entry_id,
            "stub mechanism didn't preserve entry_id across kill",
        )


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1, manifest_revision=1,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )


class BatchProbeRelay(FakeUploadRelay):
    """Relay that counts publish attempts and can inject CAS conflicts
    on demand.

    Used by the SO-3 tests to confirm the batch shape:
    ``put_manifest_attempt_count`` counts every publish attempt
    (legacy ``put_manifest`` or sharded ``put_shard_with_root``;
    success or 409). ``cas_conflicts_to_inject`` flips a one-shot 409
    on the next attempt — the inline envelope mirrors what the real
    server returns per §A1 so the §D4 rebase loop in
    ``_publish_batch_with_cas_retry`` has real ciphertext to decrypt.
    """

    def __init__(self, *, manifest: dict) -> None:
        super().__init__(manifest=manifest)
        self.put_manifest_attempt_count = 0
        self.cas_conflicts_to_inject = 0

    def put_manifest(
        self,
        vault_id,
        vault_access_secret,
        *,
        expected_current_revision,
        new_revision,
        parent_revision,
        manifest_hash,
        manifest_ciphertext,
    ):
        self.put_manifest_attempt_count += 1
        if self.cas_conflicts_to_inject > 0:
            self.cas_conflicts_to_inject -= 1
            raise VaultCASConflictError({
                "code": "vault_manifest_conflict",
                "message": "injected CAS conflict (SO-3 test)",
                "details": {
                    "current_revision": self.current_revision,
                    "current_manifest_hash": self.current_hash,
                    "current_manifest_ciphertext":
                        base64.b64encode(self.current_envelope).decode("ascii"),
                    "current_manifest_size": len(self.current_envelope),
                },
            })
        return super().put_manifest(
            vault_id, vault_access_secret,
            expected_current_revision=expected_current_revision,
            new_revision=new_revision,
            parent_revision=parent_revision,
            manifest_hash=manifest_hash,
            manifest_ciphertext=manifest_ciphertext,
        )

    def put_shard_with_root(
        self, vault_id, vault_access_secret, remote_folder_id, *,
        shard, root,
    ):
        self.put_manifest_attempt_count += 1
        if self.cas_conflicts_to_inject > 0:
            self.cas_conflicts_to_inject -= 1
            current = self.shards.get(remote_folder_id, {})
            current_envelope = current.get("envelope", b"")
            raise VaultCASConflictError({
                "code": "vault_shard_conflict",
                "message": "injected shard CAS conflict (SO-3 test)",
                "details": {
                    "remote_folder_id": remote_folder_id,
                    "current_shard_revision": int(current.get("revision", 0)),
                    "current_shard_hash": str(current.get("hash", "")),
                    "current_shard_ciphertext":
                        base64.b64encode(current_envelope).decode("ascii"),
                    "current_shard_size": len(current_envelope),
                },
            })
        return super().put_shard_with_root(
            vault_id, vault_access_secret, remote_folder_id,
            shard=shard, root=root,
        )


if __name__ == "__main__":
    unittest.main()
