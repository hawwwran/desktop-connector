"""T7.5 — eviction pass acceptance tests."""

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
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.ops.delete import delete_file  # noqa: E402
from src.vault.ops.eviction import eviction_pass  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    find_file_entry_in_shard,
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


class VaultEvictionPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_eviction_test_"))
        self._saved_xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg_cache_home is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg_cache_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stage1_drops_expired_tombstones_from_manifest_and_relay(self) -> None:
        """An expired tombstone yields step-1 purge: chunks gone, entry pruned."""
        local = self.tmpdir / "old.txt"
        local.write_bytes(b"content destined for the bin")

        manifest = _empty_manifest()
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
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="old.txt",
                author_device_id=AUTHOR,
            )
            after_delete = delete_file(
                vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                remote_folder_id=DOCS_ID, remote_path="old.txt",
                author_device_id=AUTHOR,
                deleted_at="2026-04-01T10:00:00.000Z",  # > 30 days ago
            )
            chunks_before = set(relay.chunks)
            self.assertGreater(len(chunks_before), 0)

            result = eviction_pass(
                vault=vault, relay=relay, manifest=after_delete,
                author_device_id=AUTHOR,
                target_bytes_to_free=0,  # housekeeping mode — stage 1 only
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        self.assertEqual(len(result.stages), 1)
        self.assertEqual(result.stages[0].event, "vault.eviction.tombstone_purged_expired")
        self.assertGreater(result.bytes_freed, 0)
        # Chunks physically gone from the relay.
        for cid in chunks_before:
            self.assertNotIn(cid, relay.chunks)
        # Manifest no longer references the dropped entry.
        self.assertIsNone(_entry_in_unified(result.manifest, DOCS_ID, "old.txt"))

    def test_housekeeping_does_not_force_purge_unexpired_tombstones(self) -> None:
        local = self.tmpdir / "fresh.txt"
        local.write_bytes(b"content within retention")

        manifest = _empty_manifest()
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
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="fresh.txt",
                author_device_id=AUTHOR,
            )
            after_delete = delete_file(
                vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                remote_folder_id=DOCS_ID, remote_path="fresh.txt",
                author_device_id=AUTHOR,
                deleted_at="2026-05-01T10:00:00.000Z",
            )
            result = eviction_pass(
                vault=vault, relay=relay, manifest=after_delete,
                author_device_id=AUTHOR,
                target_bytes_to_free=0,
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        self.assertEqual(result.bytes_freed, 0)
        self.assertEqual(result.stages, [])
        # Tombstone still present (chunks retained for restore).
        entry = _entry_in_unified(result.manifest, DOCS_ID, "fresh.txt")
        self.assertTrue(entry["deleted"])
        self.assertGreater(len(relay.chunks), 0)

    def test_force_purge_runs_destructive_loop_when_target_unmet_after_stage1(self) -> None:
        # Two tombstones: one expired (housekeeping picks it up), one
        # fresh (destructive loop must drain it).
        manifest = _empty_manifest()
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
            for path, deleted_at in [
                ("expired.txt", "2026-04-01T10:00:00.000Z"),
                ("fresh.txt", "2026-05-03T10:00:00.000Z"),
            ]:
                local = self.tmpdir / path
                local.write_bytes(f"content for {path}".encode("utf-8"))
                head = _decrypt_current_manifest(vault, relay) if relay.root_envelope else manifest
                uploaded = upload_file(
                    vault=vault, relay=relay, manifest=head, local_path=local,
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR,
                )
                delete_file(
                    vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR, deleted_at=deleted_at,
                )

            current = _decrypt_current_manifest(vault, relay)
            # Set a target larger than what stage 1 can free.
            target = sum(len(v) for v in relay.chunks.values()) + 1
            result = eviction_pass(
                vault=vault, relay=relay, manifest=current,
                author_device_id=AUTHOR,
                target_bytes_to_free=target,
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        events = [stage.event for stage in result.stages]
        self.assertIn("vault.eviction.tombstone_purged_expired", events)
        self.assertIn("vault.eviction.auto_purged_oldest", events)
        # Both tombstones are gone from the manifest.
        self.assertIsNone(_entry_in_unified(result.manifest, DOCS_ID, "expired.txt"))
        self.assertIsNone(_entry_in_unified(result.manifest, DOCS_ID, "fresh.txt"))

    def test_force_purge_walks_oldest_version_when_only_old_versions_remain(self) -> None:
        """A multi-version live file → destructive loop evicts oldest version."""
        local = self.tmpdir / "doc.txt"
        local.write_bytes(b"v1 content")

        manifest = _empty_manifest()
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
            v1 = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="doc.txt",
                author_device_id=AUTHOR,
                created_at="2026-04-01T10:00:00.000Z",
            )
            local.write_bytes(b"v2 content - distinct bytes here")
            v2 = upload_file(
                vault=vault, relay=relay, manifest=assemble_unified_manifest(v1.root, {v1.remote_folder_id: v1.shard}), local_path=local,
                remote_folder_id=DOCS_ID, remote_path="doc.txt",
                author_device_id=AUTHOR,
                created_at="2026-05-01T10:00:00.000Z",
            )

            current = _decrypt_current_manifest(vault, relay)
            target = sum(len(v) for v in relay.chunks.values())  # demand all bytes
            result = eviction_pass(
                vault=vault, relay=relay, manifest=current,
                author_device_id=AUTHOR,
                target_bytes_to_free=target,
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        events = [stage.event for stage in result.stages]
        self.assertIn("vault.eviction.auto_purged_oldest", events)
        # Live entry survives but only one version remains.
        entry = _entry_in_unified(result.manifest, DOCS_ID, "doc.txt")
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry["versions"]), 1)
        # No more candidates may or may not have been hit depending on
        # the byte target; if hit, the §D2 step-4 banner triggers.
        if result.no_more_candidates:
            self.assertGreater(result.bytes_freed, 0)

    def test_no_more_candidates_when_only_current_files_remain(self) -> None:
        """§D2 step 4: live current files only → can't free more, banner must surface."""
        local = self.tmpdir / "untouchable.txt"
        local.write_bytes(b"only current - cannot evict")

        manifest = _empty_manifest()
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
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="untouchable.txt",
                author_device_id=AUTHOR,
            )
            result = eviction_pass(
                vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                author_device_id=AUTHOR,
                target_bytes_to_free=10_000,
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        self.assertTrue(result.no_more_candidates)
        self.assertEqual(result.bytes_freed, 0)
        # Live file untouched.
        entry = _entry_in_unified(result.manifest, DOCS_ID, "untouchable.txt")
        self.assertIsNotNone(entry)
        self.assertFalse(entry["deleted"])
        self.assertGreater(len(relay.chunks), 0)

    def test_eviction_recovers_after_partial_mid_stage_crash(self) -> None:
        """Phase H step 7d crash-recovery: a prior eviction ran
        ``gc_execute`` (chunks deleted server-side) but crashed before
        publishing the shard cleanup. The next run sees
        ``safe_to_delete=[]`` but ``already_deleted_chunk_ids`` non-empty
        for the stale tombstone entries, skips ``gc_execute``, and runs
        shard cleanup to drop the entries.
        """
        local = self.tmpdir / "stranded.txt"
        local.write_bytes(b"content that will be stranded after crash")

        manifest = _empty_manifest()
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
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="stranded.txt",
                author_device_id=AUTHOR,
            )
            after_delete = delete_file(
                vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                remote_folder_id=DOCS_ID, remote_path="stranded.txt",
                author_device_id=AUTHOR,
                deleted_at="2026-04-01T10:00:00.000Z",  # > 30 days ago
            )

            # Simulate the prior crash: a previous eviction ran
            # ``gc_execute`` (chunks gone server-side) but crashed before
            # the shard publish, so the tombstone entry is still in the
            # shard pointing at deleted chunks.
            chunks_before = set(relay.chunks)
            self.assertGreater(len(chunks_before), 0)
            for cid in chunks_before:
                relay.chunks.pop(cid, None)

            # Count gc_execute invocations to confirm the recovery path
            # doesn't re-run it.
            original_gc_execute = relay.gc_execute
            gc_execute_call_count = [0]

            def counting_gc_execute(*args, **kwargs):
                gc_execute_call_count[0] += 1
                return original_gc_execute(*args, **kwargs)

            relay.gc_execute = counting_gc_execute  # type: ignore[method-assign]

            result = eviction_pass(
                vault=vault, relay=relay, manifest=after_delete,
                author_device_id=AUTHOR,
                target_bytes_to_free=0,
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        # Shard cleanup ran without re-running gc_execute.
        self.assertEqual(gc_execute_call_count[0], 0)
        # Manifest no longer references the stranded entry.
        self.assertIsNone(_entry_in_unified(result.manifest, DOCS_ID, "stranded.txt"))

    def test_stage_purposes_match_destructiveness(self) -> None:
        """ADR 2026-05-18: stage 1 sends purpose='sync'; the destructive
        loop sends purpose='forced_eviction' so the relay gates it on
        role=admin. Pinning the recorded purposes ensures a refactor
        can't silently relabel a hard-purge as housekeeping (a
        compromised sync-only device must not be able to wipe data
        inside the 30-day grace window).
        """
        manifest = _empty_manifest()
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
            for path, deleted_at in [
                ("expired.txt", "2026-04-01T10:00:00.000Z"),
                ("fresh.txt", "2026-05-03T10:00:00.000Z"),
            ]:
                local = self.tmpdir / path
                local.write_bytes(f"content for {path}".encode("utf-8"))
                head = _decrypt_current_manifest(vault, relay) if relay.root_envelope else manifest
                uploaded = upload_file(
                    vault=vault, relay=relay, manifest=head, local_path=local,
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR,
                )
                delete_file(
                    vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR, deleted_at=deleted_at,
                )

            current = _decrypt_current_manifest(vault, relay)
            target = sum(len(v) for v in relay.chunks.values()) + 1
            eviction_pass(
                vault=vault, relay=relay, manifest=current,
                author_device_id=AUTHOR,
                target_bytes_to_free=target,
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        purposes = [plan["purpose"] for plan in relay.gc_plans.values()]
        # Stage 1 (expired tombstone) — sync.
        self.assertIn("sync", purposes)
        # Destructive loop (unexpired tombstone / oldest version) — forced_eviction.
        self.assertIn("forced_eviction", purposes)
        sync_plans = [p for p in purposes if p == "sync"]
        forced_plans = [p for p in purposes if p == "forced_eviction"]
        self.assertGreaterEqual(len(sync_plans), 1)
        self.assertGreaterEqual(len(forced_plans), 1)

    def test_auto_purge_drains_oldest_until_upload_fits(self) -> None:
        """v1 ADR: the destructive loop walks the age-ordered iterator,
        dropping the oldest candidate until ``target_bytes_to_free`` is
        met. With two unexpired tombstones, the older one is purged
        first; the loop stops once the target is satisfied.
        """
        manifest = _empty_manifest()
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
            chunk_sizes: dict[str, int] = {}
            for idx, (path, deleted_at) in enumerate([
                ("oldest.txt", "2026-05-01T10:00:00.000Z"),
                ("middle.txt", "2026-05-02T10:00:00.000Z"),
                ("newest.txt", "2026-05-03T10:00:00.000Z"),
            ]):
                local = self.tmpdir / path
                local.write_bytes(f"content for {path} #{idx}".encode("utf-8"))
                head = _decrypt_current_manifest(vault, relay) if relay.root_envelope else manifest
                uploaded = upload_file(
                    vault=vault, relay=relay, manifest=head, local_path=local,
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR,
                )
                delete_file(
                    vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR, deleted_at=deleted_at,
                )
                chunk_sizes[path] = sum(
                    len(v) for k, v in relay.chunks.items()
                    if any(k == c["chunk_id"] for c in uploaded.shard.get("entries", [])
                           for v_ in c.get("versions", []) or [] for c in v_.get("chunks", []) or [])
                )

            # Target = just the oldest tombstone's bytes. Loop should
            # drain `oldest.txt`, hit the target, stop. The two newer
            # tombstones must still be present.
            relay_bytes_total = sum(len(v) for v in relay.chunks.values())
            target = 1  # any positive amount = trigger the destructive loop
            result = eviction_pass(
                vault=vault, relay=relay,
                manifest=_decrypt_current_manifest(vault, relay),
                author_device_id=AUTHOR,
                target_bytes_to_free=target,
                now_iso="2026-05-10T12:00:00.000Z",
            )
        finally:
            vault.close()

        # Loop ran at least once; oldest was purged first.
        events = [stage.event for stage in result.stages]
        self.assertIn("vault.eviction.auto_purged_oldest", events)
        self.assertIsNone(_entry_in_unified(result.manifest, DOCS_ID, "oldest.txt"))
        # Loop stopped at the boundary — newer tombstones intact.
        self.assertIsNotNone(_entry_in_unified(result.manifest, DOCS_ID, "newest.txt"))
        self.assertGreater(result.bytes_freed, 0)

    def test_auto_purge_excludes_latest_version(self) -> None:
        """v1 ADR: the destructive iterator must never drop the only
        live version of a file. With a single-version live file +
        target > 0, the loop exhausts immediately with
        ``no_more_candidates=True``.
        """
        local = self.tmpdir / "single-version.txt"
        local.write_bytes(b"only one version exists for this entry")

        manifest = _empty_manifest()
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
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="single-version.txt",
                author_device_id=AUTHOR,
            )
            result = eviction_pass(
                vault=vault, relay=relay,
                manifest=assemble_unified_manifest(
                    uploaded.root, {uploaded.remote_folder_id: uploaded.shard},
                ),
                author_device_id=AUTHOR,
                target_bytes_to_free=10_000,
                now_iso="2026-05-10T12:00:00.000Z",
            )
        finally:
            vault.close()

        self.assertTrue(result.no_more_candidates)
        self.assertEqual(result.bytes_freed, 0)
        # The single live version is intact.
        entry = _entry_in_unified(result.manifest, DOCS_ID, "single-version.txt")
        self.assertIsNotNone(entry)
        self.assertFalse(entry["deleted"])
        self.assertEqual(len(entry["versions"]), 1)

    def test_alarm_mode_emits_alarm_purged_oldest_event(self) -> None:
        """v1 ADR: ``mode='alarm'`` swaps the destructive-loop event
        from ``vault.eviction.auto_purged_oldest`` to
        ``vault.eviction.alarm_purged_oldest`` so audit logs distinguish
        "fit an upload" from "post-shrink cleanup."
        """
        local = self.tmpdir / "trash.txt"
        local.write_bytes(b"unexpired tombstone bytes")

        manifest = _empty_manifest()
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
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="trash.txt",
                author_device_id=AUTHOR,
            )
            delete_file(
                vault=vault, relay=relay, manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                remote_folder_id=DOCS_ID, remote_path="trash.txt",
                author_device_id=AUTHOR,
                deleted_at="2026-05-03T10:00:00.000Z",
            )

            result = eviction_pass(
                vault=vault, relay=relay,
                manifest=_decrypt_current_manifest(vault, relay),
                author_device_id=AUTHOR,
                target_bytes_to_free=1,
                mode="alarm",
                now_iso="2026-05-10T12:00:00.000Z",
            )
        finally:
            vault.close()

        events = [stage.event for stage in result.stages]
        self.assertIn("vault.eviction.alarm_purged_oldest", events)
        self.assertNotIn("vault.eviction.auto_purged_oldest", events)

    def test_destructive_loop_interleaves_tombstones_and_versions_oldest_first(self) -> None:
        """v1 ADR: the merged iterator considers both candidate sources
        together, sorted oldest-first. A 6-month-old non-latest version
        of a still-live file is purged before a 3-day-old tombstone.
        """
        manifest = _empty_manifest()
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

            # Live file with two versions: v1 from 2025-11, v2 (latest) from 2026-05.
            old_doc = self.tmpdir / "stale-version.txt"
            old_doc.write_bytes(b"v1 of stale-version content")
            v1 = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=old_doc,
                remote_folder_id=DOCS_ID, remote_path="stale-version.txt",
                author_device_id=AUTHOR,
                created_at="2025-11-01T10:00:00.000Z",
            )
            old_doc.write_bytes(b"v2 of stale-version content - latest")
            upload_file(
                vault=vault, relay=relay,
                manifest=assemble_unified_manifest(v1.root, {v1.remote_folder_id: v1.shard}),
                local_path=old_doc,
                remote_folder_id=DOCS_ID, remote_path="stale-version.txt",
                author_device_id=AUTHOR,
                created_at="2026-05-01T10:00:00.000Z",
            )

            # Recent tombstone from 2026-05-07 (newer than v1's 2025-11).
            recent_trash = self.tmpdir / "recent-trash.txt"
            recent_trash.write_bytes(b"recently deleted")
            uploaded = upload_file(
                vault=vault, relay=relay,
                manifest=_decrypt_current_manifest(vault, relay),
                local_path=recent_trash,
                remote_folder_id=DOCS_ID, remote_path="recent-trash.txt",
                author_device_id=AUTHOR,
            )
            delete_file(
                vault=vault, relay=relay,
                manifest=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                remote_folder_id=DOCS_ID, remote_path="recent-trash.txt",
                author_device_id=AUTHOR,
                deleted_at="2026-05-07T10:00:00.000Z",
            )

            # Free just enough for one candidate. The 2025-11 version is
            # older than the 2026-05-07 tombstone, so it must go first.
            result = eviction_pass(
                vault=vault, relay=relay,
                manifest=_decrypt_current_manifest(vault, relay),
                author_device_id=AUTHOR,
                target_bytes_to_free=1,
                now_iso="2026-05-10T12:00:00.000Z",
            )
        finally:
            vault.close()

        # First (and only) destructive iteration dropped the old version,
        # NOT the recent tombstone.
        entry = _entry_in_unified(result.manifest, DOCS_ID, "stale-version.txt")
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry["versions"]), 1, "old version should be purged")
        trash_entry = _entry_in_unified(result.manifest, DOCS_ID, "recent-trash.txt")
        self.assertIsNotNone(trash_entry)
        self.assertTrue(trash_entry["deleted"], "newer tombstone should still be present")


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID,
        master_key=MASTER_KEY,
        recovery_secret=None,
        vault_access_secret="vault-secret",
        header_revision=1,
        manifest_revision=1,
        manifest_ciphertext=b"",
        crypto=DefaultVaultCrypto,
    )


def _empty_manifest() -> dict:
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=1,
        parent_root_revision=0,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
            )
        ],
    )
    shard = make_folder_shard(
        vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
        shard_revision=1, parent_shard_revision=0,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        entries=[],
    )
    return assemble_unified_manifest(root, {DOCS_ID: shard})


def _entry_in_unified(manifest: dict, remote_folder_id: str, path: str) -> dict | None:
    """Look up a file entry in the unified manifest's folder."""
    folder = next(
        (f for f in manifest.get("remote_folders", []) or [] if f.get("remote_folder_id") == remote_folder_id),
        None,
    )
    if folder is None:
        return None
    return find_file_entry_in_shard(folder, path)


def _decrypt_current_manifest(vault, relay) -> dict:
    """Read the post-publish unified view by assembling root +
    per-folder shards via the sharded fetch API."""
    return vault.fetch_unified_manifest(relay)


if __name__ == "__main__":
    unittest.main()
