"""Source pin for the T4.5 folder-rename dialog wiring.

Source-pin file (one of five). See
``test_desktop_vault_browser_source`` for the policy: these greppers
catch UI-string regressions only — rename-flow correctness is
covered by ``test_desktop_vault_folders_rename`` (no ``_source``
suffix), not here.

F-518 (refactor): the tab calls ``runtime.rename_remote_folder`` and
the actual ``vault.rename_remote_folder`` lives in
``vault_folder_runtime.VaultRuntime``. Pin both halves so a future
revert to the inline call shape (or to a different runtime method
name) fails loudly.
"""

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
        cls.runtime_source = Path(
            REPO_ROOT, "desktop/src/vault_folder_runtime.py",
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

    def test_rename_dialog_publishes_via_runtime(self) -> None:
        # F-518: tab dispatches through the runtime, not into ``Vault.*``
        # directly. The kwargs the dialog passes match the runtime's
        # ``rename_remote_folder`` signature.
        for text in (
            "runtime.rename_remote_folder(",
            "remote_folder_id=rfid",
            "new_display_name=new_name",
            "author_device_id=author_device_id",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        # The tab no longer reaches into the raw Vault method or the
        # local_index parameter — the runtime owns both.
        self.assertNotIn("vault.rename_remote_folder(", self.source)

    def test_runtime_publishes_via_vault_method(self) -> None:
        # F-518: the *runtime* is where ``vault.rename_remote_folder``
        # lands now. Pin the call shape there so a misnamed kwarg
        # would fail before the rename worker ever runs.
        for text in (
            "vault.rename_remote_folder(",
            "remote_folder_id=remote_folder_id",
            "new_display_name=new_display_name",
            "author_device_id=author_device_id",
            "local_index=self._local_index",
        ):
            self.assertIn(text, self.runtime_source, msg=f"missing: {text!r}")


if __name__ == "__main__":
    unittest.main()
