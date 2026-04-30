"""Receive-side find-device handler (M.8).

When another paired device asks this desktop "where are you?", this
responder owns the lifecycle of that locate session:

- start: pin the requesting sender, optionally trigger the GTK alert
  modal + sound (non-silent), and begin sending periodic ``state``
  updates back through the encrypted fasttrack channel.
- stop: tear the session down. Triggered by the requesting sender's
  ``stop`` message, by the user clicking "Stop" in the modal, or by
  the hard timeout (5 min, matching Android's `MAX_TIMEOUT_SECONDS`).
- timeout: same teardown path as stop.

Concurrency rule (D6, mirrors Android's
`findphone.start.dropped_concurrent`): first active request wins.
A second start from a different sender while one is active is dropped
and logged. A re-start from the *same* sender refreshes the session.

The responder is platform-agnostic. The alert modal + sound are
injected behind :class:`FindDeviceAlert`; the encrypted send is
injected behind ``send_update``; the timer is injected so unit tests
don't need to wait wall-clock seconds.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .interfaces.location import LocationFix
from .messaging import DeviceMessage, MessageType

log = logging.getLogger("desktop-connector.find-device")

MAX_TIMEOUT_SECONDS = 300  # 5 minutes; mirrors Android FindPhoneManager
HEARTBEAT_INTERVAL_S = 5.0  # cadence for periodic ringing updates


class FindDeviceAlert(Protocol):
    """Side-effects of a non-silent locate request.

    The default :class:`NoopAlert` is what tests inject. Production
    wiring uses a GTK4-subprocess alert that shows an always-on-top
    modal and plays a repeating sound until ``stop()`` is called.
    """

    def start(self, sender_name: str) -> None: ...

    def stop(self) -> None: ...


class NoopAlert:
    def start(self, sender_name: str) -> None:  # noqa: D401, ARG002
        pass

    def stop(self) -> None:  # noqa: D401
        pass


# A timer abstraction so tests can pass a fake. Returns a "cancel"
# callable.  Real impl uses threading.Timer.
TimerStarter = Callable[[float, Callable[[], None]], Callable[[], None]]
# Heartbeat starter abstraction. Schedules periodic firing of the
# given callback; returns a cancel callable. Tests pass a fake that
# captures the callback for manual firing.
HeartbeatStarter = Callable[[Callable[[], None]], Callable[[], None]]


def _real_timer_starter(seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
    return timer.cancel


def _real_heartbeat_starter(callback: Callable[[], None]) -> Callable[[], None]:
    """Real periodic-heartbeat impl: a daemon thread that sleeps on a
    stop event so cancel() returns immediately.
    """
    stop_event = threading.Event()

    def loop() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_S):
            try:
                callback()
            except Exception:
                log.exception("findphone.heartbeat.loop_failed")

    thread = threading.Thread(
        target=loop, daemon=True, name="find-device-heartbeat",
    )
    thread.start()
    return stop_event.set


# send_update(sender_id, state, *, lat, lng, accuracy) -> bool. Returns
# True on successful queue. M.8 always passes lat/lng/accuracy=None;
# M.9 wires a location provider that supplies coordinates. We keep the
# parameter shape stable so the wire never sees a missed update.
SendUpdate = Callable[..., bool]


@dataclass(frozen=True)
class _Session:
    sender_id: str
    started_at: float
    silent: bool
    cancel_timeout: Callable[[], None]
    cancel_heartbeat: Callable[[], None]


class FindDeviceResponder:
    def __init__(
        self,
        *,
        alert: FindDeviceAlert | None = None,
        send_update: SendUpdate,
        device_name_lookup: Callable[[str], str | None] | None = None,
        location_provider: Callable[[], LocationFix | None] | None = None,
        start_timer: TimerStarter = _real_timer_starter,
        start_heartbeat: HeartbeatStarter = _real_heartbeat_starter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._alert = alert or NoopAlert()
        self._send_update = send_update
        self._device_name_lookup = device_name_lookup or (lambda _: None)
        self._location_provider = location_provider or (lambda: None)
        self._start_timer = start_timer
        self._start_heartbeat = start_heartbeat
        self._clock = clock
        self._lock = threading.RLock()
        self._session: _Session | None = None

    # --- public API ---------------------------------------------------

    @property
    def is_ringing(self) -> bool:
        with self._lock:
            return self._session is not None

    @property
    def active_sender_id(self) -> str | None:
        with self._lock:
            return self._session.sender_id if self._session else None

    def handle_message(self, message: DeviceMessage) -> None:
        """Route an inbound fasttrack DeviceMessage to start/stop.

        Called by the Poller's MessageDispatcher. Unknown sender_id
        (no `pairings` entry) is the dispatcher's responsibility — by
        the time we get here the message is already authenticated and
        decrypted under the sender's symkey.
        """
        sender_id = message.sender_id
        if not sender_id:
            log.warning("findphone.command.dropped reason=no_sender_id")
            return

        if message.type == MessageType.FIND_PHONE_START:
            volume = self._coerce_volume(message.payload.get("volume", 80))
            self.start(sender_id, volume=volume)
        elif message.type == MessageType.FIND_PHONE_STOP:
            self.stop_from_sender(sender_id)
        # FIND_PHONE_LOCATION_UPDATE is purely sender-side; ignore.

    def start(self, sender_id: str, *, volume: int = 80) -> None:
        with self._lock:
            existing = self._session
            if existing is not None and existing.sender_id != sender_id:
                # FCFS: drop the second sender, do not stop the first.
                log.info(
                    "findphone.start.dropped_concurrent active=%s new=%s",
                    existing.sender_id[:12],
                    sender_id[:12],
                )
                return

            if existing is not None:
                # Same-sender re-start: tear down the prior session
                # silently (no "stopped" update — sender already knows
                # they're starting again).
                self._teardown_locked(send_stopped_update=False)

            silent = volume == 0
            sender_name = self._device_name_lookup(sender_id) or sender_id[:12]

            log.info(
                "findphone.start.accepted peer=%s silent=%s",
                sender_id[:12],
                silent,
            )

            cancel_t = self._start_timer(MAX_TIMEOUT_SECONDS, self._on_timeout)
            cancel_h = self._start_heartbeat(
                lambda sid=sender_id: self._heartbeat_tick(sid),
            )
            self._session = _Session(
                sender_id=sender_id,
                started_at=self._clock(),
                silent=silent,
                cancel_timeout=cancel_t,
                cancel_heartbeat=cancel_h,
            )

            if not silent:
                try:
                    self._alert.start(sender_name)
                except Exception:
                    log.exception("findphone.alert.start_failed peer=%s",
                                  sender_id[:12])

        # Send the initial heartbeat outside the lock so a slow API
        # call doesn't block subsequent stop() commands. M.9: include
        # current location fix when one is available.
        self._heartbeat_tick(sender_id)

    def stop(self, *, send_stopped_update: bool = True) -> None:
        """Locally-initiated stop (user clicked "Stop", or timeout)."""
        with self._lock:
            if self._session is None:
                return
            sender_id = self._session.sender_id
            self._teardown_locked(send_stopped_update=False)

        if send_stopped_update:
            self._send_state_update(sender_id, "stopped")

    def stop_from_sender(self, sender_id: str) -> None:
        """``fn=find-phone action=stop`` arrived from the sender.

        Quietly accept stop only from the active sender. Silently
        ignore stops from other paired senders so a compromised second
        device can't tear down a legitimately-running session — D6.
        """
        with self._lock:
            if self._session is None:
                return
            if self._session.sender_id != sender_id:
                log.info(
                    "findphone.stop.ignored reason=wrong_sender active=%s saw=%s",
                    self._session.sender_id[:12],
                    sender_id[:12],
                )
                return
            log.info("findphone.stop.accepted peer=%s", sender_id[:12])
            self._teardown_locked(send_stopped_update=False)

        # Acknowledge with a single ``stopped`` update so the sender
        # transitions out of "ringing" cleanly even if they re-issued
        # stop concurrently.
        self._send_state_update(sender_id, "stopped")

    # --- internal -----------------------------------------------------

    def _on_timeout(self) -> None:
        log.info("findphone.timeout fired")
        self.stop(send_stopped_update=True)

    def _teardown_locked(self, *, send_stopped_update: bool) -> None:
        # Caller holds self._lock.
        session = self._session
        if session is None:
            return
        try:
            session.cancel_timeout()
        except Exception:
            log.debug("findphone.timeout.cancel_failed", exc_info=True)
        try:
            session.cancel_heartbeat()
        except Exception:
            log.debug("findphone.heartbeat.cancel_failed", exc_info=True)
        self._session = None

        if not session.silent:
            try:
                self._alert.stop()
            except Exception:
                log.exception("findphone.alert.stop_failed peer=%s",
                              session.sender_id[:12])

        if send_stopped_update:
            # Caller wanted us to send while still inside the lock:
            # not done here. Public methods send outside the lock.
            pass

    def _heartbeat_tick(self, sender_id: str) -> None:
        """Send one ``state="ringing"`` update with the current fix.

        Skips when the session has already moved on (different sender
        or torn down) so a late-firing thread can't bother a fresh
        sender or a closed session.
        """
        with self._lock:
            session = self._session
            if session is None or session.sender_id != sender_id:
                return

        coords: dict[str, float] = {}
        try:
            fix = self._location_provider()
        except Exception:
            log.exception("findphone.location.provider_failed")
            fix = None
        if fix is not None:
            coords["lat"] = fix.lat
            coords["lng"] = fix.lng
            if fix.accuracy is not None:
                coords["accuracy"] = fix.accuracy

        try:
            ok = self._send_update(sender_id, "ringing", **coords)
        except Exception:
            log.exception(
                "findphone.update.send_failed peer=%s state=ringing",
                sender_id[:12],
            )
            return
        if not ok:
            log.warning(
                "findphone.update.send_rejected peer=%s state=ringing",
                sender_id[:12],
            )

    def _send_state_update(self, sender_id: str, state: str) -> None:
        try:
            ok = self._send_update(sender_id, state)
        except Exception:
            log.exception(
                "findphone.update.send_failed peer=%s state=%s",
                sender_id[:12],
                state,
            )
            return
        if not ok:
            log.warning(
                "findphone.update.send_rejected peer=%s state=%s",
                sender_id[:12],
                state,
            )

    @staticmethod
    def _coerce_volume(value) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return 80
        return max(0, min(100, v))


def build_state_payload(state: str, *, lat=None, lng=None, accuracy=None) -> dict:
    """Build the JSON payload the desktop sends back during locate.

    Wire stays `fn=find-phone` per D5; receivers also accept `find-device`.
    Coordinates are omitted entirely when not available — Android's
    sender path treats absence as "no GPS yet, just heartbeat".
    Tests rely on the exact key set; do not silently re-order.
    """
    payload = {"fn": "find-phone", "state": state}
    if lat is not None and lng is not None:
        payload["lat"] = lat
        payload["lng"] = lng
        if accuracy is not None:
            payload["accuracy"] = accuracy
    return payload


def encode_state_payload(state: str, *, lat=None, lng=None, accuracy=None) -> bytes:
    return json.dumps(
        build_state_payload(state, lat=lat, lng=lng, accuracy=accuracy),
        separators=(",", ":"),
    ).encode()
