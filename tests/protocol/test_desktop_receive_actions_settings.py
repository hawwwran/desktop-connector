"""Source-level checks for the Receive Actions settings UI wiring.

The GTK window runs in a subprocess and needs a real display session for
full interaction testing. These checks pin the structural changes that
P.4 owns without constructing widgets in the test runner.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


WINDOWS_PY = Path(REPO_ROOT) / "desktop" / "src" / "windows.py"


class ReceiveActionsSettingsSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WINDOWS_PY.read_text()

    def test_old_auto_open_links_row_is_removed(self):
        self.assertNotIn("Auto-open links", self.source)

    def test_receive_actions_group_and_rows_are_present(self):
        for text in (
            'Adw.PreferencesGroup(title="Receive Actions")',
            "RECEIVE_KIND_URL",
            "RECEIVE_KIND_TEXT",
            "RECEIVE_KIND_IMAGE",
            "RECEIVE_KIND_VIDEO",
            "RECEIVE_KIND_DOCUMENT",
            "Open in default browser",
            "Copy to clipboard",
            "Open in default image viewer",
            "Open in default video viewer",
            "Open in default document viewer",
        ):
            self.assertIn(text, self.source)

    def test_logs_are_appended_after_connection_statistics(self):
        stats_pos = self.source.index('title="Pending outgoing"')
        logs_call_pos = self.source.index("\n        add_logs_group()")

        self.assertGreater(logs_call_pos, stats_pos)


if __name__ == "__main__":
    unittest.main()
