"""T8.5 source pins for the Vault import wizard."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class VaultImportWizardSourceTests(unittest.TestCase):
    def test_dispatcher_registers_vault_import_route(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows.py").read_text(encoding="utf-8")
        self.assertIn("from .windows_vault_import import show_vault_import", source)
        self.assertIn('"vault-import"', source)
        self.assertIn("show_vault_import(config_dir)", source)

    def test_wizard_wires_runner_and_progress_pages(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_import.py").read_text(
            encoding="utf-8"
        )
        for text in (
            "from .vault_import_runner import open_bundle_for_preview",
            "from .vault_import_runner import run_import",
            'stack.add_named(pick_box, "pick")',
            'stack.add_named(preview_box, "preview")',
            'stack.add_named(progress_box, "progress")',
            'stack.add_named(summary_box, "summary")',
            "Bundle preview",
            "Import refused",
            "ImportMergeResolution",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)


if __name__ == "__main__":
    unittest.main()
