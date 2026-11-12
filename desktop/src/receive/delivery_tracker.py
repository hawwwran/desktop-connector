"""OUTGOING-side delivery tracker: paints "Delivering X/Y" progress.

Runs as a daemon thread off the main poll loop. The loop ticks every
500 ms against local history only; the ``sent-status`` request itself
is rate-limited by a backoff (see ``TRACKER_MIN_INTERVAL``). Single
in-flight poll at a time, 750 ms abort timeout per poll. Idle when
offline or when nothing is left to learn about any sent transfer.

Poll backoff: only for a send that is going nowhere. While news keeps
arriving (recipient progress, a delivery, local upload activity) the
poll stays at the minimum interval. After ``TRACKER_FAST_WINDOW``
seconds without any news, every further poll that brings none (no
progress, no delivery, no answer at all) doubles the interval, up to
``TRACKER_MAX_INTERVAL``. The next piece of news drops it back to the
minimum. A recipient that stays offline for hours therefore costs one
request a minute, not two a second.

Stall safeguard: if ``chunks_downloaded`` does not advance for
``DELIVERY_STALL_TIMEOUT`` seconds on a given transfer, the tracker
gives up on it for UI purposes (clears its progress fields so the row
falls back to "Sent", and the tray stops showing an outgoing
transfer). The row stays sent/undelivered and is still covered by the
backed-off poll, so late progress or delivery is picked up. A transfer
the server does not list at all gets the same timeout and is then
dropped from polling entirely.

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

# After this many seconds with no chunks_downloaded advancement the tracker
# stops treating a transfer as live (UI + tray). Polling continues, backed off.
DELIVERY_STALL_TIMEOUT = 2 * 60

# sent-status poll cadence. A delivery that is going fine is polled at MIN
# throughout: the interval only starts to grow once TRACKER_FAST_WINDOW has
# passed with no news at all, then doubles per poll up to MAX
# (1, 2, 4, 8, 16, 32, 60 s).
TRACKER_MIN_INTERVAL = 0.5
TRACKER_MAX_INTERVAL = 60.0
TRACKER_BACKOFF_FACTOR = 2.0
TRACKER_FAST_WINDOW = 30.0

# _tracker_last_progress value for a transfer the server did not list.
_ABSENT = -1


class DeliveryTrackerMixin:
    def _delivery_tracker_loop(self) -> None:
        """Tick every TRACKER_MIN_INTERVAL; fire a sent-status poll when
        the backoff says one is due. See the module docstring."""
        log.info("delivery.tracker.started")
        in_flight = threading.Lock()

        def run_poll():
            try:
                self._tracker_poll_once()
            finally:
                in_flight.release()

        while self._running:
            tick_start = time.monotonic()
            try:
                if (self.conn.state == ConnectionState.CONNECTED
                        and self._tracker_poll_due()):
                    if in_flight.acquire(blocking=False):
                        threading.Thread(target=run_poll, daemon=True).start()
                    else:
                        log.debug("delivery.tracker.skipped reason=previous_in_flight")
            except Exception:
                log.exception("delivery.tracker.failed")
            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, TRACKER_MIN_INTERVAL - elapsed))

    def _tracker_poll_due(self) -> bool:
        """Local-only tick: prune tracker state, reset the backoff on
        local upload activity, and say whether a poll should fire now."""
        upload_state = self.history.get_undelivered_upload_state()
        undelivered = set(upload_state)
        now = time.monotonic()
        with self._tracker_state_lock:
            # Drop state for transfers no longer undelivered (deleted, or
            # marked delivered via the long-poll inline path).
            for tid in [k for k in self._tracker_last_progress if k not in undelivered]:
                del self._tracker_last_progress[tid]
            self._tracker_gave_up &= undelivered
            self._tracker_absent &= undelivered
            if upload_state != self._tracker_upload_state:
                # A new send, or an uploader making progress: delivery
                # news is likely soon, so poll promptly again.
                self._tracker_upload_state = upload_state
                self._tracker_interval = TRACKER_MIN_INTERVAL
                self._tracker_next_poll_at = now
                self._tracker_last_news_at = now
            pollable = undelivered - self._tracker_absent
            return bool(pollable) and now >= self._tracker_next_poll_at

    def _tracker_poll_once(self) -> None:
        """One sent-status poll plus the backoff update that follows it."""
        news = False
        try:
            # Advisory poll: 750ms timeout is aggressive on purpose so
            # overlapping ticks are skipped. track_state=False keeps a
            # missed tick from flipping the global connection state,
            # which would otherwise thrash CONNECTED⇄DISCONNECTED and
            # spam backoff-retry logs.
            statuses = self.api.fetch_sent_status(timeout=0.75, track_state=False)
            if statuses is not None:
                any_delivered, news = self._process_delivery_progress(statuses)
                if any_delivered:
                    self._check_delivery_status()
        except Exception:
            log.debug("Delivery tracker poll failed", exc_info=True)
        finally:
            self._tracker_reschedule(news)

    def _tracker_reschedule(self, news: bool) -> None:
        now = time.monotonic()
        with self._tracker_state_lock:
            if news:
                self._tracker_last_news_at = now
            if now - self._tracker_last_news_at < TRACKER_FAST_WINDOW:
                self._tracker_interval = TRACKER_MIN_INTERVAL
            else:
                self._tracker_interval = min(
                    self._tracker_interval * TRACKER_BACKOFF_FACTOR,
                    TRACKER_MAX_INTERVAL,
                )
            self._tracker_next_poll_at = now + self._tracker_interval

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

    def _process_delivery_progress(self, statuses: list[dict]) -> tuple[bool, bool]:
        """Tracker-only: paint progress for in-flight deliveries.

        Returns ``(any_just_delivered, news)``. ``any_just_delivered``
        tells the caller to delegate to the standard poll, which is the
        authoritative marker for delivered. ``news`` is True when the
        server reported something that moved (recipient progress,
        delivery, abort) and drives the poll backoff.

        Also runs stall detection: if chunks_downloaded doesn't advance within
        DELIVERY_STALL_TIMEOUT, the transfer is moved to _tracker_gave_up and its
        progress fields are cleared so UI falls back to "Sent". If the
        recipient moves again later, the transfer is picked back up.

        DB writes only on change — _tracker_last_progress already tells us whether
        the value moved since the last poll.
        """
        undelivered = set(self.history.get_undelivered_transfer_ids())
        if not undelivered:
            return False, False

        now = time.monotonic()
        any_just_delivered = False
        news = False
        listed: set[str] = set()

        for s in statuses:
            tid = s.get("transfer_id")
            if tid not in undelivered:
                continue
            listed.add(tid)

            state = s.get("delivery_state", "not_started")
            chunks_dl = s.get("chunks_downloaded", 0)
            chunk_count = s.get("chunk_count", 0)

            # Decide action under a single lock acquisition per transfer.
            action: str  # "skip" | "delivered" | "aborted" | "advanced" | "stalled"
            stall_seconds = 0.0
            with self._tracker_state_lock:
                self._tracker_absent.discard(tid)
                if state == "aborted":
                    # Either-party abort observed via sent-status. For a
                    # sender whose upload completed before the recipient
                    # aborted, this is the ONLY way it learns — the
                    # upload loop already exited, so no 410 Gone will
                    # fire on a follow-up chunk upload. Without this
                    # branch the row stays in SENDING/COMPLETE forever
                    # (user's bug report: "phone swipe-deletes a
                    # download, desktop row stays stuck").
                    self._tracker_last_progress.pop(tid, None)
                    news = True
                    action = "aborted"
                elif state == "delivered":
                    self._tracker_last_progress.pop(tid, None)
                    any_just_delivered = True
                    news = True
                    action = "delivered"
                else:
                    prev = self._tracker_last_progress.get(tid)
                    if prev is None or prev[0] != chunks_dl:
                        self._tracker_last_progress[tid] = (chunks_dl, now)
                        # First sighting is not news; a moved counter is,
                        # and it revives a transfer we had given up on.
                        if prev is not None:
                            news = True
                        self._tracker_gave_up.discard(tid)
                        action = "advanced"
                    elif tid in self._tracker_gave_up:
                        action = "skip"
                    elif now - prev[1] > DELIVERY_STALL_TIMEOUT:
                        stall_seconds = now - prev[1]
                        # Keep the last_progress entry: it is what lets a
                        # later change in chunks_dl revive the transfer.
                        self._tracker_gave_up.add(tid)
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

        for tid in undelivered - listed:
            self._note_absent_transfer(tid, now)
        return any_just_delivered, news

    def _note_absent_transfer(self, tid: str, now: float) -> None:
        """Stall timer for a sent row the server does not list (expired
        server-side, or sent under credentials this process doesn't hold).
        Without it such a row counts as a live outgoing transfer forever."""
        with self._tracker_state_lock:
            if tid in self._tracker_gave_up:
                # Already stalled while listed, now gone: stop polling for it.
                self._tracker_absent.add(tid)
                return
            prev = self._tracker_last_progress.get(tid)
            if prev is None or prev[0] != _ABSENT:
                self._tracker_last_progress[tid] = (_ABSENT, now)
                return
            absent_seconds = now - prev[1]
            if absent_seconds <= DELIVERY_STALL_TIMEOUT:
                return
            self._tracker_gave_up.add(tid)
            self._tracker_absent.add(tid)
        log.warning("delivery.tracker.stall transfer_id=%s stall_seconds=%.0f mode=absent",
                    tid[:12], absent_seconds)
        self.history.update(tid,
            recipient_chunks_downloaded=0,
            recipient_chunks_total=0)
