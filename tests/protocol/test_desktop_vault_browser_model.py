"""T5.2 — Vault browser manifest decrypt + tree-walk helpers."""

from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.ui.browser_model import (  # noqa: E402
    BrowserIndex,
    _split_path,
    get_file,
    list_folder,
    list_versions,
)
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    PHOTOS_ID,
    VAULT_ID,
)


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

    def test_subfolder_with_only_tombstones_is_marked_deleted(self) -> None:
        """In show-deleted mode, a subfolder whose every descendant is
        tombstoned reports ``deleted=True`` so the browser can dim its
        row the same way it dims tombstoned files.
        """
        folders, _files = list_folder(
            _nested_manifest(), "Documents/Invoices", include_deleted=True,
        )
        by_name = {f["name"]: f for f in folders}
        self.assertEqual(set(by_name), {"2025", "2026"})
        self.assertTrue(by_name["2025"]["deleted"])
        self.assertEqual(by_name["2025"]["status"], "Deleted")
        self.assertFalse(by_name["2026"]["deleted"])
        self.assertEqual(by_name["2026"]["status"], "Folder")


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
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=11,
        parent_root_revision=10,
        created_at="2026-05-04T10:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T10:00:00.000Z",
                created_by_device_id=AUTHOR,
            ),
            make_root_folder_pointer(
                remote_folder_id=PHOTOS_ID,
                display_name_enc="Photos",
                created_at="2026-05-04T10:01:00.000Z",
                created_by_device_id=AUTHOR,
            ),
        ],
    )
    docs_shard = make_folder_shard(
        vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
        shard_revision=1, parent_shard_revision=0,
        created_at="2026-05-04T10:00:00.000Z",
        author_device_id=AUTHOR,
        entries=[
            _current_entry("readme.txt", "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb", 512),
            _current_entry("Invoices/2026/report.pdf", "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa", 2048),
            _deleted_entry("Invoices/2025/old.pdf"),
        ],
    )
    photos_shard = make_folder_shard(
        vault_id=VAULT_ID, remote_folder_id=PHOTOS_ID,
        shard_revision=1, parent_shard_revision=0,
        created_at="2026-05-04T10:01:00.000Z",
        author_device_id=AUTHOR,
        entries=[],
    )
    return assemble_unified_manifest(root, {DOCS_ID: docs_shard, PHOTOS_ID: photos_shard})


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
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=12,
        parent_root_revision=11,
        created_at="2026-05-04T10:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T10:00:00.000Z",
                created_by_device_id=AUTHOR,
            ),
        ],
    )
    shard = make_folder_shard(
        vault_id=VAULT_ID, remote_folder_id=DOCS_ID,
        shard_revision=1, parent_shard_revision=0,
        created_at="2026-05-04T10:00:00.000Z",
        author_device_id=AUTHOR,
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
    )
    return assemble_unified_manifest(root, {DOCS_ID: shard})


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


class DecryptManifestSplitTests(unittest.TestCase):
    """Review §2.M4 — ``decrypt_manifest`` is now root-only; the
    legacy ``dc-vault-v1/manifest`` HKDF fallback lives in the
    explicitly-named ``decrypt_bundle_manifest_envelope`` and is the
    only path that reaches the legacy label. Pre-fix
    ``decrypt_manifest`` silently fell back to the legacy label on
    AEAD failure, which conflated relay-fetched and bundle-decoded
    envelopes — confusing and a maintenance trap.
    """

    def test_decrypt_manifest_does_not_fall_back_to_legacy_label(self) -> None:
        import secrets
        from src.vault.crypto import (
            aead_encrypt, build_manifest_aad, build_manifest_envelope,
            derive_subkey, normalize_vault_id,
        )
        from src.vault.manifest import (
            assemble_unified_manifest as _assemble,
            canonical_manifest_json,
            make_folder_shard as _make_shard,
            make_root_folder_pointer as _make_pointer,
            make_root_manifest as _make_root,
            normalize_manifest_plaintext,
        )
        from src.vault.ui.browser_model import (
            decrypt_bundle_manifest_envelope, decrypt_manifest,
        )
        import nacl.exceptions

        master_key = bytes([0x42] * 32)

        class _FakeVault:
            def __init__(self, master_key: bytes) -> None:
                self.master_key = master_key
                self.vault_id = "ABCD2345WXYZ"

        rf_id = "rf_v1_" + "a" * 24
        root = _make_root(
            vault_id="ABCD2345WXYZ", root_revision=1, parent_root_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id="a" * 32,
            remote_folders=[
                _make_pointer(
                    remote_folder_id=rf_id,
                    display_name_enc="Documents",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id="a" * 32,
                )
            ],
        )
        shard = _make_shard(
            vault_id="ABCD2345WXYZ", remote_folder_id=rf_id,
            shard_revision=1, parent_shard_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id="a" * 32,
            entries=[],
        )
        manifest = _assemble(root, {rf_id: shard})
        # Build a legacy (dc-vault-manifest-v1) envelope under the
        # legacy HKDF label — what older export bundles carried.
        normalized = normalize_manifest_plaintext(manifest)
        plaintext = canonical_manifest_json(normalized)
        subkey = derive_subkey("dc-vault-v1/manifest", master_key)
        nonce = secrets.token_bytes(24)
        aad = build_manifest_aad(
            vault_id=str(normalized["vault_id"]),
            revision=int(normalized["revision"]),
            parent_revision=int(normalized["parent_revision"]),
            author_device_id=str(normalized["author_device_id"]),
        )
        ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
        legacy_envelope = build_manifest_envelope(
            vault_id=str(normalized["vault_id"]),
            revision=int(normalized["revision"]),
            parent_revision=int(normalized["parent_revision"]),
            author_device_id=str(normalized["author_device_id"]),
            nonce=nonce,
            aead_ciphertext_and_tag=ciphertext,
        )

        fake = _FakeVault(master_key)
        # decrypt_manifest (root-only) must REFUSE the legacy envelope
        # — the AEAD tag check fails since the root subkey + AAD
        # differ from the legacy seal.
        with self.assertRaises(nacl.exceptions.CryptoError):
            decrypt_manifest(fake, legacy_envelope)

        # decrypt_bundle_manifest_envelope is the explicitly-named
        # legacy-compatible path; it must succeed on the same bytes.
        out = decrypt_bundle_manifest_envelope(fake, legacy_envelope)
        self.assertEqual(out["vault_id"], "ABCD2345WXYZ")
        self.assertEqual(out["revision"], 1)


if __name__ == "__main__":
    unittest.main()
