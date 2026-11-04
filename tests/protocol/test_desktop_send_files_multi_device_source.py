"""Source checks for the multi-device send-files GTK window."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _send_files_source() -> str:
    return Path(REPO_ROOT, "desktop/src/windows_send.py").read_text()


class SendFilesMultiDeviceSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # _create_device_picker now lives in windows_common.py \u2014 concat
        # so substring checks for it still match.
        cls.full_source = (
            Path(REPO_ROOT, "desktop/src/windows_send.py").read_text()
            + Path(REPO_ROOT, "desktop/src/windows_common.py").read_text()
        )
        cls.source = _send_files_source()

    def test_send_files_window_uses_new_name(self):
        self.assertIn('title="Send files to"', self.source)
        self.assertIn('Gtk.Button(label="Send files")', self.source)
        self.assertNotIn("Send to Phone", self.source)

    def test_send_files_window_has_device_picker(self):
        for text in (
            "def _create_device_picker(config, *, title: str, subtitle: str = \"\"):",
            "ConnectedDeviceRegistry(config)",
            "Adw.ComboRow(",
            "Gtk.StringList.new(device_labels or [\"No paired devices\"])",
            "device_picker, selected_device, paired_devices = _create_device_picker(",
            "title=\"Send files to\"",
            "subtitle=\"Connected device\"",
        ):
            self.assertIn(text, self.full_source)

    def test_device_picker_preselects_active_device(self):
        for text in (
            "active_device = registry.get_active_device()",
            "active_id = active_device.device_id",
            "if device.device_id == active_id:",
            "selected_device[0] = device",
            "row.set_selected(selected_index)",
        ):
            self.assertIn(text, self.full_source)

    def test_send_button_requires_selected_device(self):
        for text in (
            "send_btn.set_sensitive(selected_device[0] is not None)",
            "target = selected_device[0]",
            "if target is None:",
            "device_picker.set_sensitive(False)",
            "device_picker.set_sensitive(bool(paired_devices))",
        ):
            self.assertIn(text, self.source)

    def test_batch_uses_selected_device_not_first_pair(self):
        for text in (
            "target_id = target.device_id",
            "base64.b64decode(target.symmetric_key_b64)",
            "peer_device_id=target_id",
        ):
            self.assertIn(text, self.source)
        self.assertNotIn("config.get_first_paired_device()", self.source)


if __name__ == "__main__":
    unittest.main()
