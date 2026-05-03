"""Source pin for the T4.5 folder-rename dialog wiring."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class VaultFoldersRenameSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(
            REPO_ROOT, "desktop/src/vault_folders_tab.py",
        ).read_text()

    def test_rename_button_is_live_not_disabled_with_t45_tooltip(self) -> None:
        # Once T4.5 ships, the button must be a real action — not a
        # disabled stub with the "implemented in T4.5" tooltip.
        self.assertNotIn(
            "Folder rename is implemented in T4.5", self.source,
        )
        # Sensitivity tracks vault presence (mirrors the Add button gate).
        self.assertIn(
            "rename_folder_btn.set_sensitive(bool(vault_id))", self.source,
        )

    def test_rename_dialog_opens_picker_and_name_field(self) -> None:
        for text in (
            "def open_rename_folder_dialog(_btn) -> None:",
            'title="Rename folder"',
            "Gtk.DropDown.new_from_strings(",
            'name_entry = Gtk.Entry(',
            'confirm_btn = Gtk.Button(label="Save"',
            'rename_folder_btn.connect("clicked", open_rename_folder_dialog)',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_rename_dialog_publishes_via_vault_method(self) -> None:
        # The dialog must call the CAS-publish helper (not edit the
        # local index directly) so a rename is a real manifest revision.
        for text in (
            "vault.rename_remote_folder(",
            "remote_folder_id=rfid",
            "new_display_name=new_name",
            "author_device_id=author_device_id",
            "local_index=local_index",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")


if __name__ == "__main__":
    unittest.main()
