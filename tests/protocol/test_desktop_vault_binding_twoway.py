"""T12.1 — Two-way sync cycle: remote→local apply + local→remote drain."""

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
from src.vault.binding.twoway import run_two_way_cycle  # noqa: E402
from src.vault.binding.bindings import VaultBindingsStore, VaultLocalEntry  # noqa: E402
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402
from src.vault.crypto import (  # noqa: E402
    DefaultVaultCrypto,
    derive_content_fingerprint_key, make_content_fingerprint,
)
from src.vault.manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
)
from src.vault.upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR, DOCS_ID, MASTER_KEY, VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import (  # noqa: E402
    FakeUploadRelay,
    seed_sharded_state_from_manifest,
)


VAULT_ACCESS_SECRET = "vault-secret"
THIS_DEVICE = "abcdef0123456789abcdef0123456789"
DEVICE_NAME = "Test Desktop"


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID,
        master_key=MASTER_KEY,
        recovery_secret=None,
        vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1,
        manifest_revision=1,
        manifest_ciphertext=b"",
        crypto=DefaultVaultCrypto,
    )


def _keyed_fingerprint(content: bytes) -> str:
    """Compute the keyed fingerprint the manifest stores for ``content``."""
    import hashlib
    sha = hashlib.sha256(content).digest()
    return make_content_fingerprint(
        derive_content_fingerprint_key(MASTER_KEY), sha,
    )


class TwoWayCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_twoway_test_"))
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
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                ),
            ],
        )
        relay = FakeUploadRelay(manifest=manifest)
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
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
            seed_sharded_state_from_manifest(vault, relay, res.manifest)
        finally:
            vault.close()
        return res.manifest

    def _tombstone_remote_file(
        self,
        relay: FakeUploadRelay,
        manifest: dict,
        *,
        path: str,
    ) -> dict:
        from src.vault.manifest import (
            normalize_manifest_path, tombstone_file_entry,
        )
        normalized = normalize_manifest_path(path)
        next_manifest = tombstone_file_entry(
            manifest, remote_folder_id=DOCS_ID, path=normalized,
            deleted_at="2026-05-04T13:00:00.000Z",
            author_device_id=AUTHOR,
        )
        next_manifest["revision"] = int(manifest.get("revision", 0)) + 1
        next_manifest["parent_revision"] = int(manifest.get("revision", 0))
        next_manifest["created_at"] = "2026-05-04T13:00:00.000Z"
        next_manifest["author_device_id"] = AUTHOR
        vault = _vault()
        try:
            # Phase H step 4: upload_file no longer mirrors to legacy,
            # so the legacy current_revision has drifted from the
            # caller-supplied manifest revision. Reset before the CAS
            # publish so the chain stays coherent for the next call.
            relay.current_revision = int(next_manifest["parent_revision"])
            vault.publish_manifest(relay, next_manifest)
            seed_sharded_state_from_manifest(vault, relay, next_manifest)
        finally:
            vault.close()
        return next_manifest

    def _make_two_way_binding(self, *, last_revision: int):
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id,
            state="bound",
            sync_mode="two-way",
            last_synced_revision=last_revision,
        )
        return self.store.get_binding(binding.binding_id)

    def _seed_local_entry(self, binding_id: str, *, relative: str, content: bytes, revision: int) -> None:
        target = self.local_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding_id,
            relative_path=relative,
            content_fingerprint=_keyed_fingerprint(content),
            size_bytes=len(content),
            mtime_ns=target.stat().st_mtime_ns,
            last_synced_revision=revision,
        ))

    # ------------------------------------------------------------------
    # T12.1.A — remote upsert flows down to local
    # ------------------------------------------------------------------

    def test_remote_change_propagates_to_local_within_one_cycle(self) -> None:
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="alpha.txt",
            content=b"published from another device",
        )
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]) - 1,
        )

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        target = self.local_root / "alpha.txt"
        self.assertTrue(target.is_file(), "remote file should land locally")
        self.assertEqual(target.read_bytes(), b"published from another device")
        self.assertEqual(result.failed_count, 0)
        # local-entry row stamped with the remote fingerprint.
        entry = self.store.get_local_entry(binding.binding_id, "alpha.txt")
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.content_fingerprint,
            _keyed_fingerprint(b"published from another device"),
        )

    # ------------------------------------------------------------------
    # T12.1.B — remote tombstone trashes the unmodified local copy
    # ------------------------------------------------------------------

    def test_remote_tombstone_trashes_unmodified_local_file(self) -> None:
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="goner.txt", content=b"goodbye",
        )
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]),
        )
        # Pretend we already have it locally at the right fingerprint.
        self._seed_local_entry(
            binding.binding_id, relative="goner.txt",
            content=b"goodbye", revision=int(manifest["revision"]),
        )
        # Remote tombstones it.
        manifest = self._tombstone_remote_file(relay, manifest, path="goner.txt")

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        # The local file is gone (trash or unlink fallback) and the
        # local-entry row was cleared. Either way: not on disk.
        self.assertFalse((self.local_root / "goner.txt").is_file())
        self.assertIsNone(
            self.store.get_local_entry(binding.binding_id, "goner.txt"),
        )
        # No failures.
        self.assertEqual(result.failed_count, 0)

    # ------------------------------------------------------------------
    # T12.1.C — concurrent edit/edit produces a §A20 conflict copy + both
    # versions land in remote `versions[]` after the upload pass
    # ------------------------------------------------------------------

    def test_concurrent_edit_keeps_both_via_conflict_copy(self) -> None:
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="shared.txt", content=b"v1",
        )
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]),
        )
        self._seed_local_entry(
            binding.binding_id, relative="shared.txt",
            content=b"v1", revision=int(manifest["revision"]),
        )

        # Remote: another device publishes v2.
        manifest = self._seed_remote_file(
            relay, manifest, path="shared.txt", content=b"v2-from-remote",
        )
        # Local: the user concurrently edits to a different v2.
        (self.local_root / "shared.txt").write_bytes(b"v2-from-local")

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        # The remote bytes land at the original path.
        target = self.local_root / "shared.txt"
        self.assertEqual(target.read_bytes(), b"v2-from-remote")

        # The local pre-edit bytes survive at a §A20 conflict path.
        siblings = sorted(p.name for p in self.local_root.iterdir())
        conflict_names = [
            n for n in siblings
            if n.startswith("shared (conflict synced ") and n.endswith(".txt")
        ]
        self.assertEqual(len(conflict_names), 1, siblings)
        conflict_path = self.local_root / conflict_names[0]
        self.assertEqual(conflict_path.read_bytes(), b"v2-from-local")
        self.assertEqual(result.failed_count, 0)

        # And the cycle pushed the conflict copy back to remote so other
        # devices can see it. Decrypt the sharded view and look for it.
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        remote_paths = [e["path"] for e in shard["entries"]]
        self.assertIn(conflict_names[0], remote_paths,
                      f"conflict copy not present in remote: {remote_paths}")

    # ------------------------------------------------------------------
    # F-Y31 — remote tombstone with orphan local file: warn, don't trash
    # ------------------------------------------------------------------

    def test_remote_tombstone_with_orphan_local_file_warns_and_keeps_local(self) -> None:
        """A local file that exists on disk at a tombstoned path but has
        no ``vault_local_entries`` row (e.g. baseline missed it during a
        transient tombstone-then-restore) must NOT be auto-trashed —
        the engine can't tell whether the user has unsaved local-only
        edits there. Surface the orphan via warning so an operator can
        reconcile.
        """
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="orphan.txt", content=b"original",
        )
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]) - 1,
        )
        # Plant a local file WITHOUT a corresponding local-entry row.
        target = self.local_root / "orphan.txt"
        target.write_bytes(b"locally-present-but-unknown")
        self.assertIsNone(
            self.store.get_local_entry(binding.binding_id, "orphan.txt")
        )
        manifest = self._tombstone_remote_file(
            relay, manifest, path="orphan.txt",
        )

        vault = _vault()
        try:
            with self.assertLogs(
                "src.vault.binding.twoway", level="WARNING"
            ) as cm:
                run_two_way_cycle(
                    vault=vault, relay=relay,
                    store=self.store, binding=binding,
                    author_device_id=THIS_DEVICE,
                    device_name=DEVICE_NAME,
                )
        finally:
            vault.close()

        # File survives untouched — the engine can't tell what to do.
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"locally-present-but-unknown")
        # No phantom local-entry row was created either.
        self.assertIsNone(
            self.store.get_local_entry(binding.binding_id, "orphan.txt")
        )
        # And the warning was emitted so an operator can spot the orphan.
        self.assertTrue(
            any(
                "twoway_orphan_local_for_remote_tombstone" in line
                and "orphan.txt" in line
                for line in cm.output
            ),
            cm.output,
        )

    # ------------------------------------------------------------------
    # T12.1.D — remote tombstone vs local-modified: keep local + push it back
    # ------------------------------------------------------------------

    def test_remote_tombstone_with_local_modifications_keeps_local(self) -> None:
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="ledger.txt", content=b"original",
        )
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]),
        )
        self._seed_local_entry(
            binding.binding_id, relative="ledger.txt",
            content=b"original", revision=int(manifest["revision"]),
        )
        # User edits locally before noticing the remote tombstone.
        (self.local_root / "ledger.txt").write_bytes(b"locally-modified")
        # Remote tombstones the same path.
        manifest = self._tombstone_remote_file(relay, manifest, path="ledger.txt")

        vault = _vault()
        try:
            run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        # Local survives untouched (the user's edits are not lost).
        target = self.local_root / "ledger.txt"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"locally-modified")

        # And the modification flowed back to remote — the cycle should
        # have re-uploaded the file as a fresh version on top of the
        # tombstone.
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        entry = next(
            e for e in shard["entries"] if e["path"] == "ledger.txt"
        )
        # The post-cycle entry has at least 2 versions and is no longer
        # tombstoned — the local re-upload won.
        self.assertGreaterEqual(len(entry.get("versions", []) or []), 2)
        self.assertFalse(bool(entry.get("deleted")))

    # ------------------------------------------------------------------
    # T12.1.E — local upload still flows up (parity with backup-only)
    # ------------------------------------------------------------------

    def test_local_upload_drains_to_remote(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]),
        )
        # User adds a file; watcher would have enqueued an upload op.
        (self.local_root / "fresh.txt").write_bytes(b"new local file")
        self.store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="upload",
            relative_path="fresh.txt",
        )

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        upload_outcomes = [o for o in result.outcomes if o.op_type == "upload"]
        self.assertEqual(len(upload_outcomes), 1)
        self.assertEqual(upload_outcomes[0].status, "uploaded")
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])

        from src.vault.manifest import find_file_entry_in_shard
        observer = _vault()
        try:
            shard = observer.decrypt_shard_envelope(
                relay.shards[DOCS_ID]["envelope"], DOCS_ID,
            )
        finally:
            observer.close()
        self.assertIsNotNone(find_file_entry_in_shard(shard, "fresh.txt"))

    # ------------------------------------------------------------------
    # F-Y20 — ghost local-entries reaping
    # ------------------------------------------------------------------

    def test_ghost_row_reaped_when_local_file_also_gone(self) -> None:
        """Both the manifest entry AND the local file have vanished
        (the manifest's tombstone was server-side-purged after retention
        elapsed). The local-entries row is pure dead state and must be
        deleted so it doesn't accumulate forever.
        """
        relay, manifest = self._empty_remote()
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]),
        )
        # Pre-seed a local-entries row for a path that has never been
        # in this manifest. The local file does NOT exist on disk.
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path="purged.txt",
            content_fingerprint="ghost-fp",
            size_bytes=42,
            mtime_ns=1_700_000_000_000_000_000,
            last_synced_revision=int(manifest["revision"]),
        ))

        vault = _vault()
        try:
            run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        relatives = {
            e.relative_path
            for e in self.store.list_local_entries(binding.binding_id)
        }
        self.assertNotIn("purged.txt", relatives)

    def test_ghost_row_demoted_when_local_file_survives(self) -> None:
        """The manifest entry has been server-side-purged but the user
        still has the local file on disk. We don't delete the row —
        that would lose the watcher's mtime/size cache; instead we
        clear the fingerprint and reset ``last_synced_revision`` so
        the next watcher tick treats the file as a fresh upload
        candidate (the user can choose to back it up again).
        """
        relay, manifest = self._empty_remote()
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]),
        )
        # Pre-seed a local-entries row pointing at a real on-disk file
        # that the manifest doesn't carry.
        target = self.local_root / "kept_locally.txt"
        target.write_bytes(b"user kept this after server purged it")
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path="kept_locally.txt",
            content_fingerprint="stale-fp",
            size_bytes=target.stat().st_size,
            mtime_ns=target.stat().st_mtime_ns,
            last_synced_revision=int(manifest["revision"]),
        ))

        vault = _vault()
        try:
            run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        # Row survives, but is now flagged for re-upload (revision=0,
        # fingerprint cleared). Local file untouched.
        rows = {
            e.relative_path: e
            for e in self.store.list_local_entries(binding.binding_id)
        }
        self.assertIn("kept_locally.txt", rows)
        self.assertEqual(rows["kept_locally.txt"].last_synced_revision, 0)
        self.assertEqual(rows["kept_locally.txt"].content_fingerprint, "")
        self.assertTrue(target.is_file())

    def test_ghost_reaping_does_not_touch_visited_paths(self) -> None:
        """F-Y20 must only reap *unvisited* rows. A path that's still
        an active manifest entry — one the loop processed — must keep
        its row intact.
        """
        relay, manifest = self._empty_remote()
        manifest = self._seed_remote_file(
            relay, manifest, path="active.txt", content=b"active content",
        )
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]) - 1,
        )
        # Existing local row for the active path with up-to-date
        # fingerprint so no work is needed during the cycle.
        target = self.local_root / "active.txt"
        target.write_bytes(b"active content")
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path="active.txt",
            content_fingerprint=_keyed_fingerprint(b"active content"),
            size_bytes=target.stat().st_size,
            mtime_ns=target.stat().st_mtime_ns,
            last_synced_revision=int(manifest["revision"]),
        ))

        vault = _vault()
        try:
            run_two_way_cycle(
                vault=vault, relay=relay,
                store=self.store, binding=binding,
                author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        rows = {
            e.relative_path: e
            for e in self.store.list_local_entries(binding.binding_id)
        }
        self.assertIn("active.txt", rows)
        # Revision must NOT have been demoted — this row was visited.
        self.assertGreater(rows["active.txt"].last_synced_revision, 0)
        self.assertEqual(
            rows["active.txt"].content_fingerprint,
            _keyed_fingerprint(b"active content"),
        )

    # ------------------------------------------------------------------
    # Validation: only `bound` + sync_mode == 'two-way' is accepted
    # ------------------------------------------------------------------

    def test_backup_only_binding_refused_by_two_way_cycle(self) -> None:
        relay, manifest = self._empty_remote()
        # backup-only by default — different from `two-way`.
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
        vault = _vault()
        try:
            with self.assertRaises(ValueError):
                run_two_way_cycle(
                    vault=vault, relay=relay,
                    store=self.store, binding=binding,
                    author_device_id=THIS_DEVICE,
                    device_name=DEVICE_NAME,
                )
        finally:
            vault.close()

    def test_paused_binding_refused(self) -> None:
        relay, manifest = self._empty_remote()
        binding = self._make_two_way_binding(
            last_revision=int(manifest["revision"]),
        )
        self.store.update_binding_state(binding.binding_id, sync_mode="paused")
        binding = self.store.get_binding(binding.binding_id)
        vault = _vault()
        try:
            with self.assertRaises(ValueError):
                run_two_way_cycle(
                    vault=vault, relay=relay,
                    store=self.store, binding=binding,
                    author_device_id=THIS_DEVICE,
                    device_name=DEVICE_NAME,
                )
        finally:
            vault.close()


class TwoWayFetchManifestPerOpTests(unittest.TestCase):
    """SO-2 mirror of ``FetchManifestPerOpTests`` for ``run_two_way_cycle``.

    Two-way's Phase B drains the same pending-ops queue backup-only
    does, so the same fix applies: the loop must consume the manifest
    ``_execute_op`` returns rather than re-fetch the head after every
    successful op. If that regressed, this test counts the extra GETs
    and fails.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_twoway_so2_"))
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

    def _empty_remote(self) -> "_CountingTwoWayRelay":
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
        relay = _CountingTwoWayRelay(manifest=manifest)
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
        finally:
            vault.close()
        # Discard setup-time get_manifest calls so the assertions count
        # only Phase A + Phase B traffic.
        relay.get_manifest_count = 0
        return relay

    def _make_two_way_binding(self, *, last_revision: int):
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id,
            state="bound",
            sync_mode="two-way",
            last_synced_revision=last_revision,
        )
        return self.store.get_binding(binding.binding_id)

    def test_phase_b_does_not_fetch_per_successful_op(self) -> None:
        relay = self._empty_remote()
        binding = self._make_two_way_binding(last_revision=int(relay.current_revision))

        for i in range(5):
            path = f"tw{i}.txt"
            (self.local_root / path).write_bytes(f"payload-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 5)
        self.assertEqual(result.failed_count, 0)
        # Phase A fetches the head once. The convergence loop may
        # re-fetch once at end-of-iteration to observe any concurrent
        # writes. Both are bounded. Critically: there is NO per-op
        # GET inside the Phase B drain. The 5-op loop should
        # contribute zero additional fetches; the total stays small
        # (1 initial + 1 end-of-iter refresh = 2).
        #
        # Pre-SO-2 this would be ≥ 1 + N (= 6) because every
        # ``outcome.status in {"uploaded","deleted","failed"}`` triggered
        # ``head = vault.fetch_manifest(relay)`` inside the inner loop.
        self.assertLessEqual(
            relay.get_manifest_count, 2,
            "two-way Phase B is calling fetch_manifest per op; "
            f"saw {relay.get_manifest_count} fetches for 5 successful "
            "ops — SO-2 regression",
        )


class TwoWayBatchedPhaseBTests(unittest.TestCase):
    """SO-3 extension to two-way: Phase B drains pending ops in
    batches of K=50, not one publish per op. CAS conflicts abort the
    batch and the outer iteration loop's Phase A re-runs on the fresh
    head (which fires §D4 conflict-rename detection for any
    concurrent writer's changes) rather than blind-replaying.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_twoway_batched_"))
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

    def _empty_remote(self) -> "_TwoWayBatchProbeRelay":
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
        relay = _TwoWayBatchProbeRelay(manifest=manifest)
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
        finally:
            vault.close()
        relay.put_manifest_attempt_count = 0
        relay.published_manifests = []
        relay.published_shards = []
        relay.published_roots = []
        relay.shard_with_root_puts = 0
        return relay

    def _make_two_way_binding(self, *, last_revision: int):
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        self.store.update_binding_state(
            binding.binding_id,
            state="bound",
            sync_mode="two-way",
            last_synced_revision=last_revision,
        )
        return self.store.get_binding(binding.binding_id)

    def test_phase_b_5_ops_publish_in_one_batch(self) -> None:
        """5 uploads in a two-way cycle should produce one CAS publish
        (the K=50 default), not 5 single publishes."""
        relay = self._empty_remote()
        binding = self._make_two_way_binding(last_revision=int(relay.current_revision))

        for i in range(5):
            path = f"tw{i}.txt"
            (self.local_root / path).write_bytes(f"data-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 5)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(
            relay.put_manifest_attempt_count, 1,
            "two-way Phase B did per-op publishes instead of batching; "
            f"saw {relay.put_manifest_attempt_count} publish attempts "
            "for 5 ops — SO-3 two-way regression",
        )

    def test_phase_b_smaller_batch_size_splits_into_multiple_publishes(self) -> None:
        """7 ops with batch_size=3 → exactly 3 publishes (3 + 3 + 1
        cycle-end flush of the partial batch)."""
        relay = self._empty_remote()
        binding = self._make_two_way_binding(last_revision=int(relay.current_revision))

        for i in range(7):
            path = f"sm{i}.txt"
            (self.local_root / path).write_bytes(f"d-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
                batch_size=3,
            )
        finally:
            vault.close()

        self.assertEqual(result.succeeded_count, 7)
        self.assertEqual(relay.put_manifest_attempt_count, 3)

    def test_cas_conflict_aborts_batch_next_iteration_completes(self) -> None:
        """A CAS conflict on the two-way batch publish aborts (no
        retry-within-batch like backup-only would do). The outer
        iteration loop re-runs Phase A on the fresh head and Phase B
        re-tries the pending ops. With one injected conflict, the
        second iteration succeeds.
        """
        relay = self._empty_remote()
        binding = self._make_two_way_binding(last_revision=int(relay.current_revision))

        for i in range(3):
            path = f"cf{i}.txt"
            (self.local_root / path).write_bytes(f"c-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        # Inject one CAS conflict — first batch flush aborts, second
        # iteration's batch publishes successfully.
        relay.cas_conflicts_to_inject = 1

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        # Eventually all 3 ops uploaded — first iter's batch flushed
        # 3 "failed" outcomes, second iter's batch flushed 3
        # "uploaded" outcomes. Net succeeded_count = 3.
        uploaded = [o for o in result.outcomes if o.status == "uploaded"]
        failed = [o for o in result.outcomes if o.status == "failed"]
        self.assertEqual(len(uploaded), 3)
        # The 3 failed outcomes from the aborted first batch survive
        # in the outcomes list (they were emitted before the retry).
        self.assertEqual(len(failed), 3)
        # The pending-op rows are gone (the second iter's success
        # deleted them).
        self.assertEqual(self.store.list_pending_ops(binding.binding_id), [])
        # Two publish attempts: 1 conflict + 1 success.
        self.assertEqual(relay.put_manifest_attempt_count, 2)
        # Conflict budget exhausted.
        self.assertEqual(relay.cas_conflicts_to_inject, 0)
        # ``relay.published_shards`` only counts successful CAS,
        # so 1 entry (the retry).
        self.assertEqual(len(relay.published_shards), 1)

    def test_cas_conflict_persists_across_max_iterations_leaves_failed(self) -> None:
        """If the relay keeps conflicting every batch publish, the
        outer loop hits MAX_TWO_WAY_ITERATIONS and exits with the
        pending-ops still queued. Defends against an unbounded
        retry loop on a pathologically busy multi-device vault.
        """
        from src.vault.binding.twoway import MAX_TWO_WAY_ITERATIONS

        relay = self._empty_remote()
        binding = self._make_two_way_binding(last_revision=int(relay.current_revision))

        for i in range(3):
            path = f"perm{i}.txt"
            (self.local_root / path).write_bytes(f"p-{i}".encode())
            self.store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=path,
            )

        # Conflict every iteration — outer loop should bail at the
        # MAX_TWO_WAY_ITERATIONS cap.
        relay.cas_conflicts_to_inject = 99

        vault = _vault()
        try:
            result = run_two_way_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding, author_device_id=THIS_DEVICE,
                device_name=DEVICE_NAME,
            )
        finally:
            vault.close()

        # No ops succeeded — every batch CAS-conflicted.
        uploaded = [o for o in result.outcomes if o.status == "uploaded"]
        self.assertEqual(len(uploaded), 0)
        # Pending ops survive for the next sync cycle.
        self.assertEqual(len(self.store.list_pending_ops(binding.binding_id)), 3)
        # We attempted exactly MAX_TWO_WAY_ITERATIONS publishes
        # (one per iteration's batch flush).
        self.assertEqual(
            relay.put_manifest_attempt_count, MAX_TWO_WAY_ITERATIONS,
            f"expected {MAX_TWO_WAY_ITERATIONS} publish attempts (one "
            "per iteration), saw "
            f"{relay.put_manifest_attempt_count} — outer-loop cap "
            "is not gating the retries",
        )


class _CountingTwoWayRelay(FakeUploadRelay):
    """``FakeUploadRelay`` that counts ``get_manifest`` for SO-2 pinning."""

    def __init__(self, *, manifest: dict) -> None:
        super().__init__(manifest=manifest)
        self.get_manifest_count = 0

    def get_manifest(self, vault_id, vault_access_secret):
        self.get_manifest_count += 1
        return super().get_manifest(vault_id, vault_access_secret)


class _TwoWayBatchProbeRelay(FakeUploadRelay):
    """Relay that counts ``put_manifest`` attempts and can inject CAS
    conflicts on demand, scoped to the two-way batched-publish tests.
    Same shape as ``BatchProbeRelay`` in the batched-publish test
    file; duplicated here rather than imported across test modules.
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
        import base64

        self.put_manifest_attempt_count += 1
        if self.cas_conflicts_to_inject > 0:
            self.cas_conflicts_to_inject -= 1
            from src.vault.relay_errors import VaultCASConflictError
            raise VaultCASConflictError({
                "code": "vault_manifest_conflict",
                "message": "injected CAS conflict (two-way SO-3 test)",
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
        import base64

        self.put_manifest_attempt_count += 1
        if self.cas_conflicts_to_inject > 0:
            self.cas_conflicts_to_inject -= 1
            from src.vault.relay_errors import VaultCASConflictError
            current = self.shards.get(remote_folder_id, {})
            current_envelope = current.get("envelope", b"")
            raise VaultCASConflictError({
                "code": "vault_shard_conflict",
                "message": "injected shard CAS conflict (two-way SO-3 test)",
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
