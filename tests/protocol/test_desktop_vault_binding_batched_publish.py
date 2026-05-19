"""SO-3: batched manifest publish in ``run_backup_only_cycle``.

The plan: ``temp/finished-plans/vault-large-folder-perf.md`` §Phase 2 SO-3.

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
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
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
    seed_sharded_state,
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
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=1, parent_root_revision=0,
            created_at="2026-05-16T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_root_folder_pointer(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-16T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                ),
            ],
        )
        shard = make_folder_shard(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            shard_revision=1, parent_shard_revision=0,
            created_at="2026-05-16T12:00:00.000Z",
            author_device_id=AUTHOR,
            entries=[],
        )
        manifest = assemble_unified_manifest(root, {DOCS_ID: shard})
        relay = BatchProbeRelay()
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
        # Discard the setup publish so the per-test counters start clean.
        relay.publish_attempt_count = 0
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
        finally:
            vault.close()
        return assemble_unified_manifest(res.root, {res.remote_folder_id: res.shard})

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
            relay.publish_attempt_count, 1,
            "expected exactly one CAS publish for a 5-op batch; "
            f"saw {relay.publish_attempt_count}",
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
        self.assertEqual(relay.publish_attempt_count, 3)
        self.assertEqual(len(relay.published_shards), 3)


class CASConflictMidBatchTests(_BatchTestBase):
    def test_publish_batch_with_cas_retry_uses_merge_after_first_409(self) -> None:
        """Review §2.H2: the batch CAS retry must flip to merge-mode
        on the first 409 — matches the upload/folder.py fix from
        commit 3fb7470. Pre-fix the retry blindly re-applied the
        batch via ``_apply_batch_to_shard`` (path + entry_id match),
        so two devices uploading the same path to a backup-only
        folder under different entry_ids would have Device B's
        version silently appended to Device A's entry, losing the
        §D4 collision-rename. This is a source-level check that the
        ``use_merge=True`` flip is present; end-to-end CAS-conflict
        flow is exercised by the integration test above."""
        from pathlib import Path as _P
        from tests.protocol._paths import REPO_ROOT
        source = (
            _P(REPO_ROOT)
            / "desktop"
            / "src"
            / "vault"
            / "binding"
            / "sync.py"
        ).read_text()
        # Find the _publish_batch_with_cas_retry function body.
        marker = "def _publish_batch_with_cas_retry("
        idx = source.find(marker)
        self.assertGreater(idx, 0, "function not found")
        # 8000 chars covers the loop body + helper definition.
        body = source[idx : idx + 8000]
        self.assertIn(
            "use_merge = False", body,
            "batch CAS retry must start with use_merge=False",
        )
        self.assertIn(
            "use_merge = True", body,
            "batch CAS retry must flip to use_merge=True on conflict",
        )
        self.assertIn(
            "_merge_batch_into_shard_with_bump", body,
            "merge-mode candidate must rebuild via "
            "_merge_batch_into_shard_with_bump (§D4 collision-rename "
            "+ tie-break)",
        )

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
        self.assertEqual(relay.publish_attempt_count, 2)
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
        relay.publish_attempt_count = 0
        relay.published_shards = []
        relay.published_roots = []
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
        self.assertEqual(relay.publish_attempt_count, 1)
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
        self.assertEqual(relay.publish_attempt_count, 1)
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
            relay.publish_attempt_count, 1,
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
        relay.publish_attempt_count = 0
        relay.published_shards = []
        relay.published_roots = []
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
        self.assertEqual(relay.publish_attempt_count, 1)
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
    ``publish_attempt_count`` counts every shard-with-root publish
    attempt (success or 409). ``cas_conflicts_to_inject`` flips a
    one-shot 409 on the next attempt — the inline envelope mirrors
    what the real server returns per §A1 so the §D4 rebase loop in
    ``_publish_batch_with_cas_retry`` has real ciphertext to decrypt.
    """

    def __init__(self) -> None:
        super().__init__()
        self.publish_attempt_count = 0
        self.cas_conflicts_to_inject = 0

    def put_shard_with_root(
        self, vault_id, vault_access_secret, remote_folder_id, *,
        shard, root,
    ):
        self.publish_attempt_count += 1
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


