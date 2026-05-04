"""T14.1 + T14.2 — Clear-folder + Clear-vault danger flows."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_clear import (  # noqa: E402
    build_clear_folder_manifest,
    build_clear_vault_manifest,
    confirm_folder_clear_text_matches,
    confirm_vault_clear_text_matches,
)
from src.vault_manifest import (  # noqa: E402
    add_or_append_file_version, make_manifest, make_remote_folder,
)


VAULT_ID = "ABCD2345WXYZ"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
PHOTOS_ID = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"


def _seed_manifest_with_files() -> dict:
    manifest = make_manifest(
        vault_id=VAULT_ID, revision=5, parent_revision=4,
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
            make_remote_folder(
                remote_folder_id=PHOTOS_ID,
                display_name_enc="Photos",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            ),
        ],
    )
    # Seed a few file entries in each folder.
    for path in ("notes.txt", "ledger.txt", "draft.md"):
        manifest = _add_file(manifest, DOCS_ID, path)
    for path in ("summer.jpg", "winter.png"):
        manifest = _add_file(manifest, PHOTOS_ID, path)
    return manifest


def _add_file(manifest: dict, folder_id: str, path: str) -> dict:
    # Build a deterministic but valid fv_v1_<24base32> id.
    import re
    base = re.sub(r"[^a-z2-7]", "a", path.lower())[:24].ljust(24, "a")
    return add_or_append_file_version(
        manifest,
        remote_folder_id=folder_id,
        path=path,
        version={
            "version_id": "fv_v1_" + base,
            "chunks": [],
            "logical_size": 10,
            "content_fingerprint": "fp_" + path,
            "created_at": "2026-05-04T12:00:00.000Z",
            "created_by_device_id": AUTHOR,
        },
    )


class ConfirmTextTests(unittest.TestCase):
    def test_folder_name_match_is_exact_case(self) -> None:
        self.assertTrue(confirm_folder_clear_text_matches("Documents", "Documents"))
        self.assertFalse(confirm_folder_clear_text_matches("documents", "Documents"))
        self.assertTrue(confirm_folder_clear_text_matches("  Documents  ", "Documents"))

    def test_vault_id_match_is_case_insensitive(self) -> None:
        self.assertTrue(confirm_vault_clear_text_matches("abcd-2345-wxyz", "ABCD-2345-WXYZ"))
        self.assertTrue(confirm_vault_clear_text_matches(" ABCD-2345-WXYZ ", "ABCD-2345-WXYZ"))
        self.assertFalse(confirm_vault_clear_text_matches("WXYZ-2345-ABCD", "ABCD-2345-WXYZ"))

    def test_non_string_inputs_return_false(self) -> None:
        self.assertFalse(confirm_folder_clear_text_matches(None, "x"))  # type: ignore[arg-type]
        self.assertFalse(confirm_folder_clear_text_matches("x", None))  # type: ignore[arg-type]
        self.assertFalse(confirm_vault_clear_text_matches("x", None))  # type: ignore[arg-type]


class ClearFolderTests(unittest.TestCase):
    def test_tombstones_every_live_entry_in_folder(self) -> None:
        manifest = _seed_manifest_with_files()
        cleared = build_clear_folder_manifest(
            manifest,
            remote_folder_id=DOCS_ID,
            author_device_id=AUTHOR,
            deleted_at="2026-05-05T00:00:00.000Z",
        )

        self.assertEqual(cleared["revision"], 6)
        self.assertEqual(cleared["parent_revision"], 5)
        # Every Documents entry is now tombstoned.
        docs = next(
            f for f in cleared["remote_folders"] if f["remote_folder_id"] == DOCS_ID
        )
        for entry in docs["entries"]:
            self.assertTrue(entry["deleted"])
            self.assertEqual(entry["deleted_at"], "2026-05-05T00:00:00.000Z")
            self.assertEqual(entry["deleted_by_device_id"], AUTHOR)

        # Photos is untouched.
        photos = next(
            f for f in cleared["remote_folders"] if f["remote_folder_id"] == PHOTOS_ID
        )
        self.assertFalse(any(e.get("deleted") for e in photos["entries"]))

    def test_already_deleted_entries_are_not_double_counted(self) -> None:
        manifest = _seed_manifest_with_files()
        # Pre-tombstone one of the files.
        from src.vault_manifest import tombstone_file_entry
        manifest = tombstone_file_entry(
            manifest,
            remote_folder_id=DOCS_ID, path="ledger.txt",
            deleted_at="2026-05-04T20:00:00.000Z",
            author_device_id=AUTHOR,
        )
        cleared = build_clear_folder_manifest(
            manifest,
            remote_folder_id=DOCS_ID,
            author_device_id=AUTHOR,
            deleted_at="2026-05-05T00:00:00.000Z",
        )
        docs = next(
            f for f in cleared["remote_folders"] if f["remote_folder_id"] == DOCS_ID
        )
        # ledger.txt keeps its earlier deleted_at (we only re-tombstone
        # live entries; the bulk pass doesn't touch already-deleted rows).
        ledger = next(e for e in docs["entries"] if e["path"] == "ledger.txt")
        self.assertEqual(ledger["deleted_at"], "2026-05-04T20:00:00.000Z")

    def test_unknown_folder_id_raises(self) -> None:
        manifest = _seed_manifest_with_files()
        with self.assertRaises(KeyError):
            build_clear_folder_manifest(
                manifest,
                remote_folder_id="rf_v1_doesnotexist",
                author_device_id=AUTHOR,
                deleted_at="2026-05-05T00:00:00.000Z",
            )

    def test_empty_folder_still_bumps_revision(self) -> None:
        manifest = make_manifest(
            vault_id=VAULT_ID, revision=2, parent_revision=1,
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
        cleared = build_clear_folder_manifest(
            manifest,
            remote_folder_id=DOCS_ID,
            author_device_id=AUTHOR,
            deleted_at="2026-05-05T00:00:00.000Z",
        )
        self.assertEqual(cleared["revision"], 3)
        self.assertEqual(cleared["parent_revision"], 2)
        # Empty entries list survived.
        docs = next(
            f for f in cleared["remote_folders"] if f["remote_folder_id"] == DOCS_ID
        )
        self.assertEqual(docs["entries"], [])


class ClearVaultTests(unittest.TestCase):
    def test_tombstones_every_entry_across_every_folder(self) -> None:
        manifest = _seed_manifest_with_files()
        cleared = build_clear_vault_manifest(
            manifest,
            author_device_id=AUTHOR,
            deleted_at="2026-05-05T00:00:00.000Z",
        )

        self.assertEqual(cleared["revision"], 6)
        for folder in cleared["remote_folders"]:
            for entry in folder["entries"]:
                self.assertTrue(entry["deleted"], f"{folder['remote_folder_id']}/{entry['path']}")

    def test_audit_fields_stamped_on_envelope(self) -> None:
        manifest = _seed_manifest_with_files()
        cleared = build_clear_vault_manifest(
            manifest,
            author_device_id=AUTHOR,
            deleted_at="2026-05-05T00:00:00.000Z",
        )
        self.assertEqual(cleared["author_device_id"], AUTHOR)
        self.assertEqual(cleared["created_at"], "2026-05-05T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
