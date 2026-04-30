"""Tests for the AppImage install hook (P.3b).

The hook drops a .desktop menu entry and an autostart entry on first
launch when $APPIMAGE is set, rewrites them silently if the AppImage
has moved, and respects the user removing the autostart entry.

File-manager send targets are owned by
``src.file_manager_integration`` (M.6); they are tested separately.
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

        self._patches = [
            mock.patch.object(hook, "DESKTOP_ENTRY_PATH", self._desktop),
            mock.patch.object(hook, "AUTOSTART_ENTRY_PATH", self._autostart),
        ]
        for p in self._patches:
            p.start()

        self._config_dir = self._home / ".config/desktop-connector"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._config = Config(self._config_dir)

        self._appimage = self._home / "Apps/desktop-connector-x86_64.AppImage"
        self._appimage.parent.mkdir(parents=True, exist_ok=True)
        self._appimage.write_text("#!/bin/bash\nexit 0\n")
        self._appimage.chmod(0o755)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

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

    def test_install_hook_no_longer_writes_file_manager_scripts(self):
        """File-manager send targets moved to file_manager_integration in M.6.
        The install hook must not even attempt to read $PATH for
        nautilus/nemo/dolphin — that's the new module's job, and on
        first launch with no pairings yet there is nothing to write.
        """
        nautilus_dir = self._home / ".local/share/nautilus/scripts"
        nemo_dir = self._home / ".local/share/nemo/scripts"
        dolphin_path = (
            self._home
            / ".local/share/kservices5/ServiceMenus/desktop-connector-send.desktop"
        )
        nautilus_dir.mkdir(parents=True, exist_ok=True)
        nemo_dir.mkdir(parents=True, exist_ok=True)

        self._run_with_appimage(self._appimage)

        self.assertEqual(list(nautilus_dir.iterdir()), [])
        self.assertEqual(list(nemo_dir.iterdir()), [])
        self.assertFalse(dolphin_path.exists())

    def test_missing_appimage_path_skips(self):
        # $APPIMAGE points to a path that doesn't exist (race during update)
        with mock.patch.dict(os.environ, {"APPIMAGE": "/nonexistent/foo.AppImage"}):
            hook.ensure_appimage_integration(self._config)
        self.assertFalse(self._desktop.exists())
        self.assertFalse(self._autostart.exists())
        self.assertFalse(self._config.appimage_install_hook_done)


if __name__ == "__main__":
    unittest.main()