class BatchOpLogProducerTests(_BatchTestBase):
    """Phase 2 of docs/plans/activity-timeline.md — assert that the
    batch publish path actually appends entries to the shard's
    ``operation_log_tail``.

    Each test decrypts the post-publish shard and inspects the tail
    directly; that's the same surface the consumer side
    (``state/activity.normalize_op_log_entry``) parses.
    """

    def _decrypt_shard(self, relay: "BatchProbeRelay") -> dict:
        observer = _vault()
        try:
            return observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()

    def test_upload_batch_lands_upload_completed_entries(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))
        paths = [f"u{i}.txt" for i in range(3)]
        for path in paths:
            (self.local_root / path).write_bytes(b"x")
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=3,
            )
        finally:
            vault.close()

        shard = self._decrypt_shard(relay)
        tail = shard.get("operation_log_tail") or []
        upload_entries = [e for e in tail if e.get("type") == "vault.upload.completed"]
        self.assertEqual(len(upload_entries), 3)
        self.assertEqual(
            sorted(e["path"] for e in upload_entries),
            sorted(normalize_manifest_path(p) for p in paths),
        )
        # Every entry carries the publish revision + author + epoch ts.
        for entry in upload_entries:
            self.assertEqual(entry["device_id"], OTHER_DEVICE)
            self.assertEqual(entry["revision"], int(shard["shard_revision"]))
            self.assertIsInstance(entry["ts"], int)
            self.assertGreater(entry["ts"], 0)

    def test_mixed_batch_lands_both_event_types(self) -> None:
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="goner.txt", content=b"please go",
        )
        relay.publish_attempt_count = 0
        relay.published_shards = []
        relay.published_roots = []
        relay.shard_with_root_puts = 0

        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path="goner.txt",
            content_fingerprint="seed",
            size_bytes=8, mtime_ns=1_000_000_000,
            last_synced_revision=int(manifest["revision"]),
        ))

        (self.local_root / "fresh.txt").write_bytes(b"fresh")
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="fresh.txt",
        )
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="delete",
            relative_path="goner.txt",
        )

        vault = _vault()
        try:
            run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=10,
            )
        finally:
            vault.close()

        shard = self._decrypt_shard(relay)
        tail = shard.get("operation_log_tail") or []
        # Upload entries from the seed publish stayed put; this publish
        # added one upload + one delete entry from OTHER_DEVICE.
        own_entries = [e for e in tail if e.get("device_id") == OTHER_DEVICE]
        types = sorted(e["type"] for e in own_entries)
        self.assertEqual(
            types, ["vault.delete.completed", "vault.upload.completed"],
        )
        delete = next(e for e in own_entries if e["type"] == "vault.delete.completed")
        self.assertEqual(delete["path"], "goner.txt")
        upload = next(e for e in own_entries if e["type"] == "vault.upload.completed")
        self.assertEqual(upload["path"], "fresh.txt")

    def test_cas_retry_preserves_server_tail(self) -> None:
        """D7: when a 409 fires the CAS-merge path, the retry's tail
        must preserve the server-side tail (which already contains the
        concurrent writer's entry) instead of starting from the local
        attempt's tail."""
        relay, manifest = self._empty_remote()
        binding = self._make_bound_binding(last_revision=int(manifest["revision"]))

        (self.local_root / "ours.txt").write_bytes(b"ours")
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="ours.txt",
        )

        # Inject one 409 so the next publish hits the merge path.
        relay.cas_conflicts_to_inject = 1
        vault = _vault()
        try:
            run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=OTHER_DEVICE,
                batch_size=1,
            )
        finally:
            vault.close()

        # Two attempts (one 409, one success) on the same logical mutation.
        self.assertEqual(relay.publish_attempt_count, 2)
        shard = self._decrypt_shard(relay)
        tail = shard.get("operation_log_tail") or []
        # Exactly one entry — the merge path didn't double-record on retry.
        own = [e for e in tail if e.get("device_id") == OTHER_DEVICE]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["type"], "vault.upload.completed")
        self.assertEqual(own[0]["path"], "ours.txt")


