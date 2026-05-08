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


WINDOWS_PKG = Path(REPO_ROOT) / "desktop" / "src" / "windows_settings"


def _read_settings_package_source() -> str:
    """Concatenate every ``windows_settings/*.py`` in render order so
    source-pin assertions keep working across the per-group split
    (see plan #10).

    Order matches the dispatch sequence in ``window.py`` (Connection →
    Appearance → Vault → Receive Actions / Flood Protection → This
    Device + Connected Devices + Statistics → Security → Logs), so
    tests asserting the *relative* ordering of recognizable substrings
    (e.g. "Receive Actions" before "Receive Action Flood Protection"
    before "Logs") still hold.
    """
    ordered_modules = (
        "__init__.py",
        "context.py",
        "window.py",
        "group_relay.py",
        "group_theme.py",
        "group_vault.py",
        "group_receive_actions.py",
        "group_pairings.py",
        "group_secret_storage.py",
        "group_logs.py",
    )
    parts: list[str] = []
    for name in ordered_modules:
        parts.append((WINDOWS_PKG / name).read_text())
    return "\n".join(parts)


class ReceiveActionsSettingsSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _read_settings_package_source()

    def test_old_auto_open_links_row_is_removed(self):
        self.assertNotIn("Auto-open links", self.source)

    def test_settings_window_default_size_and_resizable(self):
        self.assertIn(
            'title="Settings", default_width=630, default_height=624',
            self.source,
        )
        self.assertIn("win.set_resizable(True)", self.source)
        self.assertNotIn("win.set_resizable(False)", self.source)

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

    def test_flood_protection_group_and_rows_are_present(self):
        for text in (
            'Adw.PreferencesGroup(title="Receive Action Flood Protection")',
            'title="Flood limits"',
            'subtitle="0 means unlimited"',
            'Gtk.Button(label="Reset to defaults"',
            "DEFAULT_RECEIVE_ACTION_LIMITS",
            "RECEIVE_ACTION_LIMIT_MAX",
            "RECEIVE_ACTION_LIMIT_BATCH",
            "RECEIVE_ACTION_LIMIT_MINUTE",
            "RECEIVE_ACTION_KEY_URL_OPEN",
            "RECEIVE_ACTION_KEY_URL_COPY",
            "RECEIVE_ACTION_KEY_TEXT_COPY",
            "RECEIVE_ACTION_KEY_IMAGE_OPEN",
            "RECEIVE_ACTION_KEY_VIDEO_OPEN",
            "RECEIVE_ACTION_KEY_DOCUMENT_OPEN",
            '"Open URL"',
            '"Copy URL to clipboard"',
            '"Copy text to clipboard"',
            '"Open image"',
            '"Open video"',
            '"Open document"',
            "Gtk.Grid(",
            '"Action type"',
            '"Max per batch"',
            '"Max per minute"',
            "Gtk.SpinButton(",
            "config.set_receive_action_limit(",
            "config.reset_receive_action_limits()",
        ):
            self.assertIn(text, self.source)

    def test_flood_protection_is_after_receive_actions_and_before_logs(self):
        receive_pos = self.source.index('title="Receive Actions"')
        flood_pos = self.source.index('title="Receive Action Flood Protection"')
        logs_pos = self.source.index('title="Logs"')

        self.assertGreater(flood_pos, receive_pos)
        self.assertLess(flood_pos, logs_pos)

    def test_logs_are_appended_after_connection_statistics(self):
        # Plan #10 split: ``add_logs_group()`` became
        # ``group_logs.build(ctx)`` invoked from ``window.py`` after
        # ``group_secret_storage.build(ctx)``. In render-order
        # concatenation the Logs group module ("Logs" PreferencesGroup
        # title) still appears after the pairings module's "Pending
        # outgoing" stats row, preserving the original UI order.
        stats_pos = self.source.index('title="Pending outgoing"')
        logs_pos = self.source.index('Adw.PreferencesGroup(title="Logs")')

        self.assertGreater(logs_pos, stats_pos)


if __name__ == "__main__":
    unittest.main()
