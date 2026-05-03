"""T4.4 — Vault per-folder usage calculation."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_manifest import make_manifest, make_remote_folder  # noqa: E402
from src.vault_usage import calculate_vault_usage  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    PHOTOS_ID,
    VAULT_ID,
)


SHARED_CHUNK = "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
DOC_HISTORY_CHUNK = "ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
PHOTO_HISTORY_CHUNK = "ch_v1_cccccccccccccccccccccccc"


class VaultUsageTests(unittest.TestCase):
    def test_shared_current_chunk_counts_in_each_folder_but_once_globally(self) -> None:
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=9,
            parent_revision=8,
            created_at="2026-05-03T15:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-03T15:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[_current_entry("docs/report.pdf", 1000, SHARED_CHUNK, 700)],
                ),
                make_remote_folder(
                    remote_folder_id=PHOTOS_ID,
                    display_name_enc="Photos",
                    created_at="2026-05-03T15:01:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[_current_entry("photos/report-copy.pdf", 1000, SHARED_CHUNK, 700)],
                ),
            ],
        )

        usage = calculate_vault_usage(manifest)

        self.assertEqual(usage.by_folder[DOCS_ID]["current_bytes"], 1000)
        self.assertEqual(usage.by_folder[PHOTOS_ID]["current_bytes"], 1000)
        self.assertEqual(usage.by_folder[DOCS_ID]["stored_bytes"], 700)
        self.assertEqual(usage.by_folder[PHOTOS_ID]["stored_bytes"], 700)
        self.assertEqual(usage.whole_vault_stored_bytes, 700)

    def test_history_counts_non_current_and_deleted_chunks(self) -> None:
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=10,
            parent_revision=9,
            created_at="2026-05-03T15:05:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-03T15:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[
                        _entry_with_history(
                            "docs/report.pdf",
                            current_logical=4096,
                            current_chunk=SHARED_CHUNK,
                            current_ciphertext=4120,
                            history_chunk=DOC_HISTORY_CHUNK,
                            history_ciphertext=2048,
                        ),
                        _deleted_entry(
                            "docs/old.txt",
                            chunk_id=PHOTO_HISTORY_CHUNK,
                            ciphertext_size=512,
                        ),
                    ],
                )
            ],
        )

        usage = calculate_vault_usage(manifest)

        self.assertEqual(usage.by_folder[DOCS_ID], {
            "current_bytes": 4096,
            "stored_bytes": 4120,
            "history_bytes": 2560,
        })
        self.assertEqual(usage.whole_vault_stored_bytes, 6680)


def _current_entry(path: str, logical_size: int, chunk_id: str, ciphertext_size: int) -> dict:
    return {
        "entry_id": "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        "path": path,
        "type": "file",
        "deleted": False,
        "latest_version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        "versions": [_version("fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa", logical_size, chunk_id, ciphertext_size)],
    }


def _entry_with_history(
    path: str,
    *,
    current_logical: int,
    current_chunk: str,
    current_ciphertext: int,
    history_chunk: str,
    history_ciphertext: int,
) -> dict:
    return {
        "entry_id": "fe_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
        "path": path,
        "type": "file",
        "deleted": False,
        "latest_version_id": "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
        "versions": [
            _version("fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa", 2048, history_chunk, history_ciphertext),
            _version("fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb", current_logical, current_chunk, current_ciphertext),
        ],
    }


def _deleted_entry(path: str, *, chunk_id: str, ciphertext_size: int) -> dict:
    return {
        "entry_id": "fe_v1_cccccccccccccccccccccccc",
        "path": path,
        "type": "file",
        "deleted": True,
        "latest_version_id": "fv_v1_cccccccccccccccccccccccc",
        "versions": [_version("fv_v1_cccccccccccccccccccccccc", 512, chunk_id, ciphertext_size)],
    }


def _version(version_id: str, logical_size: int, chunk_id: str, ciphertext_size: int) -> dict:
    return {
        "version_id": version_id,
        "logical_size": logical_size,
        "ciphertext_size": ciphertext_size,
        "chunks": [{
            "chunk_id": chunk_id,
            "index": 0,
            "plaintext_size": logical_size,
            "ciphertext_size": ciphertext_size,
        }],
    }


if __name__ == "__main__":
    unittest.main()
