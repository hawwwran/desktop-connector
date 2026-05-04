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
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_delete import delete_file  # noqa: E402
from src.vault_eviction import eviction_pass  # noqa: E402
from src.vault_manifest import (  # noqa: E402
    find_file_entry,
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
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="old.txt",
                author_device_id=AUTHOR,
            )
            after_delete = delete_file(
                vault=vault, relay=relay, manifest=uploaded.manifest,
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
        self.assertIsNone(find_file_entry(result.manifest, DOCS_ID, "old.txt"))

    def test_housekeeping_does_not_force_purge_unexpired_tombstones(self) -> None:
        local = self.tmpdir / "fresh.txt"
        local.write_bytes(b"content within retention")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="fresh.txt",
                author_device_id=AUTHOR,
            )
            after_delete = delete_file(
                vault=vault, relay=relay, manifest=uploaded.manifest,
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
        entry = find_file_entry(result.manifest, DOCS_ID, "fresh.txt")
        self.assertTrue(entry["deleted"])
        self.assertGreater(len(relay.chunks), 0)

    def test_force_purge_runs_stage2_when_target_unmet_after_stage1(self) -> None:
        # Two tombstones: one expired (for stage 1), one fresh (forces stage 2).
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            for path, deleted_at in [
                ("expired.txt", "2026-04-01T10:00:00.000Z"),
                ("fresh.txt", "2026-05-03T10:00:00.000Z"),
            ]:
                local = self.tmpdir / path
                local.write_bytes(f"content for {path}".encode("utf-8"))
                head = _decrypt_current_manifest(vault, relay) if relay.current_envelope else manifest
                uploaded = upload_file(
                    vault=vault, relay=relay, manifest=head, local_path=local,
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR,
                )
                delete_file(
                    vault=vault, relay=relay, manifest=uploaded.manifest,
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
        self.assertIn("vault.eviction.tombstone_purged_early", events)
        # Both tombstones are gone from the manifest.
        self.assertIsNone(find_file_entry(result.manifest, DOCS_ID, "expired.txt"))
        self.assertIsNone(find_file_entry(result.manifest, DOCS_ID, "fresh.txt"))

    def test_force_purge_runs_stage3_when_only_old_versions_remain(self) -> None:
        """A multi-version live file → stage 3 evicts oldest version."""
        local = self.tmpdir / "doc.txt"
        local.write_bytes(b"v1 content")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            v1 = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="doc.txt",
                author_device_id=AUTHOR,
                created_at="2026-04-01T10:00:00.000Z",
            )
            local.write_bytes(b"v2 content - distinct bytes here")
            upload_file(
                vault=vault, relay=relay, manifest=v1.manifest, local_path=local,
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
        self.assertIn("vault.eviction.version_purged", events)
        # Live entry survives but only one version remains.
        entry = find_file_entry(result.manifest, DOCS_ID, "doc.txt")
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
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="untouchable.txt",
                author_device_id=AUTHOR,
            )
            result = eviction_pass(
                vault=vault, relay=relay, manifest=uploaded.manifest,
                author_device_id=AUTHOR,
                target_bytes_to_free=10_000,
                now_iso="2026-05-04T12:00:00.000Z",
            )
        finally:
            vault.close()

        self.assertTrue(result.no_more_candidates)
        self.assertEqual(result.bytes_freed, 0)
        # Live file untouched.
        entry = find_file_entry(result.manifest, DOCS_ID, "untouchable.txt")
        self.assertIsNotNone(entry)
        self.assertFalse(entry["deleted"])
        self.assertGreater(len(relay.chunks), 0)


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
    return make_manifest(
        vault_id=VAULT_ID,
        revision=1,
        parent_revision=0,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            )
        ],
    )


def _decrypt_current_manifest(vault, relay) -> dict:
    from src.vault_browser_model import decrypt_manifest as _decrypt
    return _decrypt(vault, relay.current_envelope)


if __name__ == "__main__":
    unittest.main()
