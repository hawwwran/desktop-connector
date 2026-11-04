"""Phone-liveness ping driven from the tray.

On-demand only — the menu open and 5-min icon-poll both call
``_maybe_ping``. Atomic check-and-fire under a per-instance lock so two
concurrent menu opens can't double-ping. The server's HIGH-priority
FCM bypasses Doze, so a single ping resolves in ~1 s; the worker
records the result and repaints the icon if remote_online flipped.
"""

import logging
import threading
import time

from ..devices import ConnectedDeviceRegistry

log = logging.getLogger(__name__)

# If the server ping comes back online=false but reports the phone's
# last_seen_at within this window, we still consider it online. Longer
# than typical poll cadences (10s Android, 25s desktop long-poll) but
# short enough that a truly offline phone registers within a minute.
REMOTE_FRESH_WINDOW_S = 60


class PingMixin:
    def _maybe_ping(self, min_age_sec: float) -> None:
        """Atomic check-and-fire: under _ping_lock, confirm we're idle and
        stale enough, then claim the slot before spawning the worker. Prevents
        the icon_poll / _status_text race where both threads could pass the
        gate simultaneously and fire two pings."""
        if not self.config.is_paired:
            return
        target = ConnectedDeviceRegistry(self.config).get_active_device()
        if target is None:
            return
        target_id = target.device_id
        with self._ping_lock:
            if self._ping_in_flight:
                return
            if (time.monotonic() - self._last_ping_time) < min_age_sec:
                return
            self._last_ping_time = time.monotonic()
            self._ping_in_flight = True

        def run():
            try:
                log.debug("ping.request.sent recipient=%s", target_id[:12])
                result = self.api.ping_device(target_id)
                if result is None:
                    return
                online = bool(result.get("online"))
                # Without FCM on the server, the ping handler can only say
                # "online" via the "fresh" shortcut (phone talked to the
                # server this second) — which mostly doesn't fire for
                # phones on 10–25 s poll cadences. Fall back to the
                # server-reported last_seen_at freshness: if the phone
                # contacted the server within REMOTE_FRESH_WINDOW_S it's
                # functionally alive, regardless of what FCM says.
                if not online:
                    last_seen = result.get("last_seen_at") or 0
                    if last_seen and (time.time() - last_seen) <= REMOTE_FRESH_WINDOW_S:
                        online = True
                if online != self._remote_online:
                    log.info("ping.response.received recipient=%s online=%s via=%s rtt_ms=%s",
                             target_id[:12], online, result.get("via"), result.get("rtt_ms"))
                    self._remote_online = online
                    self._update_icon()
            except Exception as e:
                log.warning("ping.request.failed error_kind=%s", type(e).__name__)
            finally:
                with self._ping_lock:
                    self._ping_in_flight = False
        threading.Thread(target=run, daemon=True).start()
