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
    constants patched onto it.

    Also mocks the user-feedback surfaces (_notify, the GTK4 modal,
    sys.stdout.isatty) so tests exercise the relocate *logic* without
    shelling out to notify-send or popping windows.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = Path(self._tmp.name)
        self._canonical_dir = self._home / ".local/share/desktop-connector"
        self._canonical = self._canonical_dir / "desktop-connector.AppImage"
        self._patches = [
            mock.patch.object(relocate, "CANONICAL_INSTALL_DIR", self._canonical_dir),
            mock.patch.object(relocate, "CANONICAL_APPIMAGE_PATH", self._canonical),
            # Don't shell out to notify-send during tests.
            mock.patch.object(relocate, "_notify"),
            # Force the print branch — modal would try to load GTK4 which
            # may conflict with pystray's GTK3 if anything pulled it in.
            mock.patch.object(sys.stdout, "isatty", return_value=True),
            # Skip the 500 ms spawn-poll wait + post-kill 100 ms loop —
            # tests can't rely on real wall-clock pacing.
            mock.patch.object(relocate.time, "sleep"),
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
                # Spawn-poll guard: poll() returning None means "still alive".
                popen.return_value.poll.return_value = None
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
            popen.return_value.poll.return_value = None
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
             mock.patch.object(relocate.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
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

    def test_relocate_returns_false_on_spawn_died_early(self):
        """Spawn poll guard: if the canonical exits in its first 500 ms
        (broken libc, missing FUSE, ENOEXEC), we report failure instead
        of leaving the user with the modal saying 'Installation complete'
        and no tray icon."""
        src = self._home / "Downloads/desktop-connector-x86_64.AppImage"
        self._make_source_appimage(src)
        with mock.patch.dict(os.environ, {"APPIMAGE": str(src)}), \
             mock.patch.object(relocate.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = 127  # exited with rc=127
            popen.return_value.returncode = 127
            ret = relocate.relocate_to_canonical_if_needed()
        self.assertFalse(ret)
        # File still got copied — there's a usable canonical on disk for
        # the next launch attempt; we just couldn't *run* it this time.
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


class FakeProcKillMatcherTests(unittest.TestCase):
    """Lay down a fake /proc tree under a tmp dir, point relocate's
    _PROC_ROOT at it, exercise the kill-matcher logic without mocking
    it out wholesale. Catches regressions in less-tested environments
    (containers with restricted procfs, processes whose environ isn't
    readable, /proc entries that disappear mid-scan)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._proc = Path(self._tmp.name) / "proc"
        self._proc.mkdir()
        self._patches = [
            mock.patch.object(relocate, "_PROC_ROOT", self._proc),
            # Don't actually SIGTERM real processes; record calls.
            mock.patch.object(relocate.os, "kill"),
            # is_persistent_mode reads sys.argv; force "tray mode" for these tests.
            mock.patch.object(relocate.sys, "argv", ["dc"]),
            # Same time-skip as _Sandbox so the post-kill 3 s wait
            # doesn't make tests slow.
            mock.patch.object(relocate.time, "sleep"),
            mock.patch.object(relocate.time, "monotonic", side_effect=[0, 999]),
            # _pid_alive is called after the kill loop; with mocked kill
            # the real pid never existed → kill(0) returns ESRCH → False.
            # Force the helper to short-circuit on our fake pids.
            mock.patch.object(relocate, "_pid_alive", return_value=False),
        ]
        for p in self._patches:
            p.start()
        self._kill_mock = relocate.os.kill

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _make_fake_proc(
        self,
        pid: int,
        *,
        cmdline: str = "python3.11\x00-m\x00src.main\x00",
        environ_lines: list[str] | None = None,
        cwd: str | None = None,
        unreadable_environ: bool = False,
    ):
        d = self._proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(cmdline.encode())
        if not unreadable_environ:
            env_blob = "\x00".join(environ_lines or [])
            (d / "environ").write_bytes(env_blob.encode())
        else:
            # Simulate a process whose environ we can't read (other user,
            # restricted procfs). Symlink at path that won't resolve.
            (d / "environ").symlink_to("/nonexistent/proc/environ")
        if cwd:
            (d / "cwd").symlink_to(cwd)

    # --- match shape ------------------------------------------------------

    def test_match_by_appimage_env(self):
        self._make_fake_proc(
            42,
            environ_lines=["APPIMAGE=/home/u/desktop-connector/foo.AppImage"],
            cwd="/tmp/whatever",
        )
        relocate._stop_other_instances()
        self.assertEqual(self._kill_mock.call_args_list[0].args[0], 42)
        self.assertEqual(self._kill_mock.call_args_list[0].args[1], relocate.signal.SIGTERM)

    def test_match_by_cwd_substring(self):
        self._make_fake_proc(
            43,
            environ_lines=["HOME=/home/u"],  # no APPIMAGE
            cwd="/home/u/.local/share/desktop-connector",
        )
        relocate._stop_other_instances()
        self.assertTrue(
            any(c.args[0] == 43 for c in self._kill_mock.call_args_list)
        )

    def test_no_match_without_src_main_cmdline(self):
        # python process but doing something else — must not be killed.
        self._make_fake_proc(
            44,
            cmdline="python3\x00-m\x00other.module\x00",
            environ_lines=["APPIMAGE=/home/u/desktop-connector/foo.AppImage"],
        )
        relocate._stop_other_instances()
        self.assertFalse(
            any(c.args[0] == 44 for c in self._kill_mock.call_args_list)
        )

    def test_no_match_when_neither_env_nor_cwd_match(self):
        self._make_fake_proc(
            45,
            environ_lines=["HOME=/home/u", "PATH=/usr/bin"],
            cwd="/some/unrelated/dir",
        )
        relocate._stop_other_instances()
        self.assertFalse(
            any(c.args[0] == 45 for c in self._kill_mock.call_args_list)
        )

    def test_no_match_when_environ_unreadable_and_cwd_doesnt_match(self):
        # Restricted procfs case: environ unreadable, cwd doesn't match.
        # Must NOT be killed (we can't confirm it's ours).
        self._make_fake_proc(
            46,
            unreadable_environ=True,
            cwd="/home/u/somewhere-else",
        )
        relocate._stop_other_instances()
        self.assertFalse(
            any(c.args[0] == 46 for c in self._kill_mock.call_args_list)
        )

    def test_match_when_environ_unreadable_but_cwd_matches(self):
        # Restricted environ but cwd is ours — still a match.
        self._make_fake_proc(
            47,
            unreadable_environ=True,
            cwd="/home/u/git/desktop-connector",
        )
        relocate._stop_other_instances()
        self.assertTrue(
            any(c.args[0] == 47 for c in self._kill_mock.call_args_list)
        )

    # --- skip filters -----------------------------------------------------

    def test_skips_self_pid(self):
        self_pid = os.getpid()
        self._make_fake_proc(
            self_pid,
            environ_lines=["APPIMAGE=/x/desktop-connector/y.AppImage"],
        )
        relocate._stop_other_instances()
        self.assertFalse(
            any(c.args[0] == self_pid for c in self._kill_mock.call_args_list)
        )

    def test_skips_parent_pid(self):
        parent_pid = os.getppid()
        self._make_fake_proc(
            parent_pid,
            environ_lines=["APPIMAGE=/x/desktop-connector/y.AppImage"],
        )
        relocate._stop_other_instances()
        self.assertFalse(
            any(c.args[0] == parent_pid for c in self._kill_mock.call_args_list)
        )

    def test_skips_non_pid_proc_entries(self):
        # /proc has lots of non-pid entries (self, sys, kcore, etc.) —
        # those have non-numeric names and must be skipped without
        # raising.
        (self._proc / "self").mkdir()
        (self._proc / "sys").mkdir()
        (self._proc / "kallsyms").write_text("")
        # Plus one real match to confirm the loop didn't bail early.
        self._make_fake_proc(
            48, environ_lines=["APPIMAGE=/x/desktop-connector/y.AppImage"]
        )
        relocate._stop_other_instances()
        self.assertTrue(
            any(c.args[0] == 48 for c in self._kill_mock.call_args_list)
        )

    def test_handles_cmdline_unreadable(self):
        # Process exists but we can't read /proc/<pid>/cmdline (race +
        # perms). Skip it.
        d = self._proc / "49"
        d.mkdir()
        (d / "cmdline").symlink_to("/nonexistent/cmdline")
        relocate._stop_other_instances()
        # Just shouldn't crash; nothing was killed.
        self.assertEqual(self._kill_mock.call_count, 0)

    def test_kill_oserror_swallowed(self):
        self._make_fake_proc(
            50,
            environ_lines=["APPIMAGE=/x/desktop-connector/y.AppImage"],
        )
        # Simulate the process disappearing between scan and kill.
        self._kill_mock.side_effect = ProcessLookupError()
        relocate._stop_other_instances()
        # Single kill attempt was made; OSError was swallowed.
        self.assertEqual(self._kill_mock.call_count, 1)

    def test_proc_not_a_dir_returns_silently(self):
        # Containers / minimal namespaces sometimes lack /proc.
        with mock.patch.object(
            relocate, "_PROC_ROOT", Path("/nonexistent-proc-root")
        ):
            relocate._stop_other_instances()
        self.assertEqual(self._kill_mock.call_count, 0)


if __name__ == "__main__":
    unittest.main()
