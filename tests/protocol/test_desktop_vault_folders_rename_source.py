"""Source pin for the folder Configure dialog wiring.

History: this file used to pin the standalone "Rename folder" dialog
introduced in T4.5. **F-LT12** retired that dialog and folded it into
a single "Configure folder" surface so users can also edit ignore
patterns post-create — the rename-only path made those patterns
permanent at creation time. The pins below now track the Configure
shape; the original test names live on for git-blame continuity.

Source-pin file (one of five). See
``test_desktop_vault_browser_source`` for the policy: these greppers
catch UI-string regressions only — Configure-flow correctness is
covered by ``test_desktop_vault_folders_rename`` (no ``_source``
suffix) plus the runtime tests, not here.

F-518 (refactor): the tab calls
``runtime.update_remote_folder_settings`` and the actual
``vault.update_remote_folder_settings`` lives in
``vault_folder_runtime.VaultRuntime``. Pin both halves so a future
revert to an inline ``Vault.*`` call shape (or a different runtime
method name) fails loudly.
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
        # Post-#6 (file-size breakup): the tab is a package
        # ``desktop/src/vault_folders/`` whose modules together carry
        # the strings these pins track. Concatenate so substring
        # matches keep working regardless of which submodule a given
        # widget/dispatch lives in.
        package_dir = Path(REPO_ROOT, "desktop/src/vault_folders")
        cls.source = "\n".join(
            p.read_text() for p in sorted(package_dir.glob("*.py"))
        )
        cls.runtime_source = Path(
            REPO_ROOT, "desktop/src/vault_folder_runtime.py",
        ).read_text()

    def test_rename_button_is_live_not_disabled_with_t45_tooltip(self) -> None:
        # F-LT12 anti-regression: the standalone Rename button is gone
        # — the entry-point is the per-row overflow menu's "Configure
        # folder" item. Both the old disabled-stub tooltip and the
        # standalone live button must stay absent so a revert to the
        # rename-only path fails loudly.
        self.assertNotIn(
            "Folder rename is implemented in T4.5", self.source,
        )
        self.assertNotIn("rename_folder_btn", self.source)
        # The Configure entry-point lives in the per-row overflow menu
        # (per F-LT12 — Configure / Delete reachable without first
        # selecting the folder).
        self.assertIn('"Configure folder"', self.source)
        self.assertIn(
            "lambda r=rfid: open_configure_folder_dialog(ctx, r)", self.source,
        )

    def test_rename_dialog_opens_picker_and_name_field(self) -> None:
        # F-LT12: the dialog is invoked per-row (so the rfid is known —
        # no DropDown picker), and exposes BOTH a name entry and an
        # ignore-patterns text view, gated by a single Save button.
        for text in (
            # Signature now threads a FoldersContext; pin the prefix
            # so the multiline form ``def open_configure_folder_dialog(\n
            # ctx: FoldersContext, remote_folder_id: str,\n) -> None:`` matches.
            "def open_configure_folder_dialog(",
            "remote_folder_id: str,",
            'dialog.set_title("Configure folder")',
            "name_entry = Gtk.Entry(",
            "ignore_buffer = Gtk.TextBuffer()",
            "ignore_view = Gtk.TextView(",
            'confirm_btn = Gtk.Button(',
            'label="Save"',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_rename_dialog_publishes_via_runtime(self) -> None:
        # F-518 + F-LT12: the tab dispatches through
        # ``runtime.update_remote_folder_settings`` (which can carry
        # both a name change and ignore-pattern edits in one CAS),
        # not into ``Vault.*`` directly. The kwargs the dialog passes
        # match the runtime's signature.
        for text in (
            "runtime.update_remote_folder_settings(",
            "remote_folder_id=remote_folder_id",
            "author_device_id=author_device_id",
            "new_display_name=new_name if name_changed else None",
            "ignore_patterns=(",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        # The tab no longer reaches into raw Vault methods — the
        # runtime owns the manifest CAS.
        self.assertNotIn("vault.update_remote_folder_settings(", self.source)
        self.assertNotIn("vault.rename_remote_folder(", self.source)

    def test_runtime_publishes_via_vault_method(self) -> None:
        # F-518: the *runtime* is where the ``Vault.*`` calls land.
        # Both the legacy rename-only path AND the new
        # update_remote_folder_settings path live here so
        # programmatic callers (and the Configure UI respectively)
        # have somewhere to dispatch through. Pin the kwargs so a
        # misnamed parameter fails before the worker ever runs.
        for text in (
            "vault.update_remote_folder_settings(",
            "remote_folder_id=remote_folder_id",
            "new_display_name=new_display_name",
            "ignore_patterns=ignore_patterns",
            "author_device_id=author_device_id",
            "local_index=self._local_index",
        ):
            self.assertIn(text, self.runtime_source, msg=f"missing: {text!r}")


if __name__ == "__main__":
    unittest.main()
