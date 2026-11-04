"""Source checks for the multi-device history GTK window."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _history_window_source() -> str:
    package_dir = Path(REPO_ROOT, "desktop/src/windows_history")
    return "\n".join(
        p.read_text() for p in sorted(package_dir.glob("*.py"))
    )


class HistoryMultiDeviceSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _history_window_source()

    def test_history_window_has_device_picker(self):
        for text in (
            "device_picker, selected_device, paired_devices = _create_device_picker(",
            "title=\"History for\"",
            "subtitle=\"Connected device\"",
            "device_group.add(device_picker)",
            "device_picker.connect(",
            "\"notify::selected\"",
            "_on_history_device_changed(ctx",
        ):
            self.assertIn(text, self.source)

    def test_history_rows_are_filtered_to_selected_device(self):
        for text in (
            "selected_id = _selected_device_id(ctx)",
            "history.items_for_peer(",
            "fallback_device_id=selected_id",
            "s_sig = (",
            "p_sig = (",
        ):
            self.assertIn(text, self.source)

    def test_empty_state_names_selected_device(self):
        for text in (
            "def _empty_history_text(ctx: HistoryContext) -> str:",
            "return \"No connected devices\"",
            "return f\"No transfers with {_selected_device_name(ctx)}\"",
            "Gtk.Label(label=_empty_history_text(ctx))",
        ):
            self.assertIn(text, self.source)

    def test_clear_history_is_selected_device_scoped(self):
        for text in (
            "clear_all_btn.set_tooltip_text(\"Clear visible history\")",
            "heading=f\"Clear history for {device_name}?\"",
            "history.clear_for_peer(",
            "fallback_device_id=device.device_id",
            "ctx.reset_history_view()",
        ):
            self.assertIn(text, self.source)
        self.assertNotIn("history.clear()", self.source)


if __name__ == "__main__":
    unittest.main()
