"""Source- and behavior-level checks for the folder-send rejection.

Folders are not a transport-supported entity in the desktop-connector
protocol (it's per-file). When a user passes a folder via Nautilus,
the GTK4 send window, or the --send CLI flag, the desktop must
surface a "Folder transport is not supported" notification rather
than silently swallow the request.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()
sys.path.insert(0, REPO_ROOT)

from desktop.src.runners import send_runner as send_runner_mod  # noqa: E402


class NautilusScriptSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(REPO_ROOT, "desktop/nautilus-send-to-phone.py").read_text()

    def test_paths_are_partitioned_into_files_and_folders(self):
        for text in (
            "files = [p for p in paths if os.path.isfile(p)]",
            "folders = [p for p in paths if os.path.isdir(p)]",
            "for path in files:",
        ):
            self.assertIn(text, self.source)

    def test_folder_warning_is_separate_notification(self):
        self.assertIn("Folder transport is not supported", self.source)
        self.assertIn("Send individual files instead.", self.source)
        self.assertIn("dialog-warning", self.source)

    def test_queued_count_uses_files_only_not_total_paths(self):
        self.assertIn("f\"{len(files)} file(s) queued\"", self.source)
        self.assertNotIn("count = len(paths)", self.source)


class AppImageInstallHookTemplateTests(unittest.TestCase):
    def setUp(self):
        from desktop.src.bootstrap.appimage_install_hook import (
            _nautilus_nemo_script_text,
        )
        self.rendered = _nautilus_nemo_script_text(Path("/x.AppImage"))

    def test_rendered_script_parses_as_python(self):
        import ast
        ast.parse(self.rendered)

    def test_rendered_script_partitions_files_and_folders(self):
        for text in (
            "files = [p for p in paths if os.path.isfile(p)]",
            "folders = [p for p in paths if os.path.isdir(p)]",
            "for path in files:",
        ):
            self.assertIn(text, self.rendered)

    def test_rendered_script_warns_on_folders(self):
        self.assertIn("Folder transport is not supported", self.rendered)
        self.assertIn("dialog-warning", self.rendered)

    def test_rendered_script_queued_count_uses_files_only(self):
        self.assertIn('f"{len(files)} file(s) queued"', self.rendered)
        self.assertNotIn("count = len(paths)", self.rendered)


class SendFilesWindowSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(REPO_ROOT, "desktop/src/windows.py").read_text()

    def test_drop_handler_counts_skipped_folders(self):
        for text in (
            "skipped_folders = 0",
            "if p.is_dir():",
            "skipped_folders += 1",
            "if skipped_folders:",
            "_notify_folders_skipped(skipped_folders)",
        ):
            self.assertIn(text, self.source)

    def test_browse_handler_filters_folders_too(self):
        # The on_browse handler must guard against pickers that allow
        # directory selection (tkinter on some file managers).
        # Two distinct call sites — drop + browse — both call the helper.
        self.assertEqual(
            self.source.count("_notify_folders_skipped(skipped_folders)"),
            2,
        )

    def test_helper_uses_dialog_warning_icon_and_canonical_text(self):
        self.assertIn("def _notify_folders_skipped(count: int) -> None:", self.source)
        self.assertIn('"Folder transport is not supported"', self.source)
        self.assertIn('icon="dialog-warning"', self.source)


class SendRunnerDirectoryRejectionTests(unittest.TestCase):
    """End-to-end: passing a directory to --send must be rejected with
    a folder-warning notification and exit code 1, before any
    server-touching code runs."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-folder-reject-"))
        self.folder = self.tmp / "some-folder"
        self.folder.mkdir()
        (self.folder / "inner.txt").write_text("payload")

        self.config = MagicMock()
        self.config.is_registered = True
        self.config.is_paired = True
        self.config.server_url = "http://test.invalid"
        self.config.device_id = "dev-sender"
        self.config.auth_token = "tok"
        self.config.config_dir = self.tmp
        self.config.get_first_paired_device.return_value = (
            "peer-id",
            {"symmetric_key_b64": base64.b64encode(b"k" * 32).decode()},
        )
        self.crypto = MagicMock()

    def test_directory_send_is_rejected_with_notification(self):
        with patch("desktop.src.notifications.notify") as notify_mock, \
             patch.object(send_runner_mod, "ApiClient") as api_cls, \
             patch.object(send_runner_mod, "ConnectionManager") as conn_cls:
            rc = send_runner_mod.run_send_file(
                self.config, self.crypto, self.folder
            )

        self.assertEqual(rc, 1)
        notify_mock.assert_called_once()
        args, kwargs = notify_mock.call_args
        self.assertEqual(args[0], "Folder transport is not supported")
        self.assertIn("individual", args[1].lower())
        # No connection manager / API client must be built — rejection
        # happens before any server interaction.
        api_cls.assert_not_called()
        conn_cls.assert_not_called()

    def test_missing_path_still_rejected_before_dir_check(self):
        # Defensive: a path that doesn't exist hits the existing
        # not-found branch, not the folder-warning branch.
        ghost = self.tmp / "ghost"
        with patch("desktop.src.notifications.notify") as notify_mock:
            rc = send_runner_mod.run_send_file(
                self.config, self.crypto, ghost
            )

        self.assertEqual(rc, 1)
        notify_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
