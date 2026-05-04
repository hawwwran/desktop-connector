"""T5.1 source pins for the Vault browser GTK entry point."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class VaultBrowserGtkSourceTests(unittest.TestCase):
    def test_window_dispatcher_registers_vault_browser(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows.py").read_text(encoding="utf-8")

        self.assertIn("from .windows_vault_browser import show_vault_browser", source)
        self.assertIn('"vault-browser"', source)
        self.assertIn("show_vault_browser(config_dir)", source)

    def test_tray_open_vault_launches_browser_not_settings(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/tray.py").read_text(encoding="utf-8")

        self.assertIn('"Open Vault…",\n                self._spawn_vault_browser', source)
        self.assertIn('self._open_gtk4_window("vault-browser")', source)
        self.assertIn('self._open_gtk4_window("vault-main")', source)

    def test_browser_window_has_t5_surface_labels(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "Back",
            "Forward",
            "Refresh",
            "Upload",
            "Delete",
            "Versions",
            "Download",
            "Name",
            "Size",
            "Modified",
            "Status",
            "Folder is empty — drag files here or click Upload",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_single_file_download(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "download_latest_file",
            "Gtk.FileDialog",
            "File exists",
            "Keep both",
            "Overwrite",
            "ProgressBar",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)


if __name__ == "__main__":
    unittest.main()
