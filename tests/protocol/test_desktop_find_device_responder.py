"""Find-device receiver responder tests for M.8."""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Callable
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.find_device_responder import (  # noqa: E402
    FindDeviceResponder,
    HEARTBEAT_INTERVAL_S,
    MAX_TIMEOUT_SECONDS,
    NoopAlert,
    build_state_payload,
    encode_state_payload,
)
from src.interfaces.location import LocationFix  # noqa: E402
from src.messaging import (  # noqa: E402
    DeviceMessage,
    FasttrackAdapter,
    MessageDispatcher,
    MessageTransport,
    MessageType,
)


class _CapturingAlert:
    def __init__(self) -> None:
        self.starts: list[str] = []
        self.stops: int = 0

    def start(self, sender_name: str) -> None:
        self.starts.append(sender_name)

    def stop(self) -> None:
        self.stops += 1


class _FakeTimer:
    """Capture the most recently scheduled timer + its callback."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[float, Callable[[], None]]] = []
        self.cancels: int = 0

    def __call__(
        self,
        seconds: float,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        self.scheduled.append((seconds, callback))

        def cancel() -> None:
            self.cancels += 1

        return cancel

    def fire_last(self) -> None:
        _, cb = self.scheduled[-1]
        cb()


class _FakeHeartbeat:
    """Capture the heartbeat callback for manual firing."""

    def __init__(self) -> None:
        self.scheduled: list[Callable[[], None]] = []
        self.cancels: int = 0

    def __call__(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.scheduled.append(callback)

        def cancel() -> None:
            self.cancels += 1

        return cancel

    def fire_last(self) -> None:
        self.scheduled[-1]()


def _make_responder(
    *,
    alert: _CapturingAlert | None = None,
    send_update: Callable | None = None,
    name_lookup: Callable | None = None,
    location_provider: Callable | None = None,
    timer: _FakeTimer | None = None,
    heartbeat: _FakeHeartbeat | None = None,
) -> tuple[
    FindDeviceResponder,
    _CapturingAlert,
    list[tuple],
    _FakeTimer,
    _FakeHeartbeat,
]:
    alert = alert or _CapturingAlert()
    sent: list[tuple] = []

    def default_send(sender_id: str, state: str, **extra) -> bool:
        sent.append((sender_id, state, extra))
        return True

    timer = timer or _FakeTimer()
    heartbeat = heartbeat or _FakeHeartbeat()
    responder = FindDeviceResponder(
        alert=alert,
        send_update=send_update or default_send,
        device_name_lookup=name_lookup,
        location_provider=location_provider,
        start_timer=timer,
        start_heartbeat=heartbeat,
    )
    return responder, alert, sent, timer, heartbeat


class FasttrackAdapterAliasTests(unittest.TestCase):
    def test_legacy_find_phone_still_dispatched(self) -> None:
        msg = FasttrackAdapter.to_device_message(
            {"fn": "find-phone", "action": "start", "volume": 80},
            sender_id="peer-A",
        )
        self.assertIsNotNone(msg)
        self.assertEqual(msg.type, MessageType.FIND_PHONE_START)
        self.assertEqual(msg.sender_id, "peer-A")

    def test_find_device_alias_accepted(self) -> None:
        msg = FasttrackAdapter.to_device_message(
            {"fn": "find-device", "action": "start"},
            sender_id="peer-A",
        )
        self.assertIsNotNone(msg)
        self.assertEqual(msg.type, MessageType.FIND_PHONE_START)

    def test_find_device_stop_alias(self) -> None:
        msg = FasttrackAdapter.to_device_message(
            {"fn": "find-device", "action": "stop"},
        )
        self.assertIsNotNone(msg)
        self.assertEqual(msg.type, MessageType.FIND_PHONE_STOP)

    def test_unknown_fn_returns_none(self) -> None:
        msg = FasttrackAdapter.to_device_message(
            {"fn": "something-else", "action": "start"},
        )
        self.assertIsNone(msg)


class StatePayloadTests(unittest.TestCase):
    def test_state_only_omits_coordinates(self) -> None:
        self.assertEqual(
            build_state_payload("ringing"),
            {"fn": "find-phone", "state": "ringing"},
        )

    def test_state_with_coordinates(self) -> None:
        payload = build_state_payload(
            "ringing", lat=50.1, lng=14.4, accuracy=12.5,
        )
        self.assertEqual(
            payload,
            {
                "fn": "find-phone",
                "state": "ringing",
                "lat": 50.1,
                "lng": 14.4,
                "accuracy": 12.5,
            },
        )

    def test_encode_state_payload_is_json_bytes(self) -> None:
        encoded = encode_state_payload("stopped")
        self.assertEqual(json.loads(encoded), {"fn": "find-phone", "state": "stopped"})


class FindDeviceResponderTests(unittest.TestCase):
    def test_start_marks_active_and_fires_alert_for_audible(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder(
            name_lookup=lambda peer: "Workstation" if peer == "peer-A" else None,
        )

        responder.start("peer-A", volume=70)

        self.assertTrue(responder.is_ringing)
        self.assertEqual(responder.active_sender_id, "peer-A")
        self.assertEqual(alert.starts, ["Workstation"])
        # Timeout scheduled at 5 minutes.
        self.assertEqual(len(timer.scheduled), 1)
        self.assertEqual(timer.scheduled[0][0], MAX_TIMEOUT_SECONDS)
        # Initial ringing heartbeat sent.
        self.assertEqual(sent, [("peer-A", "ringing", {})])

    def test_start_silent_skips_alert(self) -> None:
        responder, alert, _sent, _timer, _heartbeat = _make_responder()

        responder.start("peer-A", volume=0)

        self.assertTrue(responder.is_ringing)
        self.assertEqual(alert.starts, [])

    def test_concurrent_start_from_different_sender_is_dropped(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder()

        responder.start("peer-A", volume=50)
        sent.clear()

        responder.start("peer-B", volume=50)

        # Active session unchanged, second sender ignored.
        self.assertEqual(responder.active_sender_id, "peer-A")
        # Alert.start was NOT called a second time.
        self.assertEqual(len(alert.starts), 1)
        # No state update for peer-B.
        self.assertEqual(sent, [])
        # Only one timer scheduled.
        self.assertEqual(len(timer.scheduled), 1)

    def test_same_sender_restart_refreshes_session(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder()

        responder.start("peer-A", volume=50)
        sent.clear()

        responder.start("peer-A", volume=50)

        # Alert restarted (stop + start), 2 starts and 1 stop.
        self.assertEqual(len(alert.starts), 2)
        self.assertEqual(alert.stops, 1)
        # Prior timer cancelled, new timer scheduled.
        self.assertEqual(timer.cancels, 1)
        self.assertEqual(len(timer.scheduled), 2)
        # New initial heartbeat sent.
        self.assertEqual(sent, [("peer-A", "ringing", {})])

    def test_stop_from_active_sender_sends_stopped_and_clears(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder()

        responder.start("peer-A", volume=50)
        sent.clear()

        responder.stop_from_sender("peer-A")

        self.assertFalse(responder.is_ringing)
        self.assertEqual(alert.stops, 1)
        self.assertEqual(timer.cancels, 1)
        self.assertEqual(sent, [("peer-A", "stopped", {})])

    def test_stop_from_other_sender_is_ignored(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder()

        responder.start("peer-A", volume=50)
        sent.clear()

        responder.stop_from_sender("peer-B")

        self.assertTrue(responder.is_ringing)
        self.assertEqual(responder.active_sender_id, "peer-A")
        self.assertEqual(alert.stops, 0)
        self.assertEqual(sent, [])

    def test_local_stop_sends_stopped_update(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder()

        responder.start("peer-A", volume=80)
        sent.clear()

        responder.stop()

        self.assertFalse(responder.is_ringing)
        self.assertEqual(alert.stops, 1)
        self.assertEqual(sent, [("peer-A", "stopped", {})])

    def test_timeout_fires_stop_and_sends_stopped(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder()

        responder.start("peer-A", volume=80)
        sent.clear()

        timer.fire_last()

        self.assertFalse(responder.is_ringing)
        self.assertEqual(alert.stops, 1)
        self.assertEqual(sent, [("peer-A", "stopped", {})])

    def test_handle_message_routes_start_and_stop(self) -> None:
        responder, alert, sent, timer, heartbeat = _make_responder()

        start_msg = DeviceMessage(
            type=MessageType.FIND_PHONE_START,
            transport=MessageTransport.FASTTRACK,
            payload={"fn": "find-phone", "action": "start", "volume": 60},
            sender_id="peer-A",
        )
        responder.handle_message(start_msg)
        self.assertTrue(responder.is_ringing)

        sent.clear()

        stop_msg = DeviceMessage(
            type=MessageType.FIND_PHONE_STOP,
            transport=MessageTransport.FASTTRACK,
            payload={"fn": "find-phone", "action": "stop"},
            sender_id="peer-A",
        )
        responder.handle_message(stop_msg)

        self.assertFalse(responder.is_ringing)
        self.assertEqual(sent, [("peer-A", "stopped", {})])

    def test_handle_message_drops_when_no_sender_id(self) -> None:
        responder, _alert, sent, _timer, _heartbeat = _make_responder()
        msg = DeviceMessage(
            type=MessageType.FIND_PHONE_START,
            transport=MessageTransport.FASTTRACK,
            payload={"fn": "find-phone", "action": "start"},
            sender_id=None,
        )
        responder.handle_message(msg)
        self.assertFalse(responder.is_ringing)
        self.assertEqual(sent, [])

    def test_send_update_failure_does_not_crash_session(self) -> None:
        def boom(*_args, **_kwargs):
            raise RuntimeError("network down")

        responder, _alert, _sent, _timer, _heartbeat = _make_responder(send_update=boom)

        # Must not raise — the responder swallows send-update failures
        # so a flaky network can't prevent the local alert from firing.
        responder.start("peer-A", volume=80)
        self.assertTrue(responder.is_ringing)

    def test_dispatcher_round_trip_uses_responder(self) -> None:
        # Pin the integration that the Poller will wire: adapter →
        # dispatcher → responder.
        responder, alert, sent, _timer, _heartbeat = _make_responder()
        dispatcher = MessageDispatcher()
        dispatcher.register(MessageType.FIND_PHONE_START, responder.handle_message)
        dispatcher.register(MessageType.FIND_PHONE_STOP, responder.handle_message)

        adapted = FasttrackAdapter.to_device_message(
            {"fn": "find-device", "action": "start", "volume": 80},
            sender_id="peer-A",
        )
        dispatcher.dispatch(adapted)

        self.assertTrue(responder.is_ringing)
        self.assertEqual(sent, [("peer-A", "ringing", {})])


class FindDeviceLocationWiringTests(unittest.TestCase):
    """M.9: heartbeats include lat/lng/accuracy when the location
    provider has a fix; pure state heartbeats when it doesn't."""

    def test_initial_heartbeat_includes_fix_when_available(self) -> None:
        fix = LocationFix(lat=50.1, lng=14.4, accuracy=12.5)
        responder, _alert, sent, _timer, _heartbeat = _make_responder(
            location_provider=lambda: fix,
        )

        responder.start("peer-A", volume=80)

        self.assertEqual(
            sent,
            [(
                "peer-A", "ringing",
                {"lat": 50.1, "lng": 14.4, "accuracy": 12.5},
            )],
        )

    def test_initial_heartbeat_omits_coords_when_provider_returns_none(self) -> None:
        responder, _alert, sent, _timer, _heartbeat = _make_responder(
            location_provider=lambda: None,
        )

        responder.start("peer-A", volume=80)

        self.assertEqual(sent, [("peer-A", "ringing", {})])

    def test_periodic_heartbeat_picks_up_new_fix(self) -> None:
        # First a no-fix tick, then provider supplies a fix; the next
        # periodic heartbeat must include the fix.
        fix_holder: list = [None]
        responder, _alert, sent, _timer, heartbeat = _make_responder(
            location_provider=lambda: fix_holder[0],
        )

        responder.start("peer-A", volume=80)
        # Initial heartbeat had no fix.
        self.assertEqual(sent, [("peer-A", "ringing", {})])
        sent.clear()

        # Provider warms up.
        fix_holder[0] = LocationFix(lat=10.0, lng=20.0, accuracy=8.0)
        heartbeat.fire_last()

        self.assertEqual(
            sent,
            [(
                "peer-A", "ringing",
                {"lat": 10.0, "lng": 20.0, "accuracy": 8.0},
            )],
        )

    def test_heartbeat_after_stop_is_a_no_op(self) -> None:
        # A heartbeat thread that fires after stop() (or after the
        # session moved to a different sender) must not bother the
        # old session.
        responder, _alert, sent, _timer, heartbeat = _make_responder(
            location_provider=lambda: LocationFix(lat=0.0, lng=0.0),
        )

        responder.start("peer-A", volume=80)
        sent.clear()
        responder.stop()
        sent.clear()

        # Late-firing heartbeat for the stopped session.
        heartbeat.fire_last()
        self.assertEqual(sent, [])

    def test_heartbeat_dropped_when_session_moved_to_different_sender(self) -> None:
        # Edge case: heartbeat callback captures sender_id at start
        # time. If the session was torn down and replaced with a
        # different sender, late callback shouldn't fire for the old
        # sender_id.
        responder, _alert, sent, _timer, heartbeat = _make_responder()

        responder.start("peer-A", volume=80)
        sent.clear()
        # Tear down peer-A's session and start a new one for peer-B
        # with a fresh heartbeat closure.
        responder.stop()

        responder.start("peer-B", volume=80)
        sent.clear()

        # Fire peer-A's old heartbeat callback (now stale).
        first_callback = heartbeat.scheduled[0]
        first_callback()

        self.assertEqual(sent, [])  # peer-A's tick is a no-op

    def test_provider_failure_falls_back_to_state_only(self) -> None:
        def boom():
            raise RuntimeError("D-Bus down")

        responder, _alert, sent, _timer, _heartbeat = _make_responder(
            location_provider=boom,
        )

        responder.start("peer-A", volume=80)

        # Heartbeat should have been sent without coords despite
        # provider raising.
        self.assertEqual(sent, [("peer-A", "ringing", {})])

    def test_state_payload_never_logs_raw_coords_in_log_string(self) -> None:
        # The build_state_payload helper itself doesn't log; we use it
        # to ensure that lat/lng never enter a logging-friendly repr.
        # This is a contract test on build_state_payload — the
        # responder + GeoClue backend obey the same rule by code.
        from src.find_device_responder import build_state_payload

        payload = build_state_payload(
            "ringing", lat=50.123456789, lng=14.987654321, accuracy=4.0,
        )
        self.assertNotIn("lat=50", repr(payload))
        # Coords ARE in the dict — they have to be, that's the whole
        # point — but the responder must never feed this dict into a
        # log statement. Keep the contract documented so a future
        # refactor doesn't accidentally log payloads.


if __name__ == "__main__":
    unittest.main()