class DeleteAndRestoreOpLogTests(unittest.TestCase):
    """Phase 2 — single-file delete/restore land op-log entries.

    Re-uses the seeded-fake-relay scaffolding from
    ``test_desktop_vault_delete``; these tests verify the
    ``_publish_shard_with_retry`` op_log_entries hook lands the
    correct entry on the resulting shard.
    """

    def _decrypt_docs_shard(self, relay) -> dict:
        observer = _vault()
        try:
            return observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()

    def test_delete_file_lands_entry(self) -> None:
        from src.vault.ops.delete import delete_file
        from tests.protocol.test_desktop_vault_delete import (
            _empty_manifest, _seeded_manifest, _vault as _delete_vault,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay, seed_sharded_state,
        )
        manifest = _seeded_manifest([("alpha.txt", "hello")])
        relay = FakeUploadRelay()
        vault = _delete_vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest["vault_id"],
                remote_folders=manifest["remote_folders"],
                created_at=manifest["created_at"],
                author_device_id=manifest["author_device_id"],
            )
            delete_file(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, remote_path="alpha.txt",
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()
        shard = self._decrypt_docs_shard(relay)
        tail = shard.get("operation_log_tail") or []
        deletes = [e for e in tail if e.get("type") == "vault.delete.completed"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["path"], "alpha.txt")
        self.assertEqual(deletes[0]["device_id"], AUTHOR)
        self.assertEqual(deletes[0]["revision"], int(shard["shard_revision"]))

    def test_restore_version_lands_entry_with_source_version_id(self) -> None:
        # Build a history: seed → delete → restore-from-original-version.
        from src.vault.ops.delete import delete_file, restore_version_to_current
        from tests.protocol.test_desktop_vault_delete import (
            _seeded_manifest, _vault as _delete_vault,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay, seed_sharded_state,
        )
        manifest = _seeded_manifest([("alpha.txt", "hello")])
        relay = FakeUploadRelay()
        vault = _delete_vault()
        original_version_id = ""
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest["vault_id"],
                remote_folders=manifest["remote_folders"],
                created_at=manifest["created_at"],
                author_device_id=manifest["author_device_id"],
            )
            # Find the seeded file's version_id.
            for folder in manifest["remote_folders"]:
                for entry in folder.get("entries", []):
                    if entry.get("path") == "alpha.txt":
                        original_version_id = entry["versions"][0]["version_id"]
            self.assertNotEqual(original_version_id, "")
            delete_file(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, remote_path="alpha.txt",
                author_device_id=AUTHOR,
            )
            restore_version_to_current(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, remote_path="alpha.txt",
                source_version_id=original_version_id,
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()
        shard = self._decrypt_docs_shard(relay)
        tail = shard.get("operation_log_tail") or []
        restores = [e for e in tail if e.get("type") == "vault.restore.completed"]
        self.assertEqual(len(restores), 1)
        self.assertEqual(restores[0]["path"], "alpha.txt")
        self.assertEqual(restores[0]["source_version_id"], original_version_id)


class ClearFolderOpLogTests(unittest.TestCase):
    """Phase 2 — clear_folder lands per-file delete entries AND a
    summary ``vault.folder.cleared`` entry on the same shard revision.
    """

    def test_clear_folder_lands_summary_alongside_per_file_deletes(self) -> None:
        from src.vault.ops.clear import clear_folder
        from tests.protocol.test_desktop_vault_delete import (
            _seeded_manifest, _vault as _delete_vault,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay, seed_sharded_state,
        )
        manifest = _seeded_manifest([
            ("a.txt", "a"), ("b.txt", "b"), ("c.txt", "c"),
        ])
        relay = FakeUploadRelay()
        vault = _delete_vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest["vault_id"],
                remote_folders=manifest["remote_folders"],
                created_at=manifest["created_at"],
                author_device_id=manifest["author_device_id"],
            )
            clear_folder(
                vault=vault, relay=relay,
                remote_folder_id=DOCS_ID, author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        tail = shard.get("operation_log_tail") or []
        types = sorted(e["type"] for e in tail)
        # 3 per-file deletes + 1 folder-cleared summary.
        self.assertEqual(types, [
            "vault.delete.completed",
            "vault.delete.completed",
            "vault.delete.completed",
            "vault.folder.cleared",
        ])
        # All entries share the publish revision.
        revisions = {e["revision"] for e in tail}
        self.assertEqual(revisions, {int(shard["shard_revision"])})


class EvictionOpLogTests(unittest.TestCase):
    """Phase 2 — eviction stages land vault.eviction.* entries on the
    shard tail. The per-folder publish path runs through
    ``_publish_folder_purge_with_retry`` which receives
    ``op_log_event`` + ``op_log_paths`` from ``_run_stage``.
    """

    def test_run_stage_lands_event_entry_per_affected_path(self) -> None:
        # Drive _publish_folder_purge_with_retry directly with a no-op
        # mutate closure — verifies the op-log wiring without needing
        # the full eviction pipeline + gc_plan plumbing.
        from src.vault.ops.eviction import _publish_folder_purge_with_retry
        from tests.protocol.test_desktop_vault_delete import (
            _seeded_manifest, _vault as _delete_vault,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay, seed_sharded_state,
        )
        manifest = _seeded_manifest([("doomed.txt", "x")])
        relay = FakeUploadRelay()
        vault = _delete_vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest["vault_id"],
                remote_folders=manifest["remote_folders"],
                created_at=manifest["created_at"],
                author_device_id=manifest["author_device_id"],
            )
            _publish_folder_purge_with_retry(
                vault=vault, relay=relay,
                remote_folder_id=DOCS_ID,
                mutate=lambda shard, _purged: shard,  # no entry change
                purged=set(),
                author_device_id=AUTHOR,
                op_log_event="vault.eviction.auto_purged_oldest",
                op_log_paths=["doomed.txt"],
            )
        finally:
            vault.close()

        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        tail = shard.get("operation_log_tail") or []
        evictions = [
            e for e in tail
            if e.get("type") == "vault.eviction.auto_purged_oldest"
        ]
        self.assertEqual(len(evictions), 1)
        self.assertEqual(evictions[0]["path"], "doomed.txt")
        self.assertEqual(evictions[0]["device_id"], AUTHOR)


