"""T4.4 — Vault per-folder usage calculation."""

from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)
from src.vault.state.usage import calculate_vault_usage  # noqa: E402

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
        manifest = _build_unified(
            revision=9, parent_revision=8,
            created_at="2026-05-03T15:00:00.000Z",
            folders=[
                (DOCS_ID, "Documents", "2026-05-03T15:00:00.000Z",
                 [_current_entry("docs/report.pdf", 1000, SHARED_CHUNK, 700)]),
                (PHOTOS_ID, "Photos", "2026-05-03T15:01:00.000Z",
                 [_current_entry("photos/report-copy.pdf", 1000, SHARED_CHUNK, 700)]),
            ],
        )

        usage = calculate_vault_usage(manifest)

        self.assertEqual(usage.by_folder[DOCS_ID]["current_bytes"], 1000)
        self.assertEqual(usage.by_folder[PHOTOS_ID]["current_bytes"], 1000)
        self.assertEqual(usage.by_folder[DOCS_ID]["stored_bytes"], 700)
        self.assertEqual(usage.by_folder[PHOTOS_ID]["stored_bytes"], 700)
        self.assertEqual(usage.whole_vault_stored_bytes, 700)

    def test_history_counts_non_current_and_deleted_chunks(self) -> None:
        manifest = _build_unified(
            revision=10, parent_revision=9,
            created_at="2026-05-03T15:05:00.000Z",
            folders=[
                (DOCS_ID, "Documents", "2026-05-03T15:00:00.000Z", [
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
                ]),
            ],
        )

        usage = calculate_vault_usage(manifest)

        self.assertEqual(usage.by_folder[DOCS_ID], {
            "current_bytes": 4096,
            "stored_bytes": 4120,
            "history_bytes": 2560,
        })
        self.assertEqual(usage.whole_vault_stored_bytes, 6680)


class MalformedChunkWarningTests(unittest.TestCase):
    """F-515 — surface a warning when a chunk's ciphertext_size is unusable."""

    def _manifest_with_missing_size(self) -> dict:
        return _build_unified(
            revision=2, parent_revision=1,
            created_at="2026-05-06T10:00:00.000Z",
            folders=[
                (DOCS_ID, "Documents", "2026-05-06T10:00:00.000Z", [{
                    "entry_id": "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                    "path": "broken.bin",
                    "type": "file",
                    "deleted": False,
                    "latest_version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                    "versions": [{
                        "version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                        "logical_size": 4096,
                        "ciphertext_size": 4120,
                        # No ciphertext_size on the chunk dict.
                        "chunks": [{
                            "chunk_id": SHARED_CHUNK,
                            "index": 0,
                            "plaintext_size": 4096,
                        }],
                    }],
                }]),
            ],
        )

    def test_missing_chunk_ciphertext_size_emits_warning(self) -> None:
        with self.assertLogs("src.vault.state.usage", level="WARNING") as cm:
            calculate_vault_usage(self._manifest_with_missing_size())
        joined = "\n".join(cm.output)
        self.assertIn("vault.usage.malformed_chunk_size_skipped", joined)
        # The chunk_id should appear truncated to 12 chars to keep the
        # log compact (matches the rest of the diagnostics catalog).
        self.assertIn(SHARED_CHUNK[:12], joined)

    def test_well_formed_manifest_does_not_warn(self) -> None:
        manifest = _build_unified(
            revision=1, parent_revision=0,
            created_at="2026-05-06T10:00:00.000Z",
            folders=[
                (DOCS_ID, "Documents", "2026-05-06T10:00:00.000Z",
                 [_current_entry("clean.bin", 1024, SHARED_CHUNK, 1048)]),
            ],
        )
        captured: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self_inner, record: logging.LogRecord) -> None:
                captured.append(record)

        logger = logging.getLogger("src.vault.state.usage")
        handler = _Cap(level=logging.WARNING)
        logger.addHandler(handler)
        try:
            calculate_vault_usage(manifest)
        finally:
            logger.removeHandler(handler)
        self.assertEqual(captured, [])


def _build_unified(
    *,
    revision: int,
    parent_revision: int,
    created_at: str,
    folders: list[tuple[str, str, str, list[dict]]],
) -> dict:
    """Build a sharded root + per-folder shards, return the unified-shape dict."""
    pointers = [
        make_root_folder_pointer(
            remote_folder_id=fid,
            display_name_enc=name,
            created_at=f_at,
            created_by_device_id=AUTHOR,
        )
        for (fid, name, f_at, _) in folders
    ]
    shards_by_id = {
        fid: make_folder_shard(
            vault_id=VAULT_ID,
            remote_folder_id=fid,
            shard_revision=1,
            parent_shard_revision=0,
            created_at=f_at,
            author_device_id=AUTHOR,
            entries=entries,
        )
        for (fid, _, f_at, entries) in folders
    }
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=revision,
        parent_root_revision=parent_revision,
        created_at=created_at,
        author_device_id=AUTHOR,
        remote_folders=pointers,
    )
    return assemble_unified_manifest(root, shards_by_id)


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
