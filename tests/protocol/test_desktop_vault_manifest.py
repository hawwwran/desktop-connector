"""T4.1 — Vault manifest plaintext schema helpers."""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_manifest import (  # noqa: E402
    add_remote_folder,
    canonical_manifest_json,
    make_manifest,
    make_remote_folder,
    normalize_manifest_plaintext,
    remove_remote_folder,
)
from src.vault import Vault  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
PHOTOS_ID = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
MASTER_KEY = bytes.fromhex("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20")


def _manifest_vector(name: str) -> dict:
    path = Path(REPO_ROOT, "tests/protocol/vault-v1/manifest_v1.json")
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        if case["name"] == name:
            return case
    raise AssertionError(f"missing vector case: {name}")


def _manifest_vector_plaintext(name: str) -> bytes:
    return base64.b64decode(_manifest_vector(name)["inputs"]["manifest_plaintext"])


class VaultManifestSchemaTests(unittest.TestCase):
    def test_missing_remote_folders_defaults_to_empty_list(self) -> None:
        manifest = {
            "schema": "dc-vault-manifest-v1",
            "vault_id": VAULT_ID,
            "revision": 1,
            "parent_revision": 0,
            "created_at": "2026-05-03T13:00:00.000Z",
            "author_device_id": AUTHOR,
            "manifest_format_version": 1,
            "operation_log_tail": [],
            "archived_op_segments": [],
        }

        normalized = normalize_manifest_plaintext(manifest)

        self.assertEqual(normalized["remote_folders"], [])

    def test_legacy_folder_names_normalize_to_t4_fields(self) -> None:
        normalized = normalize_manifest_plaintext({
            "schema": "dc-vault-manifest-v1",
            "vault_id": VAULT_ID,
            "revision": 2,
            "parent_revision": 1,
            "created_at": "2026-05-03T13:00:00.000Z",
            "author_device_id": AUTHOR,
            "manifest_format_version": 1,
            "remote_folders": [{
                "remote_folder_id": DOCS_ID,
                "name": "Documents",
                "retention": {"keep_deleted_days": 30, "keep_versions": 10},
            }],
            "operation_log_tail": [],
            "archived_op_segments": [],
        })

        folder = normalized["remote_folders"][0]
        self.assertNotIn("name", folder)
        self.assertNotIn("retention", folder)
        self.assertEqual(folder["display_name_enc"], "Documents")
        self.assertEqual(folder["created_at"], "2026-05-03T13:00:00.000Z")
        self.assertEqual(folder["created_by_device_id"], AUTHOR)
        self.assertEqual(folder["retention_policy"], {"keep_deleted_days": 30, "keep_versions": 10})
        self.assertEqual(folder["ignore_patterns"], [])
        self.assertEqual(folder["state"], "active")

    def test_vault_decrypt_manifest_normalizes_legacy_vector_without_remote_folders(self) -> None:
        case = _manifest_vector("manifest-v1-legacy-no-remote-folders")
        vault = Vault(
            vault_id=case["inputs"]["vault_id"],
            master_key=MASTER_KEY,
            recovery_secret=None,
            vault_access_secret="unused",
            header_revision=1,
            manifest_revision=int(case["inputs"]["revision"]),
            manifest_ciphertext=bytes.fromhex(case["expected"]["envelope_bytes"]),
            crypto=DefaultVaultCrypto,
        )

        manifest = vault.decrypt_manifest()

        self.assertEqual(manifest["remote_folders"], [])

    def test_add_remote_folder_matches_t4_vector_plaintext(self) -> None:
        base = make_manifest(
            vault_id=VAULT_ID,
            revision=2,
            parent_revision=1,
            created_at="2026-05-03T13:00:00.000Z",
            author_device_id=AUTHOR,
        )
        folder = make_remote_folder(
            remote_folder_id=DOCS_ID,
            display_name_enc="Documents",
            created_at="2026-05-03T13:00:00.000Z",
            created_by_device_id=AUTHOR,
            ignore_patterns=[".git/", "node_modules/", "*.tmp"],
        )

        plaintext = canonical_manifest_json(add_remote_folder(base, folder))

        self.assertEqual(plaintext, _manifest_vector_plaintext("manifest-v1-t4-add-remote-folder"))

    def test_remove_remote_folder_matches_t4_vector_plaintext(self) -> None:
        docs = make_remote_folder(
            remote_folder_id=DOCS_ID,
            display_name_enc="Documents",
            created_at="2026-05-03T13:00:00.000Z",
            created_by_device_id=AUTHOR,
            ignore_patterns=[".git/", "node_modules/", "*.tmp"],
        )
        photos = make_remote_folder(
            remote_folder_id=PHOTOS_ID,
            display_name_enc="Photos",
            created_at="2026-05-03T13:01:00.000Z",
            created_by_device_id=AUTHOR,
            retention_policy={"keep_deleted_days": 60, "keep_versions": 20},
            ignore_patterns=["*.tmp"],
        )
        base = make_manifest(
            vault_id=VAULT_ID,
            revision=3,
            parent_revision=2,
            created_at="2026-05-03T13:05:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[docs, photos],
        )

        plaintext = canonical_manifest_json(remove_remote_folder(base, PHOTOS_ID))

        self.assertEqual(plaintext, _manifest_vector_plaintext("manifest-v1-t4-remove-remote-folder"))


if __name__ == "__main__":
    unittest.main()
