"""OUTGOING-side delivery tracker: paints "Delivering X/Y" progress.

Symmetric with Android's ``PollService.deliveryTrackerLoop``. Runs as
a daemon thread off the main poll loop; 500 ms tick, single in-flight
poll at a time, 750 ms abort timeout per poll. Idle when no active
deliveries or offline.

Stall safeguard: if ``chunks_downloaded`` does not advance for
``DELIVERY_STALL_TIMEOUT`` seconds on a given transfer, the tracker
gives up tracking that transfer (clears its progress fields so UI
falls back to "Sent"). The transfer row stays sent/undelivered;
long-poll inline ``sent_status`` and the app-restart delivery check
still catch eventual delivery if the phone comes online.

Does NOT mark ``delivered=True`` itself. When the server reports
``delivery_state == "delivered"`` for any tracked transfer, the
tracker delegates to ``_check_delivery_status`` — the same path used
at app start — as the single source of truth.
"""

import logging
import threading
import time

from ..connection import ConnectionState
from ..history import TransferStatus

log = logging.getLogger(__name__)

# Delivery tracker: after this many seconds with no chunks_downloaded advancement,
# stop fast-polling a given transfer. Transfer stays as sent/undelivered; long-poll
# inline sent_status and app-restart delivery check still catch eventual delivery.
DELIVERY_STALL_TIMEOUT = 2 * 60


