"""T8.6 — Vault export-reminder cadence + dismissal helpers."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_export_reminder import (  # noqa: E402
    CADENCE_DAYS,
    DEFAULT_CADENCE,
    next_reminder_due,
    normalize_cadence,
    should_show_export_reminder,
)


NOW = "2026-05-04T12:00:00.000Z"


class CadenceConfigTests(unittest.TestCase):
    def test_cadence_table_covers_the_five_modes(self) -> None:
        self.assertEqual(CADENCE_DAYS["off"], None)
        self.assertEqual(CADENCE_DAYS["weekly"], 7)
        self.assertEqual(CADENCE_DAYS["monthly"], 30)
        self.assertEqual(CADENCE_DAYS["quarterly"], 90)
        self.assertEqual(CADENCE_DAYS["yearly"], 365)

    def test_default_is_monthly(self) -> None:
        self.assertEqual(DEFAULT_CADENCE, "monthly")
        self.assertEqual(normalize_cadence(None), "monthly")
        self.assertEqual(normalize_cadence(""), "monthly")
        self.assertEqual(normalize_cadence("garbage"), "monthly")

    def test_normalize_is_case_insensitive(self) -> None:
        self.assertEqual(normalize_cadence("WEEKLY"), "weekly")
        self.assertEqual(normalize_cadence(" Monthly "), "monthly")


class ShouldShowReminderTests(unittest.TestCase):
    def test_off_cadence_never_fires(self) -> None:
        self.assertFalse(should_show_export_reminder(
            last_export_at=None, last_dismissed_at=None,
            cadence="off", now=NOW,
        ))

    def test_no_export_ever_yields_reminder(self) -> None:
        self.assertTrue(should_show_export_reminder(
            last_export_at=None, last_dismissed_at=None,
            cadence="monthly", now=NOW,
        ))

    def test_export_within_cadence_suppresses_reminder(self) -> None:
        # Exported 5 days ago → monthly cadence (30 days) hasn't elapsed.
        self.assertFalse(should_show_export_reminder(
            last_export_at="2026-04-29T12:00:00.000Z",
            last_dismissed_at=None,
            cadence="monthly", now=NOW,
        ))

    def test_export_beyond_cadence_fires(self) -> None:
        """T8.6 acceptance: advancing last_export_at 31 days back triggers."""
        self.assertTrue(should_show_export_reminder(
            last_export_at="2026-04-03T12:00:00.000Z",  # 31 days ago
            last_dismissed_at=None,
            cadence="monthly", now=NOW,
        ))

    def test_recent_dismissal_suppresses_until_next_cadence(self) -> None:
        """T8.6 acceptance: clicking dismiss hides for that occurrence."""
        # Exported 60 days ago, but dismissed 5 days ago → suppressed.
        self.assertFalse(should_show_export_reminder(
            last_export_at="2026-03-05T12:00:00.000Z",
            last_dismissed_at="2026-04-29T12:00:00.000Z",
            cadence="monthly", now=NOW,
        ))

    def test_dismissal_eventually_expires_and_reminder_returns(self) -> None:
        # Dismissed 31 days ago with no fresh export → reminder fires again.
        self.assertTrue(should_show_export_reminder(
            last_export_at="2026-01-01T12:00:00.000Z",
            last_dismissed_at="2026-04-03T12:00:00.000Z",
            cadence="monthly", now=NOW,
        ))

    def test_weekly_cadence_uses_7_days(self) -> None:
        # 6 days back should suppress; 8 days back should fire.
        self.assertFalse(should_show_export_reminder(
            last_export_at="2026-04-28T12:00:00.000Z",
            last_dismissed_at=None,
            cadence="weekly", now=NOW,
        ))
        self.assertTrue(should_show_export_reminder(
            last_export_at="2026-04-26T12:00:00.000Z",
            last_dismissed_at=None,
            cadence="weekly", now=NOW,
        ))


class NextReminderDueTests(unittest.TestCase):
    def test_off_returns_none(self) -> None:
        self.assertIsNone(next_reminder_due(
            last_export_at=NOW, last_dismissed_at=None, cadence="off",
        ))

    def test_due_is_max_of_export_plus_cadence_and_dismiss_plus_cadence(self) -> None:
        out = next_reminder_due(
            last_export_at="2026-04-03T12:00:00.000Z",       # +30 = May 3
            last_dismissed_at="2026-04-15T12:00:00.000Z",    # +30 = May 15 (later)
            cadence="monthly",
        )
        self.assertEqual(out, "2026-05-15T12:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
