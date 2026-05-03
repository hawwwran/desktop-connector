"""T4.3 — Vault folders tab render-state helpers."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_folder_ui_state import (  # noqa: E402
    DEFAULT_FOLDER_IGNORE_PATTERNS,
    FOLDER_COLUMNS,
    default_ignore_patterns_text,
    folder_rows_from_cache,
    parse_ignore_patterns_text,
)


class VaultFolderUiStateTests(unittest.TestCase):
    def test_default_ignore_patterns_are_editable_line_text(self) -> None:
        text = default_ignore_patterns_text()

        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.splitlines(), DEFAULT_FOLDER_IGNORE_PATTERNS)

    def test_ignore_pattern_parser_strips_blanks_and_deduplicates(self) -> None:
        parsed = parse_ignore_patterns_text("\n.git/\n node_modules/ \n.git/\n*.tmp\n")

        self.assertEqual(parsed, [".git/", "node_modules/", "*.tmp"])

    def test_folder_rows_render_t4_3_columns_with_empty_usage(self) -> None:
        rows = folder_rows_from_cache([
            {
                "remote_folder_id": "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                "display_name_enc": "Documents",
                "state": "active",
            }
        ])

        self.assertEqual(FOLDER_COLUMNS, ["Name", "Binding", "Current", "Stored", "History", "Status"])
        self.assertEqual(rows, [{
            "remote_folder_id": "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
            "name": "Documents",
            "binding": "Not bound",
            "current": "0 B",
            "stored": "0 B",
            "history": "0 B",
            "status": "Active",
        }])

    def test_folder_rows_render_usage_columns(self) -> None:
        rows = folder_rows_from_cache(
            [{
                "remote_folder_id": "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                "display_name_enc": "Documents",
                "state": "active",
            }],
            usage_by_folder={
                "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa": {
                    "current_bytes": 1536,
                    "stored_bytes": 2 * 1024 * 1024,
                    "history_bytes": 0,
                }
            },
        )

        self.assertEqual(rows[0]["current"], "1 KB")
        self.assertEqual(rows[0]["stored"], "2.0 MB")
        self.assertEqual(rows[0]["history"], "0 B")


if __name__ == "__main__":
    unittest.main()
