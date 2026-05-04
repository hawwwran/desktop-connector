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

    def test_browser_window_wires_recursive_folder_download(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "download_folder",
            "select_folder",
            "Folder exists",
            "Overwrite matching files",
            "Download saves this folder recursively.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_single_file_upload(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "from .vault_upload import upload_file",
            "_resolve_upload_destination",
            "Upload file to vault",
            "VaultQuotaExceededError",
            "result.skipped_identical",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_soft_delete_and_show_deleted_toggle(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            'Gtk.CheckButton(label="Show deleted")',
            "from .vault_delete import delete_file",
            "from .vault_delete import delete_folder_contents",
            "from .vault_delete import restore_version_to_current",
            "_confirm_delete_file",
            "_confirm_delete_folder",
            "_confirm_restore_version",
            "Restore as current",
            'include_deleted = bool(state.get("show_deleted"))',
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_routes_507_through_quota_helper(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "describe_quota_exceeded",
            "_handle_quota_exceeded",
            "quota_banner",
            'action="Upload"',
            'action="Folder upload"',
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_make_space_runs_eviction_pass(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "from .vault_eviction import eviction_pass",
            "_run_eviction_pass",
            "no_more_candidates",
            "Open vault settings",
            "Reclaiming space",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_upload_resume_banner(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "list_resumable_sessions",
            "default_upload_resume_dir",
            "start_resume_pending",
            "from .vault_upload import resume_upload",
            "uploads were interrupted",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_recursive_folder_upload(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            'Gtk.Button(label="Upload folder"',
            "from .vault_upload import upload_folder",
            "select_folder_finish",
            "Upload folder to vault",
            "start_folder_upload",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_upload_conflict_prompt(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "_maybe_prompt_conflict_then_upload",
            "detect_path_conflict",
            "make_conflict_renamed_path",
            "Add as new version",
            "Keep both with rename",
            '"new_file_only"',
            "device_name",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_versions_panel_and_download(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_browser.py").read_text(
            encoding="utf-8"
        )

        for text in (
            "list_versions",
            "previous_version_filename",
            "download_version",
            "render_versions_section",
            "Download previous version",
            "side path",
            "Version file exists",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)


if __name__ == "__main__":
    unittest.main()
