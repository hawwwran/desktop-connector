"""T5.2 — Vault browser manifest decrypt + tree-walk helpers."""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.ui.browser_model import (  # noqa: E402
    BrowserIndex,
    _split_path,
    decrypt_manifest,
    get_file,
    list_folder,
    list_versions,
)
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import make_manifest, make_remote_folder  # noqa: E402

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


class SplitPathSafetyTests(unittest.TestCase):
    """F-519 — ``..`` components are dropped + a skip_unsafe warning fires."""

    def test_dotdot_components_are_dropped(self) -> None:
        # Mixed normal segments with a `..` component — `..` is silently
        # filtered just like `.` is, so the browser never resolves
        # something that "feels above" the current view.
        self.assertEqual(_split_path("Documents/../etc/passwd"),
                         ["Documents", "etc", "passwd"])

    def test_dotdot_emits_skip_unsafe_warning(self) -> None:
        with self.assertLogs("src.vault.ui.browser_model", level="WARNING") as cm:
            _split_path("Documents/../etc/passwd")
        joined = "\n".join(cm.output)
        self.assertIn("vault.browser.skip_unsafe", joined)
        self.assertIn("path=", joined)

    def test_clean_path_does_not_emit_warning(self) -> None:
        logger = logging.getLogger("src.vault.ui.browser_model")
        # `assertLogs` requires at least one record — verify the clean
        # path doesn't emit one by capturing through a manual handler.
        captured: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self_inner, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Cap(level=logging.WARNING)
        logger.addHandler(handler)
        try:
            _split_path("Documents/Invoices/2026/report.pdf")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(captured, [])

    def test_get_file_with_traversal_in_lookup_input_falls_through(self) -> None:
        # A user-supplied path with `..` collapses to the folder/file
        # name at hand; the lookup either matches a real entry or
        # raises KeyError. It must NOT silently match a parent.
        manifest = _nested_manifest()
        # `Documents/Invoices/2026/../2025/old.pdf` collapses to
        # `Documents/Invoices/2026/2025/old.pdf` — which doesn't exist
        # in the manifest, so KeyError is the correct outcome.
        with self.assertRaises(KeyError):
            get_file(manifest,
                     "Documents/Invoices/2026/../2025/old.pdf",
                     include_deleted=True)


class VaultBrowserListVersionsTests(unittest.TestCase):
    def test_three_versions_render_three_rows_newest_first(self) -> None:
        rows = list_versions(_versioned_manifest(), "Documents/report.pdf")

        self.assertEqual([row["version_id"] for row in rows], [
            "fv_v1_cccccccccccccccccccccccc",
            "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
            "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        ])
        self.assertEqual([row["is_current"] for row in rows], [True, False, False])
        self.assertEqual([row["size"] for row in rows], [3 * 1024, 2 * 1024, 1024])
        self.assertEqual(rows[1]["author_device_id"], AUTHOR)

    def test_deleted_file_versions_are_hidden_by_default(self) -> None:
        with self.assertRaises(KeyError):
            list_versions(_versioned_manifest(), "Documents/Invoices/2025/old.pdf")

    def test_deleted_file_versions_visible_when_requested(self) -> None:
        rows = list_versions(
            _versioned_manifest(),
            "Documents/Invoices/2025/old.pdf",
            include_deleted=True,
        )
        self.assertTrue(rows)
        self.assertTrue(rows[0]["is_deleted"])


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


def _versioned_manifest() -> dict:
    versions = [
        {
            "version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
            "created_at": "2026-04-01T10:00:00.000Z",
            "modified_at": "2026-04-01T09:58:00.000Z",
            "logical_size": 1024,
            "ciphertext_size": 1048,
            "chunks": [],
            "author_device_id": AUTHOR,
        },
        {
            "version_id": "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
            "created_at": "2026-04-15T11:00:00.000Z",
            "modified_at": "2026-04-15T10:58:00.000Z",
            "logical_size": 2 * 1024,
            "ciphertext_size": 2 * 1024 + 24,
            "chunks": [],
            "author_device_id": AUTHOR,
        },
        {
            "version_id": "fv_v1_cccccccccccccccccccccccc",
            "created_at": "2026-05-01T12:00:00.000Z",
            "modified_at": "2026-05-01T11:58:00.000Z",
            "logical_size": 3 * 1024,
            "ciphertext_size": 3 * 1024 + 24,
            "chunks": [],
            "author_device_id": AUTHOR,
        },
    ]
    return make_manifest(
        vault_id=VAULT_ID,
        revision=12,
        parent_revision=11,
        created_at="2026-05-04T10:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T10:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[
                    {
                        "entry_id": "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                        "path": "report.pdf",
                        "type": "file",
                        "deleted": False,
                        "latest_version_id": "fv_v1_cccccccccccccccccccccccc",
                        "versions": versions,
                    },
                    {
                        "entry_id": "fe_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
                        "path": "Invoices/2025/old.pdf",
                        "type": "file",
                        "deleted": True,
                        "latest_version_id": "fv_v1_dddddddddddddddddddddddd",
                        "versions": [{
                            "version_id": "fv_v1_dddddddddddddddddddddddd",
                            "created_at": "2025-12-01T08:00:00.000Z",
                            "modified_at": "2025-12-01T07:58:00.000Z",
                            "logical_size": 64,
                            "ciphertext_size": 88,
                            "chunks": [],
                            "author_device_id": AUTHOR,
                        }],
                    },
                ],
            ),
        ],
    )