class FetchUnifiedManifestIntegrationTests(unittest.TestCase):
    """Phase 4 of docs/plans/activity-timeline.md — closes the
    producer→consumer loop with the same fetch path the Activity tab
    uses at runtime.

    Phase 1's assemble_unified_manifest tests proved the synthetic
    merge; Phase 2/3's producer tests decrypted shards directly. This
    test runs the real Vault.fetch_unified_manifest (fetch root + N
    shards from relay + assemble) so a regression in either side of
    the merge surfaces here — e.g., a future refactor that drops
    shard tails on assemble, or a publish path that forgets to bump
    the shard hash.
    """

    def test_upload_delete_and_clear_round_trip_via_fetch_unified(self) -> None:
        from src.vault.ops.clear import clear_vault
        from src.vault.ops.delete import delete_file
        from src.vault.upload import upload_file
        from tests.protocol.test_desktop_vault_delete import (
            _seeded_manifest, _vault as _delete_vault,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay, seed_sharded_state,
        )
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR, DOCS_ID,
        )

        # Seed: one folder, one file (seed already publishes one upload entry).
        manifest = _seeded_manifest([("seed.txt", "seed")])
        relay = FakeUploadRelay()
        vault = _delete_vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest["vault_id"],
                remote_folders=manifest["remote_folders"],
                created_at=manifest["created_at"],
                author_device_id=manifest["author_device_id"],
            )
        finally:
            vault.close()

        # Drive a fresh upload through the real upload_file producer.
        import tempfile
        with tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False, mode="w",
        ) as tmp:
            tmp.write("hello")
            tmp_path = tmp.name
        vault = _delete_vault()
        try:
            upload_file(
                vault=vault, relay=relay, manifest={},
                local_path=Path(tmp_path), remote_folder_id=DOCS_ID,
                remote_path="hello.txt", author_device_id=AUTHOR,
            )
            delete_file(
                vault=vault, relay=relay, manifest={},
                remote_folder_id=DOCS_ID, remote_path="seed.txt",
                author_device_id=AUTHOR,
            )
            clear_vault(
                vault=vault, relay=relay, author_device_id=AUTHOR,
            )
        finally:
            vault.close()
            os.unlink(tmp_path)

        # The Activity tab's fetch path — exactly what tab_activity.py
        # uses at runtime to populate the timeline.
        observer = _delete_vault()
        try:
            unified = observer.fetch_unified_manifest(relay)
        finally:
            observer.close()

        tail = unified.get("operation_log_tail") or []
        types = sorted(e["type"] for e in tail)
        # Expected timeline:
        #   - vault.upload.completed × 1 (seed) — from seed_sharded_state
        #   - vault.upload.completed × 1 (hello.txt)
        #   - vault.delete.completed × 1 (seed.txt)
        #   - vault.vault.cleared × 1 (root publish from clear_vault)
        #   - vault.folder.cleared × 1 (summary entry from clear_vault's
        #     internal delete_folder_contents call)
        #   - vault.delete.completed × 1 (hello.txt being tombstoned by clear_vault)
        # The exact upload counts depend on whether seed_sharded_state's
        # baseline publish stamped an entry; assert the event-types set
        # without pinning multiplicity.
        self.assertIn("vault.upload.completed", types)
        self.assertIn("vault.delete.completed", types)
        self.assertIn("vault.vault.cleared", types)
        self.assertIn("vault.folder.cleared", types)

        # Sort by ts is deterministic per D2 (tie-break on device_id, revision).
        timestamps = [int(e.get("ts", 0)) for e in tail]
        self.assertEqual(timestamps, sorted(timestamps),
                         "merged tail must be sorted ascending by ts")

        # Every entry carries the AUTHOR device_id from this single-device
        # session.
        for entry in tail:
            self.assertEqual(entry.get("device_id"), AUTHOR)


if __name__ == "__main__":
    unittest.main()
