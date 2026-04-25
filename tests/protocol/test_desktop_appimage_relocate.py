"""Tests for the AppImage self-install relocation hook.

Covers the trigger gate (env var, $APPIMAGE, canonical-path check),
the side-effect sequence (stop canonical → copy → spawn), and the
"return value" contract that signals the caller to exit.

Real /proc scanning + Popen + chmod are mocked. The full
"AppImage relocates and runs" loop is exercised in the live cutover
smoke (running ./temp/appimage-out/desktop-connector-x86_64.AppImage
should end up running the canonical copy).
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

from src.bootstrap import appimage_relocate as relocate  # noqa: E402


class _Sandbox(unittest.TestCase):
    """Each test runs in its own tmp tree with the canonical-path
    constants patched onto it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = Path(self._tmp.name)
        self._canonical_dir = self._home / ".local/share/desktop-connector"
        self._canonical = self._canonical_dir / "desktop-connector.AppImage"
        self._patches = [
            mock.patch.object(relocate, "CANONICAL_INSTALL_DIR", self._canonical_dir),
            mock.patch.object(relocate, "CANONICAL_APPIMAGE_PATH", self._canonical),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _make_source_appimage(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF\x02\x01\x01\x00fake-AppImage-bytes\n")
        path.chmod(0o755)
        return path


class GateTests(_Sandbox):
    def test_no_relocate_outside_appimage(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        env.pop("DC_NO_RELOCATE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(relocate.subprocess, "Popen") as p:
                self.assertFalse(relocate.relocate_to_canonical_if_needed())
            p.assert_not_called()

    def test_no_relocate_when_already_canonical(self):
        # Source == canonical
        self._make_source_appimage(self._canonical)
        with mock.patch.dict(os.environ, {"APPIMAGE": str(self._canonical)}):
            with mock.patch.object(relocate.subprocess, "Popen") as p:
                self.assertFalse(relocate.relocate_to_canonical_if_needed())
            p.assert_not_called()

    def test_no_relocate_with_env_override(self):
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        with mock.patch.dict(
            os.environ, {"APPIMAGE": str(src), "DC_NO_RELOCATE": "1"}
        ):
            with mock.patch.object(relocate.subprocess, "Popen") as p:
                self.assertFalse(relocate.relocate_to_canonical_if_needed())
            p.assert_not_called()


class RelocationTests(_Sandbox):
    def test_relocate_copies_and_spawns(self):
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}, clear=False):
            os.environ.pop("DC_NO_RELOCATE", None)
            with mock.patch.object(relocate.subprocess, "Popen") as popen:
                ret = relocate.relocate_to_canonical_if_needed()
        self.assertTrue(ret)
        # Canonical now exists with the same bytes
        self.assertTrue(self._canonical.exists())
        self.assertEqual(self._canonical.read_bytes(), src.read_bytes())
        self.assertTrue(self._canonical.stat().st_mode & 0o111)
        # Spawn fired with canonical path as argv[0]
        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], str(self._canonical.resolve()))
        # start_new_session=True so the spawn detaches from this group
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))

    def test_relocate_preserves_argv_tail(self):
        src = self._home / "build/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        # Simulate the user running with extra flags
        argv = ["script-name", "--headless", "--config-dir=/tmp/foo"]
        with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}), \
             mock.patch.object(relocate.sys, "argv", argv), \
             mock.patch.object(relocate.subprocess, "Popen") as popen:
            self.assertTrue(relocate.relocate_to_canonical_if_needed())
        cmd = popen.call_args.args[0]
        # argv[0] is replaced with canonical; rest preserved
        self.assertEqual(cmd[1:], ["--headless", "--config-dir=/tmp/foo"])

    def test_relocate_creates_install_dir_if_missing(self):
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        # Canonical dir doesn't exist yet
        self.assertFalse(self._canonical_dir.exists())
        with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}), \
             mock.patch.object(relocate.subprocess, "Popen"):
            self.assertTrue(relocate.relocate_to_canonical_if_needed())
        self.assertTrue(self._canonical_dir.is_dir())

    def test_relocate_returns_false_on_copy_failure(self):
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}), \
             mock.patch.object(relocate.shutil, "copy2", side_effect=OSError("disk full")), \
             mock.patch.object(relocate.subprocess, "Popen") as popen:
            ret = relocate.relocate_to_canonical_if_needed()
        self.assertFalse(ret)
        popen.assert_not_called()

    def test_relocate_returns_false_on_spawn_failure(self):
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}), \
             mock.patch.object(
                 relocate.subprocess, "Popen", side_effect=OSError("ENOEXEC")
             ):
            ret = relocate.relocate_to_canonical_if_needed()
        self.assertFalse(ret)
        # File got copied, even though we couldn't spawn — caller proceeds
        # with the in-process bootstrap, on the OLD path's mmap.
        self.assertTrue(self._canonical.exists())


