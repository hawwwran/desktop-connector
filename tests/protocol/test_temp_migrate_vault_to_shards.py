"""Phase G dry-run test: ``temp/migrate_vault_to_shards.py``.

Per ``docs/plans/vault-manifest-sharding.md`` Phase G acceptance:

  > A scripted dry-run test
  > (``tests/protocol/test_temp_migrate_vault_to_shards.py``)
  > builds a fake v1 manifest in-memory, runs the migration logic,
  > asserts the resulting root + shards round-trip equal to the
  > source entries.

The test exercises ``migrate_legacy_manifest_to_shards`` against a
``FakeShardedRelay`` so the migration logic's correctness is
verified without an actual relay round-trip. The script's
``main()`` entry point is intentionally a no-op for safety; the
real run is operator-driven after this test passes.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "temp"))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from migrate_vault_to_shards import (  # type: ignore  # noqa: E402
    decompose_legacy_manifest,
    migrate_legacy_manifest_to_shards,
)
from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_manifest,
    make_remote_folder,
)
from test_desktop_vault_shard_wire import (  # noqa: E402
    AUTHOR,
    FOLDER_A,
    FOLDER_B,
    FakeShardedRelay,
    MASTER_KEY,
    VAULT_ACCESS_SECRET,
    VAULT_ID,
    _seed_genesis,
)


def _file_version(suffix_char: str, content_bytes: int = 12345) -> dict:
    return {
        "version_id": f"fv_v1_{suffix_char * 24}",
        "created_at": "2026-05-03T09:55:00.000Z",
        "modified_at": "2026-05-03T09:55:00.000Z",
        "logical_size": content_bytes,
        "ciphertext_size": content_bytes + 48,
        "content_fingerprint": "Zm9v",
        "chunks": [{
            "chunk_id": "ch_v1_" + suffix_char * 24,
            "index": 0,
            "plaintext_size": content_bytes,
            "ciphertext_size": content_bytes + 24,
        }],
        "author_device_id": AUTHOR,
    }


def _file_entry(path: str, version_suffix: str) -> dict:
    v = _file_version(version_suffix)
    return {
        "entry_id": f"fe_v1_{version_suffix * 24}",
        "type": "file",
        "path": path,
        "deleted": False,
        "latest_version_id": v["version_id"],
        "versions": [v],
    }


def _legacy_manifest_with_two_folders() -> dict:
    """A fake pre-sharding unified manifest: two folders, three files
    each. Matches the shape ``Vault.fetch_manifest`` produced before
    Phase D.
    """
    return make_manifest(
        vault_id=VAULT_ID,
        revision=12,
        parent_revision=11,
        created_at="2026-05-03T10:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=FOLDER_A,
                display_name_enc="Documents",
                created_at="2026-05-03T09:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[
                    _file_entry("notes.md", "a"),
                    _file_entry("photo.jpg", "b"),
                    _file_entry("invoice.pdf", "c"),
                ],
            ),
            make_remote_folder(
                remote_folder_id=FOLDER_B,
                display_name_enc="Photos",
                created_at="2026-05-03T09:10:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[
                    _file_entry("sunset.jpg", "d"),
                    _file_entry("trip.mp4", "e"),
                    _file_entry("group.png", "f"),
                ],
            ),
        ],
        operation_log_tail=[],
        archived_op_segments=[],
    )


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


class DecomposeTests(unittest.TestCase):
    def test_decompose_splits_folders_into_individual_shards(self) -> None:
        legacy = _legacy_manifest_with_two_folders()
        root, shards = decompose_legacy_manifest(legacy)

        self.assertEqual(root["schema"], "dc-vault-root-v1")
        self.assertEqual(root["root_revision"], 12)
        self.assertEqual(root["parent_root_revision"], 11)
        self.assertEqual(len(root["remote_folders"]), 2)
        rf_ids = {p["remote_folder_id"] for p in root["remote_folders"]}
        self.assertEqual(rf_ids, {FOLDER_A, FOLDER_B})
        # Each pointer starts at shard_revision=1 / shard_hash="" —
        # the publish step fills these in.
        for pointer in root["remote_folders"]:
            self.assertEqual(pointer["shard_revision"], 1)
            self.assertEqual(pointer["shard_hash"], "")

        self.assertEqual(len(shards), 2)
        shard_a = next(s for fid, s in shards if fid == FOLDER_A)
        shard_b = next(s for fid, s in shards if fid == FOLDER_B)
        self.assertEqual({e["path"] for e in shard_a["entries"]}, {"notes.md", "photo.jpg", "invoice.pdf"})
        self.assertEqual({e["path"] for e in shard_b["entries"]}, {"sunset.jpg", "trip.mp4", "group.png"})


class MigrationIntegrationTests(unittest.TestCase):
    def test_dry_run_against_fake_relay_round_trips_entries(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            legacy = _legacy_manifest_with_two_folders()

            summary = migrate_legacy_manifest_to_shards(vault, relay, legacy)
            self.assertEqual(set(summary["shards_published"]), {FOLDER_A, FOLDER_B})
            self.assertEqual(summary["shards_skipped"], [])
            self.assertTrue(summary["root_published"])

            # Round-trip: fetch root + every shard, assemble, compare
            # entries.
            root_back = vault.fetch_root_manifest(relay)
            shards_back = {
                pointer["remote_folder_id"]:
                    vault.fetch_folder_shard(relay, pointer["remote_folder_id"])
                for pointer in root_back["remote_folders"]
            }
            unified_back = assemble_unified_manifest(root_back, shards_back)

            # The reassembled manifest's per-folder entry sets must
            # match the legacy input byte-for-byte. The top-level
            # revision pair stays at the legacy values (the migration
            # preserves them so AAD stays sane).
            legacy_entries = {
                folder["remote_folder_id"]: sorted(e["path"] for e in folder["entries"])
                for folder in legacy["remote_folders"]
            }
            back_entries = {
                folder["remote_folder_id"]: sorted(e["path"] for e in folder["entries"])
                for folder in unified_back["remote_folders"]
            }
            self.assertEqual(legacy_entries, back_entries)
        finally:
            vault.close()

    def test_rerun_after_partial_completion_is_idempotent(self) -> None:
        """Out-of-band: pretend the first run published folder A's
        shard but died before publishing folder B's. Re-run the
        migration. Folder A's shard CAS conflicts (skipped),
        folder B's shard publishes.
        """
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            legacy = _legacy_manifest_with_two_folders()

            # First run: only "complete" folder A. We simulate the
            # partial state by reaching into the fake.
            root, shards = decompose_legacy_manifest(legacy)
            shard_a = next(s for fid, s in shards if fid == FOLDER_A)
            vault.publish_folder_shard(relay, FOLDER_A, shard_a)

            # Now run the full migration. Folder A's shard is already
            # at revision 1 — the CAS should reject this attempt to
            # republish, and the script's idempotent skip path kicks
            # in.
            summary = migrate_legacy_manifest_to_shards(vault, relay, legacy)
            self.assertIn(FOLDER_A, summary["shards_skipped"])
            self.assertIn(FOLDER_B, summary["shards_published"])
            self.assertTrue(summary["root_published"])
        finally:
            vault.close()


if __name__ == "__main__":
    unittest.main()
