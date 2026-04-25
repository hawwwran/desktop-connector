"""Tests for the AppImageUpdate runner + dismissal helpers (P.6b).

`run_update` is a thin wrapper around appimageupdatetool — we mock the
subprocess and assert on shape, gating, and stdout streaming. The full
end-to-end "AppImage updates itself in place" loop is exercised in the
real-AppImage smoke check, not here.

Dismissal helpers live in version_check.py but are P.6b in scope; tested
here alongside the runner since they're co-conceptual ("user said don't
bug me about this version").
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.updater import update_runner, version_check  # noqa: E402


class _PathResolutionTests(unittest.TestCase):
    def test_appimageupdatetool_path_none_outside_appimage(self):
        env = dict(os.environ)
        env.pop("APPDIR", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(update_runner.appimageupdatetool_path())

    def test_appimageupdatetool_path_none_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"APPDIR": d}):
                self.assertIsNone(update_runner.appimageupdatetool_path())

    def test_appimageupdatetool_path_resolves_when_bundled(self):
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / update_runner.APPIMAGEUPDATETOOL_RELATIVE
            tool.parent.mkdir(parents=True)
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {"APPDIR": d}):
                self.assertEqual(update_runner.appimageupdatetool_path(), tool)

    def test_appimage_path_none_when_unset(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(update_runner.appimage_path())

    def test_appimage_path_none_when_path_missing(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/nonexistent/foo.AppImage"}):
            self.assertIsNone(update_runner.appimage_path())

    def test_appimage_path_resolves_when_present(self):
        with tempfile.NamedTemporaryFile() as f:
            with mock.patch.dict(os.environ, {"APPIMAGE": f.name}):
                self.assertEqual(update_runner.appimage_path(), Path(f.name))


class _RunUpdateTests(unittest.TestCase):
    """run_update behaviour with a mocked appimageupdatetool subprocess."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._appdir = Path(self._tmp.name)
        # Lay down the bundled tool path so resolution succeeds.
        self._tool = self._appdir / update_runner.APPIMAGEUPDATETOOL_RELATIVE
        self._tool.parent.mkdir(parents=True)
        self._tool.write_text("#!/bin/sh\nexit 0\n")
        self._tool.chmod(0o755)
        # And a fake $APPIMAGE.
        self._appimage = self._appdir / "fake.AppImage"
        self._appimage.write_text("#!/bin/sh\nexit 0\n")
        self._appimage.chmod(0o755)
        self._env = mock.patch.dict(
            os.environ,
            {
                "APPDIR": str(self._appdir),
                "APPIMAGE": str(self._appimage),
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _stub_proc(self, *, stdout_lines, returncode):
        proc = mock.Mock()
        proc.stdout = io.StringIO("\n".join(stdout_lines) + ("\n" if stdout_lines else ""))
        proc.wait.return_value = returncode
        return proc

    def test_returns_failed_when_tool_missing(self):
        env = dict(os.environ)
        env.pop("APPDIR", None)
        with mock.patch.dict(os.environ, env, clear=True):
            statuses = []
            outcome = update_runner.run_update(on_status=statuses.append)
        self.assertEqual(outcome, update_runner.UpdateOutcome.FAILED)
        self.assertTrue(any("not bundled" in s.lower() for s in statuses))

    def test_returns_failed_when_appimage_missing(self):
        # clear=True wipes os.environ first; we need APPDIR back so the
        # tool resolves, but APPIMAGE absent so the second check fails.
        with mock.patch.dict(
            os.environ, {"APPDIR": str(self._appdir)}, clear=True
        ):
            statuses = []
            outcome = update_runner.run_update(on_status=statuses.append)
        self.assertEqual(outcome, update_runner.UpdateOutcome.FAILED)
        self.assertTrue(any("appimage path" in s.lower() for s in statuses))

    def test_invokes_appimageupdatetool_with_extract_and_run(self):
        proc = self._stub_proc(stdout_lines=["Update completed"], returncode=0)
        with mock.patch.object(update_runner.subprocess, "Popen", return_value=proc) as p, \
             mock.patch.object(
                 update_runner, "_file_sha256",
                 side_effect=["sha-before", "sha-after"],  # different = updated
             ):
            outcome = update_runner.run_update()
        self.assertEqual(outcome, update_runner.UpdateOutcome.UPDATED)
        cmd = p.call_args.args[0]
        self.assertEqual(cmd[0], str(self._tool))
        self.assertIn("--appimage-extract-and-run", cmd)
        self.assertIn(str(self._appimage), cmd)

    def test_streams_stdout_lines_to_callback(self):
        lines = [
            "Reading remote zsync metadata…",
            "Downloading 12 of 320 blocks",
            "Verifying signature…",
            "Update completed",
        ]
        proc = self._stub_proc(stdout_lines=lines, returncode=0)
        statuses = []
        with mock.patch.object(update_runner.subprocess, "Popen", return_value=proc), \
             mock.patch.object(
                 update_runner, "_file_sha256",
                 side_effect=["sha-before", "sha-after"],
             ):
            update_runner.run_update(on_status=statuses.append)
        # Each non-empty line should reach the callback in order.
        self.assertEqual(statuses[1:1 + len(lines)], lines)

    def test_returns_failed_on_non_zero_exit(self):
        proc = self._stub_proc(stdout_lines=["Network error"], returncode=2)
        statuses = []
        with mock.patch.object(update_runner.subprocess, "Popen", return_value=proc), \
             mock.patch.object(update_runner, "_file_sha256", return_value="x"):
            outcome = update_runner.run_update(on_status=statuses.append)
        self.assertEqual(outcome, update_runner.UpdateOutcome.FAILED)
        self.assertTrue(any("Update failed (exit 2)" in s for s in statuses))

    def test_returns_failed_on_spawn_oserror(self):
        statuses = []
        with mock.patch.object(
            update_runner.subprocess, "Popen", side_effect=OSError("ENOENT")
        ), mock.patch.object(update_runner, "_file_sha256", return_value="x"):
            outcome = update_runner.run_update(on_status=statuses.append)
        self.assertEqual(outcome, update_runner.UpdateOutcome.FAILED)
        self.assertTrue(any("Could not start" in s for s in statuses))

    def test_returns_no_change_when_sha_unchanged(self):
        """Tool exits 0 (success) but the AppImage on disk is byte-
        identical — user was already on the latest version. Tray will
        notify "Already up to date" and skip the relaunch."""
        proc = self._stub_proc(stdout_lines=["No update available"], returncode=0)
        statuses = []
        with mock.patch.object(update_runner.subprocess, "Popen", return_value=proc), \
             mock.patch.object(
                 update_runner, "_file_sha256",
                 # Both pre and post return the same hash → NO_CHANGE.
                 return_value="sha-identical",
             ):
            outcome = update_runner.run_update(on_status=statuses.append)
        self.assertEqual(outcome, update_runner.UpdateOutcome.NO_CHANGE)
        self.assertTrue(any("up to date" in s.lower() for s in statuses))

    def test_treats_sha_read_failure_as_updated(self):
        """If we couldn't sample the sha (file unreadable for some reason)
        we conservatively assume an update happened — better to relaunch
        unnecessarily than to silently swallow a real update."""
        proc = self._stub_proc(stdout_lines=["Update completed"], returncode=0)
        with mock.patch.object(update_runner.subprocess, "Popen", return_value=proc), \
             mock.patch.object(update_runner, "_file_sha256", return_value=None):
            outcome = update_runner.run_update()
        self.assertEqual(outcome, update_runner.UpdateOutcome.UPDATED)


class _DismissalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {"XDG_CACHE_HOME": self._tmp.name, "APPIMAGE": "/tmp/x.AppImage"},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_not_dismissed_by_default(self):
        self.assertFalse(version_check.is_version_dismissed("0.2.0"))

    def test_dismiss_then_query(self):
        version_check.dismiss_version("0.2.0")
        self.assertTrue(version_check.is_version_dismissed("0.2.0"))
        self.assertFalse(version_check.is_version_dismissed("0.3.0"))

    def test_dismiss_is_idempotent(self):
        version_check.dismiss_version("0.2.0")
        version_check.dismiss_version("0.2.0")
        # Read the file directly: should appear once, not twice.
        import json

        cache = json.loads(version_check.cache_path().read_text())
        self.assertEqual(cache["dismissed_versions"], ["0.2.0"])

    def test_dismiss_preserves_existing_release_cache(self):
        # Seed a release into the cache, then dismiss; the release entry
        # must survive (the dismissal helper rewrites the same file).
        import json
        import time

        path = version_check.cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": int(time.time()),
                    "last_modified": "Mon",
                    "release": {
                        "tag_name": "desktop/v0.2.0",
                        "html_url": "https://example",
                        "asset_url": "https://example/x.AppImage",
                    },
                }
            )
        )
        version_check.dismiss_version("0.2.0")
        cache = json.loads(path.read_text())
        self.assertEqual(cache["release"]["tag_name"], "desktop/v0.2.0")
        self.assertEqual(cache["dismissed_versions"], ["0.2.0"])


if __name__ == "__main__":
    unittest.main()
