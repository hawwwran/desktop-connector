"""F-LT11 — fresh-unlock stamp + gate exception.

Tests the per-process stamp module that underpins §3.9 / §3.11
fresh-unlock enforcement on destructive vault operations
(clear-folder, clear-vault, schedule-purge, import-merge).
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import fresh_unlock  # noqa: E402
from src.vault.relay_errors import FreshUnlockRequiredError  # noqa: E402


class _FakeClock:
    """Monotonic-shaped fake clock with manual advance."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class FreshUnlockStampTests(unittest.TestCase):
    def setUp(self) -> None:
        fresh_unlock._reset_for_tests()
        self.clock = _FakeClock()
        fresh_unlock._set_clock_for_tests(self.clock)
        self.addCleanup(fresh_unlock._reset_for_tests)

    def test_window_matches_architecture_doc_15_minutes(self) -> None:
        """Review §2.H3: pin the window to the spec
        ``docs/vault-architecture.md`` §13 ("default unlock timeout
        is 15 min idle"). Pre-fix the constant was 120 s and chained
        destructive ops re-prompted twice."""
        self.assertEqual(fresh_unlock.FRESH_UNLOCK_WINDOW_S, 900.0)

    def test_no_stamp_initially(self) -> None:
        self.assertFalse(fresh_unlock.is_fresh_unlock_active())
        self.assertEqual(fresh_unlock.seconds_remaining(), 0.0)

    def test_stamp_activates_window(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.assertTrue(fresh_unlock.is_fresh_unlock_active())
        self.assertEqual(
            fresh_unlock.seconds_remaining(),
            fresh_unlock.FRESH_UNLOCK_WINDOW_S,
        )

    def test_stamp_expires_after_window(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.clock.advance(fresh_unlock.FRESH_UNLOCK_WINDOW_S - 0.1)
        self.assertTrue(fresh_unlock.is_fresh_unlock_active())
        self.clock.advance(0.2)
        self.assertFalse(fresh_unlock.is_fresh_unlock_active())

    def test_stamp_boundary_is_strictly_less_than_window(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.clock.advance(fresh_unlock.FRESH_UNLOCK_WINDOW_S)
        # At exact equality the window is expired — strictly less.
        self.assertFalse(fresh_unlock.is_fresh_unlock_active())

    def test_seconds_remaining_decreases_with_clock(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.assertEqual(
            fresh_unlock.seconds_remaining(),
            fresh_unlock.FRESH_UNLOCK_WINDOW_S,
        )
        self.clock.advance(30.0)
        self.assertAlmostEqual(
            fresh_unlock.seconds_remaining(),
            fresh_unlock.FRESH_UNLOCK_WINDOW_S - 30.0,
            places=3,
        )

    def test_seconds_remaining_zero_after_expiry(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.clock.advance(fresh_unlock.FRESH_UNLOCK_WINDOW_S + 60.0)
        self.assertEqual(fresh_unlock.seconds_remaining(), 0.0)

    def test_restamp_refreshes_window(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.clock.advance(100.0)
        fresh_unlock.stamp_fresh_unlock()
        self.assertEqual(
            fresh_unlock.seconds_remaining(),
            fresh_unlock.FRESH_UNLOCK_WINDOW_S,
        )

    def test_clear_drops_active_stamp(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.assertTrue(fresh_unlock.is_fresh_unlock_active())
        fresh_unlock.clear_fresh_unlock()
        self.assertFalse(fresh_unlock.is_fresh_unlock_active())


class RequireFreshUnlockGateTests(unittest.TestCase):
    def setUp(self) -> None:
        fresh_unlock._reset_for_tests()
        self.clock = _FakeClock()
        fresh_unlock._set_clock_for_tests(self.clock)
        self.addCleanup(fresh_unlock._reset_for_tests)

    def test_no_stamp_raises_typed_error(self) -> None:
        with self.assertRaises(FreshUnlockRequiredError) as ctx:
            fresh_unlock.require_fresh_unlock("clear-folder")
        self.assertEqual(ctx.exception.operation, "clear-folder")

    def test_active_stamp_allows_passage(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        # Does not raise.
        fresh_unlock.require_fresh_unlock("clear-vault")

    def test_expired_stamp_raises(self) -> None:
        fresh_unlock.stamp_fresh_unlock()
        self.clock.advance(fresh_unlock.FRESH_UNLOCK_WINDOW_S + 1.0)
        with self.assertRaises(FreshUnlockRequiredError):
            fresh_unlock.require_fresh_unlock("schedule-purge")

    def test_operation_label_is_optional(self) -> None:
        with self.assertRaises(FreshUnlockRequiredError) as ctx:
            fresh_unlock.require_fresh_unlock()
        self.assertEqual(ctx.exception.operation, "")
        self.assertIn("fresh-unlock required", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
