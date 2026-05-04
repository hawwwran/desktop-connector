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
from src.vault_binding_sync import (  # noqa: E402
    SyncOpOutcome,
    run_backup_only_cycle,
)
from src.vault_bindings import VaultBindingsStore, VaultLocalEntry  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
)
from src.vault_upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import FakeUploadRelay  # noqa: E402


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
        # FakeUploadRelay stores current_revision from the manifest but
        # leaves current_envelope = b"" — fetch_manifest would explode.
        # Lower current_revision so parent_revision=0 ⇒ CAS passes, then
        # publish to populate the encrypted envelope.
        relay.current_revision = int(manifest.get("parent_revision", 0))
        vault = _vault()
        try:
            vault.publish_manifest(relay, manifest)
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
        return res.manifest

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
        # Revision is at least the remote head's revision (advanced or matched).
        rebound = self.store.get_binding(binding.binding_id)
        self.assertEqual(
            rebound.last_synced_revision, int(manifest["revision"]),
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

        # Remote manifest now has the entry tombstoned.
        from src.vault_browser_model import decrypt_manifest
        observer = _vault()
        try:
            current = decrypt_manifest(observer, relay.current_envelope)
        finally:
            observer.close()
        folder = next(
            f for f in current["remote_folders"]
            if f["remote_folder_id"] == DOCS_ID
        )
        target = next(e for e in folder["entries"] if e["path"] == "goner.txt")
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


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1, manifest_revision=1,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )


if __name__ == "__main__":
    unittest.main()