class BrowserIndexTests(unittest.TestCase):
    """F-520 — indexed manifest view matches the bare helpers' output."""

    def test_list_folder_matches_bare_helper(self) -> None:
        manifest = _nested_manifest()
        index = BrowserIndex(manifest)
        for path in [
            "",
            "Documents",
            "Documents/Invoices",
            "Documents/Invoices/2026",
            "Photos",
        ]:
            with self.subTest(path=path):
                expected = list_folder(manifest, path)
                actual = index.list_folder(path)
                # Compare folders + files separately; rows may carry
                # the same content but iteration order is sorted by
                # name (case-folded) on both paths.
                self.assertEqual(
                    [(f["name"], f["path"]) for f in expected[0]],
                    [(f["name"], f["path"]) for f in actual[0]],
                )
                self.assertEqual(
                    [(f["name"], f["path"]) for f in expected[1]],
                    [(f["name"], f["path"]) for f in actual[1]],
                )

    def test_index_caches_per_folder(self) -> None:
        manifest = _nested_manifest()
        index = BrowserIndex(manifest)
        # Spy on _index_for_folder by counting builds via cache key.
        index.list_folder("Documents")
        # A second call into the same folder must hit the cache.
        before_keys = set(index._folder_cache.keys())
        index.list_folder("Documents/Invoices")
        index.list_folder("Documents/Invoices/2026")
        after_keys = set(index._folder_cache.keys())
        # All three calls landed in the same Documents-folder bucket.
        new_keys = after_keys - before_keys
        self.assertEqual(new_keys, set())

    def test_index_separates_include_deleted_buckets(self) -> None:
        manifest = _nested_manifest()
        index = BrowserIndex(manifest)
        # Same folder, different include_deleted → different cache entries.
        _, files_default = index.list_folder("Documents/Invoices/2025")
        _, files_with_deleted = index.list_folder(
            "Documents/Invoices/2025", include_deleted=True,
        )
        self.assertEqual(files_default, [])
        self.assertEqual([f["name"] for f in files_with_deleted], ["old.pdf"])
        # Two cache entries on the same folder id (one per
        # include_deleted flag).
        keys = list(index._folder_cache.keys())
        flags = sorted(set(k[1] for k in keys))
        self.assertEqual(flags, [False, True])

    def test_revision_matches_manifest(self) -> None:
        manifest = _nested_manifest()
        self.assertEqual(BrowserIndex(manifest).revision, 11)

    def test_get_file_via_index_matches_bare_helper(self) -> None:
        manifest = _nested_manifest()
        index = BrowserIndex(manifest)
        bare = get_file(manifest, "Documents/Invoices/2026/report.pdf")
        via_index = index.get_file("Documents/Invoices/2026/report.pdf")
        self.assertEqual(bare, via_index)


if __name__ == "__main__":
    unittest.main()
