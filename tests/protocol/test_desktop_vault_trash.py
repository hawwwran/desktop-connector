"""T11.4 — Trash-on-delete helper."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

import src.vault_trash as trash_mod  # noqa: E402
from src.vault_trash import can_use_trash, trash_path  # noqa: E402


class CanUseTrashTests(unittest.TestCase):
    def test_returns_true_when_gio_present(self) -> None:
        with mock.patch.object(trash_mod.shutil, "which", return_value="/usr/bin/gio"):
            self.assertTrue(can_use_trash())

    def test_returns_false_when_gio_missing(self) -> None:
        with mock.patch.object(trash_mod.shutil, "which", return_value=None):
            self.assertFalse(can_use_trash())


class TrashPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_trash_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_invokes_gio_trash_with_path(self) -> None:
        target = self.tmpdir / "doomed.txt"
        target.write_bytes(b"x")
        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(trash_mod.shutil, "which", return_value="/usr/bin/gio"), \
             mock.patch.object(trash_mod.subprocess, "run", return_value=completed) as run:
            ok = trash_path(target)

        self.assertTrue(ok)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["gio", "trash", "--", str(target)])
        self.assertFalse(kwargs.get("check", True))

    def test_returns_false_on_non_zero_exit(self) -> None:
        target = self.tmpdir / "stuck.txt"
        target.write_bytes(b"x")
        completed = mock.Mock(returncode=1, stderr="permission denied")
        with mock.patch.object(trash_mod.shutil, "which", return_value="/usr/bin/gio"), \
             mock.patch.object(trash_mod.subprocess, "run", return_value=completed):
            ok = trash_path(target)
        self.assertFalse(ok)

    def test_returns_true_when_path_already_gone(self) -> None:
        absent = self.tmpdir / "never-existed.txt"
        with mock.patch.object(trash_mod.subprocess, "run") as run:
            self.assertTrue(trash_path(absent))
        run.assert_not_called()

    def test_fallback_unlinks_when_gio_missing(self) -> None:
        target = self.tmpdir / "deleteme.txt"
        target.write_bytes(b"x")
        with mock.patch.object(trash_mod.shutil, "which", return_value=None):
            ok = trash_path(target)
        self.assertTrue(ok)
        self.assertFalse(target.exists())

    def test_fallback_returns_false_when_unlink_fails(self) -> None:
        target = self.tmpdir / "ghost.txt"  # doesn't exist; force-unlink raises
        # Cover the path where gio missing AND the file exists but unlink fails.
        target.write_bytes(b"x")

        def _boom(self_inner) -> None:
            raise OSError("permission denied")

        with mock.patch.object(trash_mod.shutil, "which", return_value=None), \
             mock.patch.object(Path, "unlink", _boom):
            ok = trash_path(target)
        self.assertFalse(ok)

    def test_subprocess_oserror_returns_false(self) -> None:
        target = self.tmpdir / "x.txt"
        target.write_bytes(b"x")
        with mock.patch.object(trash_mod.shutil, "which", return_value="/usr/bin/gio"), \
             mock.patch.object(
                 trash_mod.subprocess, "run",
                 side_effect=OSError("exec format error"),
             ):
            ok = trash_path(target)
        self.assertFalse(ok)


class AllowUnlinkFallbackTests(unittest.TestCase):
    """F-D18 — let callers decline the irreversible-delete fallback.

    The default behavior (``allow_unlink_fallback=True``) preserves
    backwards-compatibility with the existing T11.4 sync path. The new
    knob lets a user-initiated deletion path say "don't silently turn
    a missing-gio environment into a permanent rm" — the sync engine
    can leave the local file untouched and surface a warning instead.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_trash_F_D18_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_default_keeps_unlink_fallback(self) -> None:
        target = self.tmpdir / "default.txt"
        target.write_bytes(b"x")
        with mock.patch.object(trash_mod.shutil, "which", return_value=None):
            ok = trash_path(target)
        self.assertTrue(ok)
        self.assertFalse(target.exists())

    def test_explicit_decline_leaves_file_in_place(self) -> None:
        target = self.tmpdir / "preserved.txt"
        target.write_bytes(b"important data")
        with mock.patch.object(trash_mod.shutil, "which", return_value=None):
            with self.assertLogs("src.vault_trash", level="WARNING") as cm:
                ok = trash_path(target, allow_unlink_fallback=False)
        self.assertFalse(ok)
        self.assertTrue(target.exists(), "file should still be on disk")
        self.assertEqual(target.read_bytes(), b"important data")
        self.assertTrue(
            any(
                "trash_unlink_fallback_declined" in line
                for line in cm.output
            ),
            cm.output,
        )

    def test_decline_does_not_affect_gio_path(self) -> None:
        # When gio is available the kwarg is irrelevant — gio handles
        # the operation reversibly.
        target = self.tmpdir / "via_gio.txt"
        target.write_bytes(b"x")
        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(trash_mod.shutil, "which", return_value="/usr/bin/gio"), \
             mock.patch.object(trash_mod.subprocess, "run", return_value=completed):
            ok = trash_path(target, allow_unlink_fallback=False)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
