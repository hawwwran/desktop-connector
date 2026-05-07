"""T5.1 source pins for the Vault browser GTK entry point.

This is one of five ``*_source.py`` files in ``tests/protocol/``
that are deliberately *source-text greppers*, not behavioural tests.
They open the GTK4 module on disk and assert that specific UI strings
or import lines appear in it. Per F-T08 (slice 07 review):

- **Scope**: anti-regression for UI strings + import sites that the
  Maintenance / Folders / Browser / Wizard / Disconnect surfaces
  rely on. If someone renames "Open Vault…" to "Browse vault" or
  drops the ``upload_file`` import, this file fails immediately.
- **Not coverage**: these tests neither construct the widgets nor
  drive them. Behavioural correctness for the underlying logic
  lives in ``test_desktop_vault_*.py`` (no ``_source`` suffix) —
  those exercise the data layer with real fixtures.
- **Why keep them**: GTK4 widgets cannot be instantiated headlessly
  in CI without a display server, and dogtail-driven smoke tests
  need a Wayland session. Source-pinning is the cheapest layer that
  catches the specific class of regressions UI work introduces.

Take any failure here as: "the UI was structurally edited; review
the change to confirm the affected surface still does what these
strings advertise". Don't add behavioural assertions to these
files — extend the matching ``test_desktop_vault_*.py`` instead.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


_BROWSER_PKG = Path(REPO_ROOT, "desktop", "src", "windows_vault_browser")


def _read_browser_source() -> str:
    """Concatenate every module under ``windows_vault_browser/`` so the
    source-pin greppers continue to find UI strings + import lines no
    matter which submodule a closure ended up in. Same shape as the
    ``_read_windows_vault_pkg`` helper in test_desktop_vault_a11y_source."""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(_BROWSER_PKG.glob("*.py"))
    )


class VaultBrowserGtkSourceTests(unittest.TestCase):
    def test_window_dispatcher_registers_vault_browser(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows.py").read_text(encoding="utf-8")

        self.assertIn("from .windows_vault_browser import show_vault_browser", source)
        self.assertIn('"vault-browser"', source)
        # F-U14: dispatcher threads ``vault_id_override`` so a future
        # multi-vault tray (or smoke-test driver) can repoint the
        # browser without rewriting ``last_known_id`` on disk.
        self.assertIn(
            "show_vault_browser(config_dir, vault_id_override=vault_id_override)",
            source,
        )

    def test_tray_open_vault_launches_browser_not_settings(self) -> None:
        # tray.py is now a package; the vault submenu lives in
        # tray/vault_submenu.py.
        source = Path(REPO_ROOT, "desktop/src/tray/vault_submenu.py").read_text(encoding="utf-8")

        self.assertIn('"Open Vault…",\n                self._spawn_vault_browser', source)
        self.assertIn('self._open_gtk4_window("vault-browser")', source)
        self.assertIn('self._open_gtk4_window("vault-main")', source)

    def test_browser_window_has_t5_surface_labels(self) -> None:
        source = _read_browser_source()

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
        source = _read_browser_source()

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
        source = _read_browser_source()

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
        source = _read_browser_source()

        for text in (
            "from ..vault_upload import upload_file",
            "_resolve_upload_destination",
            "Upload file to vault",
            "VaultQuotaExceededError",
            "result.skipped_identical",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_soft_delete_and_show_deleted_toggle(self) -> None:
        source = _read_browser_source()

        for text in (
            'Gtk.CheckButton(label="Show deleted")',
            "from ..vault_delete import delete_file",
            "from ..vault_delete import delete_folder_contents",
            "from ..vault_delete import restore_version_to_current",
            "_confirm_delete_file",
            "_confirm_delete_folder",
            "_confirm_restore_version",
            "Restore as current",
            # v2 reads the dataclass field directly instead of dict-getting
            # off a flat ``state`` closure capture.
            "include_deleted = self.state.show_deleted",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_routes_507_through_quota_helper(self) -> None:
        source = _read_browser_source()

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
        source = _read_browser_source()

        for text in (
            "from ..vault_eviction import eviction_pass",
            "_run_eviction_pass",
            "no_more_candidates",
            "Open vault settings",
            "Reclaiming space",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_upload_resume_banner(self) -> None:
        source = _read_browser_source()

        for text in (
            "list_resumable_sessions",
            "default_upload_resume_dir",
            "start_resume_pending",
            "from ..vault_upload import resume_upload",
            "uploads were interrupted",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_recursive_folder_upload(self) -> None:
        source = _read_browser_source()

        for text in (
            # v2 splits the constructor args across lines; the label
            # literal is the stable anchor.
            'label="Upload folder"',
            "from ..vault_upload import upload_folder",
            "select_folder_finish",
            "Upload folder to vault",
            "start_folder_upload",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_browser_window_wires_upload_conflict_prompt(self) -> None:
        source = _read_browser_source()

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
        source = _read_browser_source()

        for text in (
            "list_versions",
            "previous_version_filename",
            "download_version",
            "render_versions_section",
            "Download previous version",
            # Hyphenated in the conflict-banner copy ("side-path name").
            "side-path",
            "Version file exists",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)


if __name__ == "__main__":
    unittest.main()
