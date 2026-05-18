"""T4.2 — local SQLite remote-folder cache."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.state.local_index import DB_FILENAME, VaultLocalIndex  # noqa: E402
from src.vault.manifest import make_manifest, make_remote_folder  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
PHOTOS_ID = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"


def _folder(remote_folder_id: str, name: str, *, created_at: str = "2026-05-03T13:00:00.000Z") -> dict:
    return make_remote_folder(
        remote_folder_id=remote_folder_id,
        display_name_enc=name,
        created_at=created_at,
        created_by_device_id=AUTHOR,
        ignore_patterns=[".git/"] if name == "Documents" else ["*.tmp"],
    )


def _manifest(revision: int, folders: list[dict]) -> dict:
    return make_manifest(
        vault_id=VAULT_ID,
        revision=revision,
        parent_revision=revision - 1,
        created_at=f"2026-05-03T13:{revision:02d}:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=folders,
    )


class VaultRemoteFoldersCacheTests(unittest.TestCase):
    def test_refresh_replaces_previous_snapshot_without_stale_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = VaultLocalIndex(Path(tmp))

            index.refresh_remote_folders_cache(_manifest(2, [
                _folder(DOCS_ID, "Documents"),
                _folder(PHOTOS_ID, "Photos", created_at="2026-05-03T13:01:00.000Z"),
            ]))
            self.assertEqual(
                [row["remote_folder_id"] for row in index.list_remote_folders(VAULT_ID)],
                [DOCS_ID, PHOTOS_ID],
            )

            index.refresh_remote_folders_cache(_manifest(3, [
                _folder(PHOTOS_ID, "Photos", created_at="2026-05-03T13:01:00.000Z"),
            ]))

            rows = index.list_remote_folders(VAULT_ID)
            self.assertEqual([row["remote_folder_id"] for row in rows], [PHOTOS_ID])
            self.assertEqual(rows[0]["manifest_revision"], 3)
            self.assertEqual(rows[0]["display_name_enc"], "Photos")
            self.assertEqual(rows[0]["ignore_patterns"], ["*.tmp"])

    def test_manifest_without_remote_folders_clears_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = VaultLocalIndex(Path(tmp))
            index.refresh_remote_folders_cache(_manifest(2, [_folder(DOCS_ID, "Documents")]))

            index.refresh_remote_folders_cache({
                "schema": "dc-vault-manifest-v1",
                "vault_id": VAULT_ID,
                "revision": 3,
                "parent_revision": 2,
                "created_at": "2026-05-03T13:03:00.000Z",
                "author_device_id": AUTHOR,
                "manifest_format_version": 1,
                "operation_log_tail": [],
                "archived_op_segments": [],
            })

            self.assertEqual(index.list_remote_folders(VAULT_ID), [])

    def test_refresh_rolls_back_on_insert_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = VaultLocalIndex(Path(tmp))
            index.refresh_remote_folders_cache(_manifest(2, [_folder(DOCS_ID, "Documents")]))

            with self.assertRaises(Exception):
                index.refresh_remote_folders_cache(_manifest(3, [
                    _folder(PHOTOS_ID, "Photos"),
                    _folder(PHOTOS_ID, "Duplicate photos"),
                ]))

            rows = index.list_remote_folders(VAULT_ID)
            self.assertEqual([row["remote_folder_id"] for row in rows], [DOCS_ID])
            self.assertEqual(rows[0]["manifest_revision"], 2)

    def test_database_uses_wal_journal_mode(self) -> None:
        """Review §3.H8: the watcher/sync engine writes and reads the
        same SQLite file from two threads. Default rollback-journal
        mode serializes them; WAL lets them run concurrently. Without
        WAL a watcher burst could time out the 3-second stability
        gate's stat reads with stale data."""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            index = VaultLocalIndex(Path(tmp))
            # Trigger schema creation (and the connect pragmas).
            index.list_remote_folders(VAULT_ID)
            # Open an independent connection to read the PRAGMA.
            db_path = str(Path(tmp) / DB_FILENAME)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("PRAGMA journal_mode").fetchone()
                self.assertEqual(row[0].lower(), "wal")
            finally:
                conn.close()

    def test_cache_database_is_created_with_restrictive_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = VaultLocalIndex(Path(tmp))

            mode = (Path(tmp) / DB_FILENAME).stat().st_mode & 0o777

            self.assertEqual(mode, 0o600)
            self.assertEqual(index.list_remote_folders(VAULT_ID), [])


if __name__ == "__main__":
    unittest.main()
