"""T4.3 — Vault add-folder manifest publish path."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_grant import VaultGrant  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
    _manifest_vector,
)


class FakeManifestRelay:
    def __init__(self, envelope: bytes, *, revision: int, parent_revision: int) -> None:
        self.current_revision = revision
        self.current_parent_revision = parent_revision
        self.current_hash = hashlib.sha256(envelope).hexdigest()
        self.current_envelope = envelope
        self.put_calls: list[dict] = []

    def get_manifest(self, vault_id: str, vault_access_secret: str) -> dict:
        return {
            "revision": self.current_revision,
            "parent_revision": self.current_parent_revision,
            "manifest_hash": self.current_hash,
            "manifest_ciphertext": self.current_envelope,
            "manifest_size": len(self.current_envelope),
        }

    def put_manifest(
        self,
        vault_id: str,
        vault_access_secret: str,
        *,
        expected_current_revision: int,
        new_revision: int,
        parent_revision: int,
        manifest_hash: str,
        manifest_ciphertext: bytes,
    ) -> dict:
        self.put_calls.append({
            "vault_id": vault_id,
            "vault_access_secret": vault_access_secret,
            "expected_current_revision": expected_current_revision,
            "new_revision": new_revision,
            "parent_revision": parent_revision,
            "manifest_hash": manifest_hash,
            "manifest_ciphertext": manifest_ciphertext,
        })
        self.current_revision = new_revision
        self.current_parent_revision = parent_revision
        self.current_hash = manifest_hash
        self.current_envelope = manifest_ciphertext
        return {"revision": new_revision, "manifest_hash": manifest_hash}


class VaultFolderPublishTests(unittest.TestCase):
    def test_from_grant_copies_material_before_grant_zero(self) -> None:
        grant = VaultGrant.from_bytes(VAULT_ID, MASTER_KEY, "bearer")

        vault = Vault.from_grant(grant)
        grant.zero()

        self.assertEqual(vault.master_key, MASTER_KEY)
        self.assertEqual(vault.vault_access_secret, "bearer")

    def test_add_remote_folder_fetches_and_cas_publishes_revision(self) -> None:
        case = _manifest_vector("manifest-v1-legacy-no-remote-folders")
        relay = FakeManifestRelay(
            bytes.fromhex(case["expected"]["envelope_bytes"]),
            revision=int(case["inputs"]["revision"]),
            parent_revision=int(case["inputs"]["parent_revision"]),
        )
        vault = Vault(
            vault_id=VAULT_ID,
            master_key=MASTER_KEY,
            recovery_secret=None,
            vault_access_secret="bearer",
            header_revision=0,
            manifest_revision=0,
            manifest_ciphertext=b"",
            crypto=DefaultVaultCrypto,
        )
        tmpdir = tempfile.mkdtemp(prefix="vault_folder_publish_test_")
        local_index = VaultLocalIndex(Path(tmpdir))

        updated = vault.add_remote_folder(
            relay,
            display_name="Documents",
            ignore_patterns=[".git/", "node_modules/", "*.tmp"],
            author_device_id=AUTHOR,
            created_at="2026-05-03T13:00:00.000Z",
            remote_folder_id=DOCS_ID,
            local_index=local_index,
        )

        self.assertEqual(updated["revision"], 7)
        self.assertEqual(updated["parent_revision"], 6)
        self.assertEqual(len(updated["remote_folders"]), 1)
        self.assertEqual(updated["remote_folders"][0]["display_name_enc"], "Documents")
        self.assertEqual(relay.put_calls[0]["expected_current_revision"], 6)
        self.assertEqual(relay.put_calls[0]["new_revision"], 7)
        self.assertEqual(
            relay.put_calls[0]["manifest_hash"],
            hashlib.sha256(relay.put_calls[0]["manifest_ciphertext"]).hexdigest(),
        )

        decrypted = vault.decrypt_manifest(local_index=local_index)
        self.assertEqual(decrypted["revision"], 7)
        self.assertEqual(decrypted["remote_folders"][0]["remote_folder_id"], DOCS_ID)
        cached = local_index.list_remote_folders(VAULT_ID)
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["display_name_enc"], "Documents")

    def test_rename_remote_folder_fetches_and_cas_publishes_revision(self) -> None:
        """T4.5 — rename round-trips via fetch → mutate display_name_enc →
        publish, and the per-folder local cache reflects the new name on
        the next decrypt.
        """
        case = _manifest_vector("manifest-v1-legacy-no-remote-folders")
        relay = FakeManifestRelay(
            bytes.fromhex(case["expected"]["envelope_bytes"]),
            revision=int(case["inputs"]["revision"]),
            parent_revision=int(case["inputs"]["parent_revision"]),
        )
        vault = Vault(
            vault_id=VAULT_ID,
            master_key=MASTER_KEY,
            recovery_secret=None,
            vault_access_secret="bearer",
            header_revision=0,
            manifest_revision=0,
            manifest_ciphertext=b"",
            crypto=DefaultVaultCrypto,
        )
        tmpdir = tempfile.mkdtemp(prefix="vault_folder_rename_test_")
        local_index = VaultLocalIndex(Path(tmpdir))

        # Seed: add a folder so there's something to rename.
        vault.add_remote_folder(
            relay,
            display_name="Documents",
            ignore_patterns=[".git/"],
            author_device_id=AUTHOR,
            created_at="2026-05-03T13:00:00.000Z",
            remote_folder_id=DOCS_ID,
            local_index=local_index,
        )

        renamed = vault.rename_remote_folder(
            relay,
            remote_folder_id=DOCS_ID,
            new_display_name="Notes",
            author_device_id=AUTHOR,
            created_at="2026-05-03T13:10:00.000Z",
            local_index=local_index,
        )

        # Revision advances by 1 (CAS), parent_revision matches the seed
        # publish's revision.
        self.assertEqual(renamed["revision"], 8)
        self.assertEqual(renamed["parent_revision"], 7)
        # Only display_name_enc changed.
        self.assertEqual(len(renamed["remote_folders"]), 1)
        folder = renamed["remote_folders"][0]
        self.assertEqual(folder["display_name_enc"], "Notes")
        self.assertEqual(folder["remote_folder_id"], DOCS_ID)
        self.assertEqual(folder["ignore_patterns"], [".git/"])

        # Last put_call is the rename publish, with parent_revision=7.
        last_put = relay.put_calls[-1]
        self.assertEqual(last_put["expected_current_revision"], 7)
        self.assertEqual(last_put["new_revision"], 8)
        self.assertEqual(
            last_put["manifest_hash"],
            hashlib.sha256(last_put["manifest_ciphertext"]).hexdigest(),
        )

        # Local index reflects the new name.
        cached = local_index.list_remote_folders(VAULT_ID)
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["display_name_enc"], "Notes")

    def test_rename_remote_folder_rejects_unknown_id(self) -> None:
        case = _manifest_vector("manifest-v1-legacy-no-remote-folders")
        relay = FakeManifestRelay(
            bytes.fromhex(case["expected"]["envelope_bytes"]),
            revision=int(case["inputs"]["revision"]),
            parent_revision=int(case["inputs"]["parent_revision"]),
        )
        vault = Vault(
            vault_id=VAULT_ID,
            master_key=MASTER_KEY,
            recovery_secret=None,
            vault_access_secret="bearer",
            header_revision=0,
            manifest_revision=0,
            manifest_ciphertext=b"",
            crypto=DefaultVaultCrypto,
        )

        with self.assertRaises(ValueError):
            vault.rename_remote_folder(
                relay,
                remote_folder_id=DOCS_ID,
                new_display_name="Notes",
                author_device_id=AUTHOR,
            )

        # Nothing was published.
        self.assertEqual(relay.put_calls, [])


if __name__ == "__main__":
    unittest.main()