class DeliveryTrackerMixin:
    def _delivery_tracker_loop(self) -> None:
        """Paints per-chunk "Delivering X/Y" progress for OUTGOING transfers while
        the phone pulls them off the server.

        Cadence: 500ms tick, single in-flight poll at a time (overlap -> skip + log),
        750ms abort timeout per poll. Idle when no active deliveries or offline.

        Stall safeguard: if chunks_downloaded does not advance for DELIVERY_STALL_TIMEOUT
        seconds on a given transfer, the tracker gives up tracking that transfer
        (clears its progress fields so UI falls back to "Sent"). The transfer row stays
        sent/undelivered; long-poll inline sent_status and app-restart delivery check
        still catch eventual delivery if the phone comes online.

        Does NOT mark delivered=True itself. When the server reports delivery_state
        == "delivered" for any tracked transfer, delegates to _check_delivery_status
        - the same path used at app start - as the single source of truth.

        Symmetric with Android's PollService.deliveryTrackerLoop.
        """
        log.info("delivery.tracker.started")
        in_flight = threading.Lock()

        def run_poll():
            try:
                # Advisory poll: 750ms timeout is aggressive on purpose so
                # overlapping ticks are skipped. track_state=False keeps a
                # missed tick from flipping the global connection state,
                # which would otherwise thrash CONNECTED⇄DISCONNECTED and
                # spam backoff-retry logs.
                statuses = self.api.get_sent_status(timeout=0.75, track_state=False)
                if self._process_delivery_progress(statuses):
                    self._check_delivery_status()
            except Exception:
                log.debug("Delivery tracker poll failed", exc_info=True)
            finally:
                in_flight.release()

        while self._running:
            tick_start = time.monotonic()
            try:
                if self.conn.state == ConnectionState.CONNECTED:
                    undelivered = set(self.history.get_undelivered_transfer_ids())
                    # Prune tracker state for transfers no longer undelivered
                    # (deleted, or marked delivered via long-poll inline path).
                    with self._tracker_state_lock:
                        stale = [k for k in self._tracker_last_progress if k not in undelivered]
                        for k in stale:
                            del self._tracker_last_progress[k]
                        self._tracker_gave_up &= undelivered
                        has_tracked = bool(undelivered - self._tracker_gave_up)

                    if has_tracked:
                        if in_flight.acquire(blocking=False):
                            threading.Thread(target=run_poll, daemon=True).start()
                        else:
                            log.debug("delivery.tracker.skipped reason=previous_in_flight")
            except Exception:
                log.exception("delivery.tracker.failed")
            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, 0.5 - elapsed))

    def _check_delivery_status(self) -> None:
        """Check delivery via separate request (fallback when inline data unavailable)."""
        now = time.time()
        if now - self._last_delivery_check < 0.5:
            return
        self._last_delivery_check = now

        undelivered = self.history.get_undelivered_transfer_ids()
        if not undelivered:
            return

        statuses = self.api.get_sent_status(timeout=3)
        self._process_delivery_statuses(statuses)

    def _process_delivery_statuses(self, statuses: list[dict]) -> None:
        """Process sent-status data (inline long poll or standard / app-start path).

        Authoritative marker for `delivered=True`. The delivery tracker does NOT
        come through here — it paints progress via `_process_delivery_progress`
        and delegates to this function only at the delivered transition.

        Authoritative signal is `delivery_state`: not_started | in_progress | delivered.
        Server guarantees chunks_downloaded == chunk_count iff delivery_state == "delivered"
        (incremented to cap-1 during serving, bumped to full count only on ack).
        """
        undelivered = self.history.get_undelivered_transfer_ids()
        if not undelivered:
            return

        for s in statuses:
            tid = s.get("transfer_id")
            if tid not in undelivered:
                continue

            state = s.get("delivery_state", "not_started")
            chunks_dl = s.get("chunks_downloaded", 0)
            chunk_count = s.get("chunk_count", 0)

            if state == "delivered":
                # Delivery logic cleans up its own progress fields as it marks delivered.
                self.history.update(tid,
                    recipient_chunks_downloaded=0,
                    recipient_chunks_total=0,
                    delivered=True)
                log.info("delivery.acked transfer_id=%s", tid[:12])
            elif state == "aborted":
                # Counterpart aborted (either sender or recipient). Flip
                # the row terminal so the tracker stops polling and UI
                # renders "Aborted" instead of a stuck "Sending".
                abort_reason = s.get("abort_reason")
                log.info("transfer.abort.observed transfer_id=%s reason=%s",
                         tid[:12], abort_reason or "unspecified")
                self.history.update(tid,
                    status=TransferStatus.ABORTED,
                    abort_reason=abort_reason,
                    recipient_chunks_downloaded=0,
                    recipient_chunks_total=0)
            elif state == "in_progress":
                self.history.update(tid,
                    recipient_chunks_downloaded=chunks_dl,
                    recipient_chunks_total=chunk_count)
            elif state == "not_started" and chunks_dl > 0:
                # Streaming overlap: server's delivery_state stays
                # "not_started" until complete=1, but chunks_downloaded
                # climbs as the recipient drains. Paint it. Classic
                # rows never reach this branch (chunks_dl=0 while
                # complete=0).
                self.history.update(tid,
                    recipient_chunks_downloaded=chunks_dl,
                    recipient_chunks_total=chunk_count)

    def _process_delivery_progress(self, statuses: list[dict]) -> bool:
        """Tracker-only: paint progress for in-flight deliveries. Returns True if
        any transfer flipped to delivery_state="delivered" (caller delegates to
        standard poll as the authoritative marker).

        Also runs stall detection: if chunks_downloaded doesn't advance within
        DELIVERY_STALL_TIMEOUT, the transfer is moved to _tracker_gave_up and its
        progress fields are cleared so UI falls back to "Sent".

        DB writes only on change — _tracker_last_progress already tells us whether
        the value moved since last tick. Avoids ~240 redundant history writes per
        stuck transfer over the 2 min stall window.
        """
        undelivered = set(self.history.get_undelivered_transfer_ids())
        if not undelivered:
            return False

        now = time.monotonic()
        any_just_delivered = False

        for s in statuses:
            tid = s.get("transfer_id")
            if tid not in undelivered:
                continue

            state = s.get("delivery_state", "not_started")
            chunks_dl = s.get("chunks_downloaded", 0)
            chunk_count = s.get("chunk_count", 0)

            # Decide action under a single lock acquisition per transfer.
            action: str  # "skip" | "delivered" | "aborted" | "advanced" | "stalled"
            stall_seconds = 0.0
            with self._tracker_state_lock:
                if tid in self._tracker_gave_up:
                    action = "skip"
                elif state == "aborted":
                    # Either-party abort observed via sent-status. For a
                    # sender whose upload completed before the recipient
                    # aborted, this is the ONLY way it learns — the
                    # upload loop already exited, so no 410 Gone will
                    # fire on a follow-up chunk upload. Without this
                    # branch the row stays in SENDING/COMPLETE forever
                    # (user's bug report: "phone swipe-deletes a
                    # download, desktop row stays stuck").
                    self._tracker_last_progress.pop(tid, None)
                    action = "aborted"
                elif state == "delivered":
                    self._tracker_last_progress.pop(tid, None)
                    any_just_delivered = True
                    action = "delivered"
                else:
                    prev = self._tracker_last_progress.get(tid)
                    if prev is None or prev[0] != chunks_dl:
                        self._tracker_last_progress[tid] = (chunks_dl, now)
                        action = "advanced"
                    elif now - prev[1] > DELIVERY_STALL_TIMEOUT:
                        stall_seconds = now - prev[1]
                        self._tracker_gave_up.add(tid)
                        self._tracker_last_progress.pop(tid, None)
                        action = "stalled"
                    else:
                        action = "skip"  # unchanged value, no DB write needed

            # I/O outside the lock.
            if action == "stalled":
                log.warning("delivery.tracker.stall transfer_id=%s stall_seconds=%.0f",
                            tid[:12], stall_seconds)
                self.history.update(tid,
                    recipient_chunks_downloaded=0,
                    recipient_chunks_total=0)
            elif action == "aborted":
                abort_reason = s.get("abort_reason")
                log.info("transfer.abort.observed_via_tracker transfer_id=%s reason=%s",
                         tid[:12], abort_reason or "unspecified")
                self.history.update(tid,
                    status=TransferStatus.ABORTED,
                    abort_reason=abort_reason,
                    recipient_chunks_downloaded=0,
                    recipient_chunks_total=0)
            elif action == "advanced":
                if state == "in_progress":
                    self.history.update(tid,
                        recipient_chunks_downloaded=chunks_dl,
                        recipient_chunks_total=chunk_count)
                elif state == "not_started":
                    # Streaming overlap: during the upload phase (server
                    # lifecycle still UPLOADING → delivery_state maps to
                    # "not_started"), chunks_downloaded climbs as the
                    # recipient drains. Painting whatever the server
                    # reports keeps the "Sending X→Y/N" label accurate.
                    # Classic rows always see chunks_dl=0 here (no
                    # download while complete=0) so byte-for-byte same
                    # as before for classic.
                    self.history.update(tid,
                        recipient_chunks_downloaded=chunks_dl,
                        recipient_chunks_total=chunk_count)
        return any_just_delivered
