"""T5.2 — Vault browser manifest decrypt + tree-walk helpers."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault_browser_model import decrypt_manifest, get_file, list_folder  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_manifest import make_manifest, make_remote_folder  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    PHOTOS_ID,
    VAULT_ID,
)


def _manifest_vector(name: str) -> dict:
    cases = json.loads(
        Path(REPO_ROOT, "tests/protocol/vault-v1/manifest_v1.json").read_text(encoding="utf-8")
    )
    for case in cases:
        if case["name"] == name:
            return case
    raise AssertionError(f"missing vector case: {name}")


class VaultBrowserDecryptTests(unittest.TestCase):
    def test_decrypt_manifest_helper_accepts_t2_manifest_vector(self) -> None:
        case = _manifest_vector("manifest-v1-genesis-happy-path")
        vault = Vault(
            vault_id=case["inputs"]["vault_id"],
            master_key=MASTER_KEY,
            recovery_secret=None,
            vault_access_secret="unused",
            header_revision=1,
            manifest_revision=int(case["inputs"]["revision"]),
            manifest_ciphertext=b"",
            crypto=DefaultVaultCrypto,
        )
        try:
            manifest = decrypt_manifest(vault, bytes.fromhex(case["expected"]["envelope_bytes"]))
        finally:
            vault.close()

        folders, files = list_folder(manifest, "Documents")

        self.assertEqual([folder["name"] for folder in folders], [])
        self.assertEqual([file_row["name"] for file_row in files], ["report.pdf"])
        self.assertEqual(files[0]["size"], 12345)

    def test_closed_vault_cannot_decrypt_manifest(self) -> None:
        case = _manifest_vector("manifest-v1-legacy-no-remote-folders")
        vault = Vault(
            vault_id=case["inputs"]["vault_id"],
            master_key=MASTER_KEY,
            recovery_secret=None,
            vault_access_secret="unused",
            header_revision=1,
            manifest_revision=int(case["inputs"]["revision"]),
            manifest_ciphertext=b"",
            crypto=DefaultVaultCrypto,
        )
        vault.close()

        with self.assertRaisesRegex(ValueError, "vault is closed"):
            decrypt_manifest(vault, bytes.fromhex(case["expected"]["envelope_bytes"]))


class VaultBrowserTreeWalkTests(unittest.TestCase):
    def test_root_lists_remote_folders(self) -> None:
        folders, files = list_folder(_nested_manifest(), "")

        self.assertEqual([folder["name"] for folder in folders], ["Documents", "Photos"])
        self.assertEqual(files, [])

    def test_nested_paths_list_immediate_children_only(self) -> None:
        folders, files = list_folder(_nested_manifest(), "Documents")

        self.assertEqual([folder["name"] for folder in folders], ["Invoices"])
        self.assertEqual([file_row["name"] for file_row in files], ["readme.txt"])
        self.assertEqual(files[0]["path"], "Documents/readme.txt")

    def test_deleted_entries_are_excluded_by_default(self) -> None:
        folders, files = list_folder(_nested_manifest(), "Documents/Invoices/2025")

        self.assertEqual(folders, [])
        self.assertEqual(files, [])
        with self.assertRaises(KeyError):
            get_file(_nested_manifest(), "Documents/Invoices/2025/old.pdf")

    def test_get_file_returns_exact_nested_entry(self) -> None:
        entry = get_file(_nested_manifest(), "/Documents/Invoices/2026/report.pdf/")

        self.assertEqual(entry["path"], "Invoices/2026/report.pdf")
        self.assertEqual(entry["latest_version_id"], "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa")

    def test_get_file_can_include_deleted(self) -> None:
        entry = get_file(
            _nested_manifest(),
            "Documents/Invoices/2025/old.pdf",
            include_deleted=True,
        )

        self.assertTrue(entry["deleted"])

    def test_missing_display_folder_raises(self) -> None:
        with self.assertRaisesRegex(KeyError, "folder not found"):
            list_folder(_nested_manifest(), "Missing")


def _nested_manifest() -> dict:
    return make_manifest(
        vault_id=VAULT_ID,
        revision=11,
        parent_revision=10,
        created_at="2026-05-04T10:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T10:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[
                    _current_entry("readme.txt", "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb", 512),
                    _current_entry("Invoices/2026/report.pdf", "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa", 2048),
                    _deleted_entry("Invoices/2025/old.pdf"),
                ],
            ),
            make_remote_folder(
                remote_folder_id=PHOTOS_ID,
                display_name_enc="Photos",
                created_at="2026-05-04T10:01:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            ),
        ],
    )


def _current_entry(path: str, version_id: str, logical_size: int) -> dict:
    return {
        "entry_id": "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        "path": path,
        "type": "file",
        "deleted": False,
        "latest_version_id": version_id,
        "versions": [_version(version_id, logical_size)],
    }


def _deleted_entry(path: str) -> dict:
    return {
        "entry_id": "fe_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
        "path": path,
        "type": "file",
        "deleted": True,
        "latest_version_id": "fv_v1_cccccccccccccccccccccccc",
        "versions": [_version("fv_v1_cccccccccccccccccccccccc", 128)],
    }


def _version(version_id: str, logical_size: int) -> dict:
    return {
        "version_id": version_id,
        "created_at": "2026-05-04T09:59:00.000Z",
        "modified_at": "2026-05-04T09:58:00.000Z",
        "logical_size": logical_size,
        "ciphertext_size": logical_size + 24,
        "chunks": [],
        "author_device_id": AUTHOR,
    }


if __name__ == "__main__":
    unittest.main()