class CleanEnvTests(unittest.TestCase):
    """The spawned canonical must not inherit /tmp/.mount_*/ paths from
    its non-canonical parent — Python's import machinery follows
    PYTHONPATH at startup and chokes when an entry has unmounted."""

    def test_strips_mount_entries_from_path_like_vars(self):
        env = {
            "PATH": "/tmp/.mount_xyz/opt/python3.11/bin:/usr/bin:/bin",
            "LD_LIBRARY_PATH": "/tmp/.mount_xyz/usr/lib:/usr/local/lib",
            "PYTHONPATH": "/tmp/.mount_xyz/usr/lib/desktop-connector",
            "GI_TYPELIB_PATH": "/tmp/.mount_xyz/usr/lib/girepository-1.0",
            "GSETTINGS_SCHEMA_DIR": "/tmp/.mount_xyz/usr/share/glib-2.0/schemas",
            "XDG_DATA_DIRS": "/tmp/.mount_xyz/usr/share:/usr/local/share:/usr/share",
        }
        with mock.patch.object(relocate.os, "environ", env):
            cleaned = relocate._clean_env_for_spawn()
        self.assertEqual(cleaned["PATH"], "/usr/bin:/bin")
        self.assertEqual(cleaned["LD_LIBRARY_PATH"], "/usr/local/lib")
        self.assertNotIn("PYTHONPATH", cleaned)  # only had the mount entry
        self.assertNotIn("GI_TYPELIB_PATH", cleaned)
        self.assertNotIn("GSETTINGS_SCHEMA_DIR", cleaned)
        self.assertEqual(cleaned["XDG_DATA_DIRS"], "/usr/local/share:/usr/share")

    def test_clears_runtime_single_path_vars(self):
        env = {
            "APPDIR": "/tmp/.mount_xyz",
            "APPIMAGE": "/home/u/foo.AppImage",
            "ARGV0": "./foo.AppImage",
            "OWD": "/home/u",
            "PYTHONHOME": "/tmp/.mount_xyz/opt/python3.11",
            "GDK_PIXBUF_MODULE_FILE": "/tmp/.mount_xyz/usr/lib/gdk-pixbuf-2.0/loaders.cache",
            "WEBKIT_EXEC_PATH": "/tmp/.mount_xyz/usr/lib/webkitgtk-6.0",
            "HOME": "/home/u",
        }
        with mock.patch.object(relocate.os, "environ", env):
            cleaned = relocate._clean_env_for_spawn()
        for var in (
            "APPDIR",
            "APPIMAGE",
            "ARGV0",
            "OWD",
            "PYTHONHOME",
            "GDK_PIXBUF_MODULE_FILE",
            "WEBKIT_EXEC_PATH",
        ):
            self.assertNotIn(var, cleaned, f"{var} should be stripped")
        # Untouched vars survive
        self.assertEqual(cleaned["HOME"], "/home/u")

    def test_preserves_user_paths_when_no_mount_prefix(self):
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "XDG_DATA_DIRS": "/home/u/.local/share:/usr/share",
            "HOME": "/home/u",
        }
        with mock.patch.object(relocate.os, "environ", env):
            cleaned = relocate._clean_env_for_spawn()
        self.assertEqual(cleaned["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertEqual(cleaned["XDG_DATA_DIRS"], "/home/u/.local/share:/usr/share")


class PersistentModeTests(unittest.TestCase):
    def test_default_argv_is_persistent(self):
        with mock.patch.object(relocate.sys, "argv", ["dc"]):
            self.assertTrue(relocate.is_persistent_mode())

    def test_headless_is_persistent(self):
        with mock.patch.object(relocate.sys, "argv", ["dc", "--headless"]):
            self.assertTrue(relocate.is_persistent_mode())

    def test_send_is_transient(self):
        with mock.patch.object(relocate.sys, "argv", ["dc", "--send=/tmp/x"]):
            self.assertFalse(relocate.is_persistent_mode())
        with mock.patch.object(
            relocate.sys, "argv", ["dc", "--send", "/tmp/x"]
        ):
            self.assertFalse(relocate.is_persistent_mode())

    def test_pair_is_transient(self):
        with mock.patch.object(relocate.sys, "argv", ["dc", "--pair"]):
            self.assertFalse(relocate.is_persistent_mode())


class EnforceSingleInstanceTests(_Sandbox):
    def test_no_op_in_transient_mode(self):
        with mock.patch.object(relocate.sys, "argv", ["dc", "--send=/x"]):
            with mock.patch.object(relocate, "_stop_other_instances") as s:
                relocate.enforce_single_instance()
        s.assert_not_called()

    def test_fires_in_persistent_mode(self):
        with mock.patch.object(relocate.sys, "argv", ["dc"]):
            with mock.patch.object(relocate, "_stop_other_instances") as s:
                relocate.enforce_single_instance()
        s.assert_called_once()


class RelocateGatesOnPersistentMode(_Sandbox):
    def test_relocate_skipped_for_send_even_from_non_canonical(self):
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        with mock.patch.object(
            relocate.sys, "argv", ["dc", "--send=/tmp/foo"]
        ):
            with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}):
                with mock.patch.object(relocate.subprocess, "Popen") as p:
                    self.assertFalse(relocate.relocate_to_canonical_if_needed())
                p.assert_not_called()
        # Canonical was NOT created
        self.assertFalse(self._canonical.exists())

    def test_relocate_skipped_for_pair(self):
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        with mock.patch.object(relocate.sys, "argv", ["dc", "--pair"]):
            with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}):
                with mock.patch.object(relocate.subprocess, "Popen") as p:
                    self.assertFalse(relocate.relocate_to_canonical_if_needed())
                p.assert_not_called()


if __name__ == "__main__":
    unittest.main()
