"""Delivery tracker: poll backoff, stall handling, absent transfers.

Drives ``DeliveryTrackerMixin`` against a real ``TransferHistory`` in a
tempdir and a fake API, with a controllable monotonic clock. No network
and no threads: the tests call the tick and poll steps directly.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.history import TransferHistory, TransferStatus  # noqa: E402
from src.receive import delivery_tracker  # noqa: E402
from src.receive.delivery_tracker import (  # noqa: E402
    DELIVERY_STALL_TIMEOUT,
    TRACKER_FAST_WINDOW,
    TRACKER_MAX_INTERVAL,
    TRACKER_MIN_INTERVAL,
    DeliveryTrackerMixin,
)


class _FakeApi:
    def __init__(self) -> None:
        self.sent_status: list[dict] | None = []
        self.fetch_count = 0

    def fetch_sent_status(self, timeout: float = 30, *, track_state: bool = True):
        self.fetch_count += 1
        return self.sent_status

    def get_sent_status(self, timeout: float = 30, *, track_state: bool = True):
        return self.sent_status or []


class _Tracker(DeliveryTrackerMixin):
    """The slice of Poller state the tracker mixin reads and writes."""

    def __init__(self, history: TransferHistory, api: _FakeApi) -> None:
        self.history = history
        self.api = api
        self._last_delivery_check = 0.0
        self._tracker_state_lock = threading.Lock()
        self._tracker_last_progress: dict[str, tuple[int, float]] = {}
        self._tracker_gave_up: set[str] = set()
        self._tracker_absent: set[str] = set()
        self._tracker_interval = TRACKER_MIN_INTERVAL
        self._tracker_next_poll_at = 0.0
        self._tracker_last_news_at = 0.0
        self._tracker_upload_state: dict[str, tuple] = {}


def _status(tid: str, chunks_downloaded: int = 0, *, state: str = "not_started",
            chunk_count: int = 4) -> dict:
    return {
        "transfer_id": tid,
        "delivery_state": state,
        "chunks_downloaded": chunks_downloaded,
        "chunk_count": chunk_count,
    }


class DeliveryTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.history = TransferHistory(Path(self._tmp.name))
        self.api = _FakeApi()
        self.tracker = _Tracker(self.history, self.api)
        self.now = 1000.0
        patcher = mock.patch.object(
            delivery_tracker.time, "monotonic", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, tid: str, status: str = TransferStatus.COMPLETE) -> None:
        self.history.add(
            filename=tid, display_label=tid, direction="sent",
            size=1, transfer_id=tid, status=status,
        )

    def _poll_if_due(self) -> bool:
        due = self.tracker._tracker_poll_due()
        if due:
            self.tracker._tracker_poll_once()
        return due

    def _run_for(self, seconds: float) -> int:
        """Advance the clock in tick-sized steps; return polls fired."""
        before = self.api.fetch_count
        end = self.now + seconds
        while self.now < end:
            self._poll_if_due()
            self.now += TRACKER_MIN_INTERVAL
        return self.api.fetch_count - before

    def test_idle_without_undelivered_transfers(self) -> None:
        self.assertFalse(self.tracker._tracker_poll_due())

    def test_poll_stays_fast_for_the_first_window_without_news(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a")]
        polls = self._run_for(TRACKER_FAST_WINDOW - 1)
        self.assertEqual(self.tracker._tracker_interval, TRACKER_MIN_INTERVAL)
        self.assertEqual(polls, (TRACKER_FAST_WINDOW - 1) / TRACKER_MIN_INTERVAL)

    def test_steady_recipient_progress_never_backs_off(self) -> None:
        self._send("tid-a")
        for chunk in range(1, 200):
            self.api.sent_status = [_status("tid-a", chunk, state="in_progress",
                                            chunk_count=500)]
            self._run_for(5)
            self.assertEqual(self.tracker._tracker_interval, TRACKER_MIN_INTERVAL)

    def test_interval_doubles_up_to_the_cap_when_nothing_moves(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a")]
        self._run_for(TRACKER_FAST_WINDOW)
        intervals = []
        for _ in range(10):
            self.now = self.tracker._tracker_next_poll_at
            self.assertTrue(self._poll_if_due())
            intervals.append(self.tracker._tracker_interval)
        self.assertEqual(intervals, [1, 2, 4, 8, 16, 32, 60, 60, 60, 60])
        self.assertEqual(TRACKER_MAX_INTERVAL, 60)

    def test_hour_of_silence_costs_about_one_poll_a_minute(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a")]
        self._run_for(TRACKER_FAST_WINDOW)
        polls = self._run_for(3600)
        self.assertLess(polls, 70)
        self.assertGreater(polls, 55)

    def test_failed_polls_back_off_without_starting_absence_timers(self) -> None:
        self._send("tid-a")
        self.api.sent_status = None  # server gave no usable answer
        self._run_for(DELIVERY_STALL_TIMEOUT * 3)
        self.assertEqual(self.tracker._tracker_interval, TRACKER_MAX_INTERVAL)
        self.assertEqual(self.tracker._tracker_gave_up, set())
        self.assertEqual(self.tracker._tracker_last_progress, {})

    def test_recipient_progress_resets_the_interval(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a", 0)]
        self._run_for(40)
        self.assertGreater(self.tracker._tracker_interval, TRACKER_MIN_INTERVAL)

        self.api.sent_status = [_status("tid-a", 2, state="in_progress")]
        self.now = self.tracker._tracker_next_poll_at
        self._poll_if_due()

        self.assertEqual(self.tracker._tracker_interval, TRACKER_MIN_INTERVAL)
        [row] = self.history.items
        self.assertEqual(row["recipient_chunks_downloaded"], 2)

    def test_local_upload_activity_resets_the_interval(self) -> None:
        self._send("tid-a", TransferStatus.UPLOADING)
        self.api.sent_status = [_status("tid-a")]
        self._run_for(40)
        self.assertGreater(self.tracker._tracker_interval, TRACKER_MIN_INTERVAL)

        self.history.update("tid-a", chunks_downloaded=3)

        self.assertTrue(self.tracker._tracker_poll_due())
        self.assertEqual(self.tracker._tracker_interval, TRACKER_MIN_INTERVAL)

    def test_new_send_is_polled_right_away_despite_backoff(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a")]
        self._run_for(300)
        self.assertFalse(self.tracker._tracker_poll_due())

        self._send("tid-b")

        self.assertTrue(self.tracker._tracker_poll_due())

    def test_unlisted_transfer_is_given_up_and_stops_the_polling(self) -> None:
        # Sent row the server never lists for this device: before the
        # fix it counted as live forever and was polled twice a second.
        self._send("tid-ghost")
        self.api.sent_status = []
        self._run_for(DELIVERY_STALL_TIMEOUT + 2 * TRACKER_MAX_INTERVAL)

        self.assertEqual(self.tracker._tracker_gave_up, {"tid-ghost"})
        self.assertEqual(self.tracker._tracker_absent, {"tid-ghost"})
        self.assertEqual(self._run_for(600), 0)

    def test_listed_stalled_transfer_is_given_up_but_still_polled(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a", 1, state="in_progress")]
        self._run_for(DELIVERY_STALL_TIMEOUT + 2 * TRACKER_MAX_INTERVAL)

        self.assertEqual(self.tracker._tracker_gave_up, {"tid-a"})
        self.assertEqual(self.tracker._tracker_absent, set())
        [row] = self.history.items
        self.assertEqual(row["recipient_chunks_downloaded"], 0)
        self.assertGreater(self._run_for(600), 5)

    def test_given_up_transfer_revives_when_the_recipient_moves(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a", 1, state="in_progress")]
        self._run_for(DELIVERY_STALL_TIMEOUT + 2 * TRACKER_MAX_INTERVAL)
        self.assertEqual(self.tracker._tracker_gave_up, {"tid-a"})

        self.api.sent_status = [_status("tid-a", 3, state="in_progress")]
        self._run_for(2 * TRACKER_MAX_INTERVAL)

        self.assertEqual(self.tracker._tracker_gave_up, set())
        [row] = self.history.items
        self.assertEqual(row["recipient_chunks_downloaded"], 3)

    def test_delivery_of_a_given_up_transfer_is_still_recorded(self) -> None:
        self._send("tid-a")
        self.api.sent_status = [_status("tid-a")]
        self._run_for(DELIVERY_STALL_TIMEOUT + 2 * TRACKER_MAX_INTERVAL)
        self.assertEqual(self.tracker._tracker_gave_up, {"tid-a"})

        self.api.sent_status = [_status("tid-a", 4, state="delivered")]
        self._run_for(2 * TRACKER_MAX_INTERVAL)

        [row] = self.history.items
        self.assertTrue(row["delivered"])


if __name__ == "__main__":
    unittest.main()
