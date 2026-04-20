"""Auth-failure state machine on the desktop ConnectionManager.

Drives _record_auth_response directly (no network) to pin the contract that
powers the re-pair banner and the honest offline/online rendering:

  - 3 consecutive 401/403 trip the latch; subsequent failures don't re-fire.
  - Any 2xx resets the streak counter but leaves a latched kind alone.
  - `effective_state` flips to DISCONNECTED on count 0 → 1 already,
    not only on the trip.
  - Observers registered via `on_state_change` see the effective sequence,
    not the raw state.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.connection import (  # noqa: E402
    AUTH_FAILURE_THRESHOLD,
    AuthFailureKind,
    ConnectionManager,
    ConnectionState,
)


class ConnectionAuthStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = ConnectionManager("http://example.invalid", "did", "tok")
        self.state_events: list[ConnectionState] = []
        self.auth_events: list[AuthFailureKind] = []
        self.cm.on_state_change(self.state_events.append)
        self.cm.on_auth_failure(self.auth_events.append)
        # Start from CONNECTED so state-change transitions are observable.
        self.cm._set_state(ConnectionState.CONNECTED)
        self.state_events.clear()

    def test_trip_at_threshold(self) -> None:
        for _ in range(AUTH_FAILURE_THRESHOLD - 1):
            self.cm._record_auth_response(401)
        self.assertIsNone(self.cm.auth_failure_kind)
        self.assertEqual(self.auth_events, [])
        self.cm._record_auth_response(401)
        self.assertEqual(self.cm.auth_failure_kind, AuthFailureKind.CREDENTIALS_INVALID)
        self.assertEqual(self.auth_events, [AuthFailureKind.CREDENTIALS_INVALID])

    def test_latched_flag_does_not_refire(self) -> None:
        for _ in range(AUTH_FAILURE_THRESHOLD):
            self.cm._record_auth_response(401)
        self.assertEqual(len(self.auth_events), 1)
        for _ in range(5):
            self.cm._record_auth_response(401)
        self.assertEqual(len(self.auth_events), 1)

    def test_success_resets_streak_but_not_latch(self) -> None:
        # Partial streak cleared by a 2xx.
        self.cm._record_auth_response(401)
        self.cm._record_auth_response(401)
        self.cm._record_auth_response(200)
        self.cm._record_auth_response(401)
        self.assertIsNone(self.cm.auth_failure_kind)
        # Latched flag survives a 2xx (only user-driven clear removes it).
        self.cm._record_auth_response(401)
        self.cm._record_auth_response(401)
        self.cm._record_auth_response(401)  # trip
        self.assertEqual(self.cm.auth_failure_kind, AuthFailureKind.CREDENTIALS_INVALID)
        self.cm._record_auth_response(200)
        self.assertEqual(self.cm.auth_failure_kind, AuthFailureKind.CREDENTIALS_INVALID)
        self.cm.clear_auth_failure()
        self.assertIsNone(self.cm.auth_failure_kind)

    def test_different_kinds_reset_streak(self) -> None:
        # A 403 after two 401s restarts the counter at 1 with the new kind.
        self.cm._record_auth_response(401)
        self.cm._record_auth_response(401)
        self.cm._record_auth_response(403)
        self.assertIsNone(self.cm.auth_failure_kind)
        self.cm._record_auth_response(403)
        self.cm._record_auth_response(403)  # trip on PAIRING_MISSING
        self.assertEqual(self.cm.auth_failure_kind, AuthFailureKind.PAIRING_MISSING)

    def test_effective_state_flips_on_first_failure(self) -> None:
        self.assertEqual(self.cm.effective_state, ConnectionState.CONNECTED)
        self.cm._record_auth_response(401)
        self.assertEqual(self.cm.effective_state, ConnectionState.DISCONNECTED)
        # One state-change event: CONNECTED → DISCONNECTED
        self.assertEqual(self.state_events, [ConnectionState.DISCONNECTED])

    def test_effective_state_restores_on_success(self) -> None:
        self.cm._record_auth_response(401)
        self.cm._record_auth_response(200)
        self.assertEqual(self.cm.effective_state, ConnectionState.CONNECTED)
        self.assertEqual(
            self.state_events,
            [ConnectionState.DISCONNECTED, ConnectionState.CONNECTED],
        )

    def test_clear_auth_failure_restores_effective_state(self) -> None:
        for _ in range(AUTH_FAILURE_THRESHOLD):
            self.cm._record_auth_response(401)
        self.state_events.clear()
        self.cm.clear_auth_failure()
        self.assertEqual(self.cm.effective_state, ConnectionState.CONNECTED)
        self.assertEqual(self.state_events, [ConnectionState.CONNECTED])

    def test_status_text_per_kind(self) -> None:
        for _ in range(AUTH_FAILURE_THRESHOLD):
            self.cm._record_auth_response(401)
        self.assertIn("doesn't recognise", self.cm.get_status_text())
        self.cm.clear_auth_failure()
        for _ in range(AUTH_FAILURE_THRESHOLD):
            self.cm._record_auth_response(403)
        self.assertIn("pairing lost", self.cm.get_status_text())

    def test_update_credentials_is_atomic(self) -> None:
        self.cm.update_credentials("new-did", "new-tok")
        headers = self.cm.auth_headers()
        self.assertEqual(headers["X-Device-ID"], "new-did")
        self.assertEqual(headers["Authorization"], "Bearer new-tok")

    def test_advisory_request_does_not_flip_state_on_timeout(self) -> None:
        """`track_state=False` on an advisory request (e.g. the delivery
        tracker's 750ms poll) must not flip the global state on a
        timeout. Regression coverage for the CONNECTED⇄DISCONNECTED
        thrash that spammed backoff-retry logs once any zombie
        'undelivered failed' transfer was in history."""
        # Pretend the transport raised (mock `requests.request` to raise).
        from unittest.mock import patch
        import requests

        initial_state = self.cm.state
        with patch("src.connection.requests.request",
                   side_effect=requests.RequestException("timeout")):
            resp = self.cm.request("GET", "/anything", track_state=False)
        self.assertIsNone(resp)
        self.assertEqual(self.cm.state, initial_state,
                         "track_state=False must not transition state")
        self.assertEqual(self.cm.retry_count, 0,
                         "track_state=False must not advance backoff")

    def test_4xx_does_not_trigger_backoff(self) -> None:
        """A 429 (rate limit), 409 (storage limit), etc. means the server
        is perfectly healthy — it just rejected this specific call. Must
        NOT flip state to DISCONNECTED or advance backoff. Regression
        coverage for the startup "Connection lost" notification where a
        just-connected ping fired inside the server's 30s cooldown and
        the 429 response dragged the whole connection state to
        DISCONNECTED for ~25 s."""
        from unittest.mock import patch, Mock

        self.cm._on_success()  # prime to CONNECTED
        self.assertEqual(self.cm.state, ConnectionState.CONNECTED)

        for code in (400, 404, 409, 413, 422, 429):
            fake = Mock(status_code=code, text="")
            with patch("src.connection.requests.request", return_value=fake):
                self.cm.request("POST", "/anything")
            self.assertEqual(
                self.cm.state, ConnectionState.CONNECTED,
                f"status {code} should not have flipped state",
            )
            self.assertEqual(
                self.cm.retry_count, 0,
                f"status {code} should not have advanced backoff",
            )

    def test_5xx_still_triggers_backoff(self) -> None:
        """5xx means the server is in trouble — keep the existing
        back-off semantics so we don't hammer it."""
        from unittest.mock import patch, Mock

        self.cm._on_success()  # prime to CONNECTED
        fake = Mock(status_code=503, text="")
        with patch("src.connection.requests.request", return_value=fake):
            self.cm.request("GET", "/anything")
        self.assertEqual(self.cm.state, ConnectionState.DISCONNECTED)
        self.assertEqual(self.cm.retry_count, 1)

    def test_advisory_request_still_feeds_auth_counter(self) -> None:
        """A 401 on an advisory call still counts — the banner eventually
        fires even if only the tracker is exercising the creds."""
        from unittest.mock import patch, Mock

        fake_resp = Mock(status_code=401, text="")
        with patch("src.connection.requests.request", return_value=fake_resp):
            for _ in range(AUTH_FAILURE_THRESHOLD):
                self.cm.request("GET", "/anything", track_state=False)
        self.assertEqual(self.cm.auth_failure_kind,
                         AuthFailureKind.CREDENTIALS_INVALID)


if __name__ == "__main__":
    unittest.main()
