"""T14.1 + T14.2 — Clear-folder + Clear-vault danger flows.

The bulk-tombstone mechanics are exercised by
``test_desktop_vault_delete.py`` (the orchestrator calls
``delete_folder_contents`` under the hood); this file pins the
confirm-text guards that live in front of those operations.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.ops.clear import (  # noqa: E402
    confirm_folder_clear_text_matches,
    confirm_vault_clear_text_matches,
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


if __name__ == "__main__":
    unittest.main()
