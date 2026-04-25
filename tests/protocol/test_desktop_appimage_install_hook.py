"""Tests for the AppImage install hook (P.3b).

The hook drops a .desktop menu entry and an autostart entry on first
launch when $APPIMAGE is set, rewrites them silently if the AppImage
has moved, and respects the user removing the autostart entry.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.bootstrap import appimage_install_hook as hook  # noqa: E402
from src.config import Config  # noqa: E402


class AppImageInstallHookTests(unittest.TestCase):
    def setUp(self):
        # Sandbox HOME so we don't touch the developer's real
        # ~/.local/share/applications etc. The hook reads HOME at import
        # time into module-level path constants, so we patch those.
        self._tmp = tempfile.TemporaryDirectory()
        self._home = Path(self._tmp.name)
        (self._home / ".local/share/applications").mkdir(parents=True, exist_ok=True)
        (self._home / ".config/autostart").mkdir(parents=True, exist_ok=True)
        self._desktop = self._home / ".local/share/applications/desktop-connector.desktop"
        self._autostart = self._home / ".config/autostart/desktop-connector.desktop"
        self._nautilus = self._home / ".local/share/nautilus/scripts/Send to Phone"
        self._nemo = self._home / ".local/share/nemo/scripts/Send to Phone"
        self._dolphin = (
            self._home / ".local/share/kservices5/ServiceMenus/desktop-connector-send.desktop"
        )

        self._patches = [
            mock.patch.object(hook, "DESKTOP_ENTRY_PATH", self._desktop),
            mock.patch.object(hook, "AUTOSTART_ENTRY_PATH", self._autostart),
            mock.patch.object(hook, "NAUTILUS_SCRIPT_PATH", self._nautilus),
            mock.patch.object(hook, "NEMO_SCRIPT_PATH", self._nemo),
            mock.patch.object(hook, "DOLPHIN_SERVICE_PATH", self._dolphin),
        ]
        for p in self._patches:
            p.start()
        # By default no file managers exist; individual tests opt in.
        self._which_patch = mock.patch.object(hook.shutil, "which", return_value=None)
        self._which_mock = self._which_patch.start()

        self._config_dir = self._home / ".config/desktop-connector"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._config = Config(self._config_dir)

        self._appimage = self._home / "Apps/desktop-connector-x86_64.AppImage"
        self._appimage.parent.mkdir(parents=True, exist_ok=True)
        self._appimage.write_text("#!/bin/bash\nexit 0\n")
        self._appimage.chmod(0o755)

    def tearDown(self):
        self._which_patch.stop()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _set_file_managers(self, *names):
        """Make hook.shutil.which see the named binaries as installed."""
        names_set = set(names)
        self._which_mock.side_effect = lambda x: f"/usr/bin/{x}" if x in names_set else None

    def _run_with_appimage(self, appimage_path):
        with mock.patch.dict(os.environ, {"APPIMAGE": str(appimage_path)}):
            hook.ensure_appimage_integration(self._config)

    def test_no_op_outside_appimage(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            hook.ensure_appimage_integration(self._config)
        self.assertFalse(self._desktop.exists())
        self.assertFalse(self._autostart.exists())
        self.assertFalse(self._config.appimage_install_hook_done)

    def test_first_launch_writes_both_entries(self):
        self._run_with_appimage(self._appimage)
        self.assertTrue(self._desktop.exists())
        self.assertTrue(self._autostart.exists())
        self.assertIn(f"Exec={self._appimage}", self._desktop.read_text())
        self.assertIn(f"Exec={self._appimage}", self._autostart.read_text())
        self.assertIn("StartupWMClass=com.desktopconnector.Desktop", self._desktop.read_text())
        self.assertIn("Categories=Network;Utility;", self._desktop.read_text())
        self.assertTrue(self._config.appimage_install_hook_done)

    def test_move_rewrites_silently(self):
        self._run_with_appimage(self._appimage)
        moved = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        moved.parent.mkdir(parents=True, exist_ok=True)
        moved.write_text("#!/bin/bash\nexit 0\n")
        moved.chmod(0o755)
        self._run_with_appimage(moved)
        self.assertIn(f"Exec={moved}", self._desktop.read_text())
        self.assertIn(f"Exec={moved}", self._autostart.read_text())

    def test_removed_autostart_stays_gone(self):
        self._run_with_appimage(self._appimage)
        self.assertTrue(self._autostart.exists())
        self._autostart.unlink()
        self._run_with_appimage(self._appimage)
        self.assertFalse(self._autostart.exists())
        # Menu entry should still get rewritten if needed (here untouched).
        self.assertTrue(self._desktop.exists())

    def test_removed_menu_entry_stays_gone(self):
        self._run_with_appimage(self._appimage)
        self._desktop.unlink()
        self._run_with_appimage(self._appimage)
        self.assertFalse(self._desktop.exists())

    def test_no_autostart_marker_blocks_creation(self):
        marker = self._config_dir / hook.NO_AUTOSTART_MARKER
        marker.touch()
        self._run_with_appimage(self._appimage)
        self.assertTrue(self._desktop.exists())
        self.assertFalse(self._autostart.exists())

    def test_unchanged_path_does_not_rewrite(self):
        self._run_with_appimage(self._appimage)
        first_mtime = self._desktop.stat().st_mtime_ns
        # Tweak the file so we can detect a rewrite
        self._desktop.write_text(self._desktop.read_text() + "# user-comment\n")
        marker_mtime = self._desktop.stat().st_mtime_ns
        self.assertGreater(marker_mtime, first_mtime)
        self._run_with_appimage(self._appimage)
        # Same Exec=, same path → hook should NOT touch the file again.
        self.assertEqual(self._desktop.stat().st_mtime_ns, marker_mtime)

    def test_file_manager_scripts_created_when_present(self):
        self._set_file_managers("nautilus", "nemo", "dolphin")
        self._run_with_appimage(self._appimage)
        self.assertTrue(self._nautilus.exists())
        self.assertTrue(self._nemo.exists())
        self.assertTrue(self._dolphin.exists())
        self.assertTrue(self._nautilus.stat().st_mode & 0o111)
        self.assertIn(str(self._appimage), self._nautilus.read_text())
        self.assertIn(
            f"Exec={self._appimage} --headless --send=%f",
            self._dolphin.read_text(),
        )

    def test_file_manager_scripts_skipped_when_absent(self):
        self._set_file_managers()
        self._run_with_appimage(self._appimage)
        self.assertFalse(self._nautilus.exists())
        self.assertFalse(self._nemo.exists())
        self.assertFalse(self._dolphin.exists())

    def test_only_installed_file_manager_gets_script(self):
        self._set_file_managers("nemo")
        self._run_with_appimage(self._appimage)
        self.assertFalse(self._nautilus.exists())
        self.assertTrue(self._nemo.exists())
        self.assertFalse(self._dolphin.exists())

    def test_file_manager_script_rewrites_on_move(self):
        self._set_file_managers("nautilus", "dolphin")
        self._run_with_appimage(self._appimage)
        moved = self._home / "Apps2/desktop-connector-x86_64.AppImage"
        moved.parent.mkdir(parents=True, exist_ok=True)
        moved.write_text("#!/bin/bash\n")
        moved.chmod(0o755)
        self._run_with_appimage(moved)
        self.assertIn(str(moved), self._nautilus.read_text())
        self.assertIn(f"Exec={moved}", self._dolphin.read_text())

    def test_file_manager_removed_stays_gone(self):
        self._set_file_managers("nautilus")
        self._run_with_appimage(self._appimage)
        self._nautilus.unlink()
        self._run_with_appimage(self._appimage)
        self.assertFalse(self._nautilus.exists())

    def test_missing_appimage_path_skips(self):
        # $APPIMAGE points to a path that doesn't exist (race during update)
        with mock.patch.dict(os.environ, {"APPIMAGE": "/nonexistent/foo.AppImage"}):
            hook.ensure_appimage_integration(self._config)
        self.assertFalse(self._desktop.exists())
        self.assertFalse(self._autostart.exists())
        self.assertFalse(self._config.appimage_install_hook_done)


if __name__ == "__main__":
    unittest.main()
