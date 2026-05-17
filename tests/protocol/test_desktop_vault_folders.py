"""T4.3 — Vault add-folder manifest publish path.

Phase H step 7a: the publish path goes through ``put_root`` (sharded
root) rather than the legacy ``put_manifest``; assertions count root
revisions, not unified-manifest revisions.
"""

from __future__ import annotations

import base64
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
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.grant.store import VaultGrant  # noqa: E402
from src.vault.manifest import make_root_manifest  # noqa: E402
from src.vault.relay_errors import VaultCASConflictError  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)


class FakeRootRelay:
    """In-memory fake of the sharded root publish surface for T4.3 tests.

    Tracks the latest published root envelope + CAS revisions. A stale
    ``expected_current_root_revision`` raises ``VaultCASConflictError``
    with the freshly-published envelope embedded — same shape the real
    server returns per §A1.
    """

    def __init__(self) -> None:
        self.root_envelope: bytes = b""
        self.root_revision: int = 0
        self.root_hash: str = ""
        self.put_root_calls: list[dict] = []

    def get_root(self, vault_id: str, vault_access_secret: str) -> dict:
        return {
            "root_revision": self.root_revision,
            "parent_root_revision": max(0, self.root_revision - 1),
            "root_hash": self.root_hash,
            "root_ciphertext": self.root_envelope,
            "root_size": len(self.root_envelope),
        }

    def put_root(
        self,
        vault_id: str,
        vault_access_secret: str,
        *,
        expected_current_root_revision: int,
        new_root_revision: int,
        parent_root_revision: int,
        root_hash: str,
        root_ciphertext: bytes,
    ) -> dict:
        if int(expected_current_root_revision) != self.root_revision:
            raise VaultCASConflictError({
                "code": "vault_root_conflict",
                "message": "fake CAS conflict (root)",
                "details": {
                    "current_root_revision": self.root_revision,
                    "current_root_hash": self.root_hash,
                    "current_root_ciphertext":
                        base64.b64encode(self.root_envelope).decode("ascii"),
                    "current_root_size": len(self.root_envelope),
                },
            })
        self.put_root_calls.append({
            "vault_id": vault_id,
            "vault_access_secret": vault_access_secret,
            "expected_current_root_revision": expected_current_root_revision,
            "new_root_revision": new_root_revision,
            "parent_root_revision": parent_root_revision,
            "root_hash": root_hash,
            "root_ciphertext": root_ciphertext,
        })
        self.root_revision = int(new_root_revision)
        self.root_envelope = bytes(root_ciphertext)
        self.root_hash = root_hash
        return {"root_revision": new_root_revision, "root_hash": root_hash}


def _seed_empty_root(relay: FakeRootRelay, vault: Vault, *, created_at: str) -> None:
    """Publish a genesis root with no folder pointers so subsequent
    ``add_remote_folder`` / ``rename_remote_folder`` calls have a
    parent to chain from."""
    initial_root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=1,
        parent_root_revision=0,
        created_at=created_at,
        author_device_id=AUTHOR,
        remote_folders=[],
    )
    vault.publish_root_manifest(relay, initial_root)


class VaultFolderPublishTests(unittest.TestCase):
    def test_from_grant_copies_material_before_grant_zero(self) -> None:
        grant = VaultGrant.from_bytes(VAULT_ID, MASTER_KEY, "bearer")

        vault = Vault.from_grant(grant)
        grant.zero()

        self.assertEqual(vault.master_key, MASTER_KEY)
        self.assertEqual(vault.vault_access_secret, "bearer")

    def test_add_remote_folder_fetches_and_cas_publishes_revision(self) -> None:
        relay = FakeRootRelay()
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

        _seed_empty_root(relay, vault, created_at="2026-05-03T12:00:00.000Z")
        # Discard the seed's put_root call so the assertions below count
        # only the add-folder publish.
        relay.put_root_calls = []

        updated = vault.add_remote_folder(
            relay,
            display_name="Documents",
            ignore_patterns=[".git/", "node_modules/", "*.tmp"],
            author_device_id=AUTHOR,
            created_at="2026-05-03T13:00:00.000Z",
            remote_folder_id=DOCS_ID,
            local_index=local_index,
        )

        # ``add_remote_folder`` returns a synthesized unified manifest
        # whose ``revision`` mirrors the root chain's revision.
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["parent_revision"], 1)
        self.assertEqual(len(updated["remote_folders"]), 1)
        self.assertEqual(updated["remote_folders"][0]["display_name_enc"], "Documents")
        self.assertEqual(relay.put_root_calls[0]["expected_current_root_revision"], 1)
        self.assertEqual(relay.put_root_calls[0]["new_root_revision"], 2)
        self.assertEqual(
            relay.put_root_calls[0]["root_hash"],
            hashlib.sha256(relay.put_root_calls[0]["root_ciphertext"]).hexdigest(),
        )

        cached = local_index.list_remote_folders(VAULT_ID)
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["display_name_enc"], "Documents")

    def test_rename_remote_folder_fetches_and_cas_publishes_revision(self) -> None:
        """T4.5 — rename round-trips via fetch → mutate display_name_enc →
        publish, and the per-folder local cache reflects the new name on
        the next decrypt.
        """
        relay = FakeRootRelay()
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

        _seed_empty_root(relay, vault, created_at="2026-05-03T12:00:00.000Z")

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
        # publish's revision: genesis seed = 1, add = 2, rename = 3.
        self.assertEqual(renamed["revision"], 3)
        self.assertEqual(renamed["parent_revision"], 2)
        # Only display_name_enc changed.
        self.assertEqual(len(renamed["remote_folders"]), 1)
        folder = renamed["remote_folders"][0]
        self.assertEqual(folder["display_name_enc"], "Notes")
        self.assertEqual(folder["remote_folder_id"], DOCS_ID)
        self.assertEqual(folder["ignore_patterns"], [".git/"])

        # Last put_root call is the rename publish, with parent=2.
        last_put = relay.put_root_calls[-1]
        self.assertEqual(last_put["expected_current_root_revision"], 2)
        self.assertEqual(last_put["new_root_revision"], 3)
        self.assertEqual(
            last_put["root_hash"],
            hashlib.sha256(last_put["root_ciphertext"]).hexdigest(),
        )

        # Local index reflects the new name.
        cached = local_index.list_remote_folders(VAULT_ID)
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["display_name_enc"], "Notes")

    def test_rename_remote_folder_rejects_unknown_id(self) -> None:
        relay = FakeRootRelay()
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

        _seed_empty_root(relay, vault, created_at="2026-05-03T12:00:00.000Z")
        relay.put_root_calls = []

        with self.assertRaises(ValueError):
            vault.rename_remote_folder(
                relay,
                remote_folder_id=DOCS_ID,
                new_display_name="Notes",
                author_device_id=AUTHOR,
            )

        # Nothing was published.
        self.assertEqual(relay.put_root_calls, [])


if __name__ == "__main__":
    unittest.main()
