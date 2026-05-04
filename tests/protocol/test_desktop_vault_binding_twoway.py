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
from src.vault_binding_twoway import run_two_way_cycle  # noqa: E402
from src.vault_bindings import VaultBindingsStore, VaultLocalEntry  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_crypto import (  # noqa: E402
    DefaultVaultCrypto,
    derive_content_fingerprint_key, make_content_fingerprint,
)
from src.vault_manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
)
from src.vault_upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR, DOCS_ID, MASTER_KEY, VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import FakeUploadRelay  # noqa: E402


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

    def _tombstone_remote_file(
        self,
        relay: FakeUploadRelay,
        manifest: dict,
        *,
        path: str,
    ) -> dict:
        from src.vault_manifest import (
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
            vault.publish_manifest(relay, next_manifest)
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
        # devices can see it. Decrypt and look for it.
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
        remote_paths = [e["path"] for e in folder["entries"]]
        self.assertIn(conflict_names[0], remote_paths,
                      f"conflict copy not present in remote: {remote_paths}")

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
        entry = next(
            e for e in folder["entries"] if e["path"] == "ledger.txt"
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

        from src.vault_manifest import find_file_entry
        from src.vault_browser_model import decrypt_manifest
        observer = _vault()
        try:
            current = decrypt_manifest(observer, relay.current_envelope)
        finally:
            observer.close()
        self.assertIsNotNone(find_file_entry(current, DOCS_ID, "fresh.txt"))

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


if __name__ == "__main__":
    unittest.main()
