"""Source checks for the multi-device Settings GTK window (M.5)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _settings_window_source() -> str:
    source = Path(REPO_ROOT, "desktop/src/windows.py").read_text()
    start = source.index("def show_settings(")
    end = source.index("# ─── History Window", start)
    return source[start:end]


class SettingsMultiDeviceSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _settings_window_source()

    def test_pair_section_lists_all_paired_devices(self):
        for text in (
            "Adw.PreferencesGroup(title=\"Connected Devices\")",
            "settings_registry = ConnectedDeviceRegistry(config)",
            "paired_devices = settings_registry.list_devices()",
            "for device in paired_devices:",
            "Adw.ActionRow(title=target_name",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        # Old single-pair section must be gone.
        self.assertNotIn(
            "Adw.PreferencesGroup(title=\"Paired Device\")", self.source
        )
        self.assertNotIn("config.get_first_paired_device()", self.source)

    def test_per_device_rename_action_uses_registry(self):
        for text in (
            "def open_rename_dialog(target_id: str, current_name: str):",
            "settings_registry.rename(target_id, name_entry.get_text())",
            "DuplicateDeviceNameError",
            "Gtk.Button(\n                    label=\"Rename\"",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_per_device_unpair_only_removes_one_pairing(self):
        for text in (
            "def open_unpair_dialog(target_id: str, target_name: str, target_info: dict):",
            "filename_override=\".fn.unpair\"",
            "settings_registry.unpair(target_id)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        # Per-device unpair must scope to the target id; the legacy
        # full-window-close-after-single-pair path is fine, but the
        # body must not call config.remove_paired_device(device_id) on
        # an outer-scope id.
        self.assertNotIn("config.remove_paired_device(device_id)", self.source)

    def test_active_device_marker_is_rendered(self):
        for text in (
            "settings_active_device = settings_registry.get_active_device()",
            "active_device_id = (",
            "if target_id == active_device_id:",
            "·  Active",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_pair_another_device_entry_point_present(self):
        # M.10 follow-up: a Settings entry point that spawns the pairing
        # window so users can add a second pair without a terminal. The
        # tray's Pair... item also stays visible after the first pair —
        # but Settings is the parity surface with Android's PairingsCard.
        for text in (
            'title="Pair another device"',
            'def on_add_pair(_row):',
            "--gtk-window=pairing",
            'add_pair_row.connect("activated", on_add_pair)',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")


if __name__ == "__main__":
    unittest.main()
