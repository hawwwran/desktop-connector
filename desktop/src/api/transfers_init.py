"""Transfer init handshake (classic + streaming negotiation)."""

import logging
import time

import requests

from .constants import (
    CHUNK_MAX_FAILURE_WINDOW_S,
    CHUNK_RETRY_DELAY_S,
    STORAGE_FULL_MAX_WINDOW_S,
)

log = logging.getLogger(__name__)


class TransfersInitMixin:
    def init_transfer(self, transfer_id: str, recipient_id: str,
                      encrypted_meta: str, chunk_count: int,
                      *, mode: str = "classic") -> tuple[str, str | None]:
        """Initialize a transfer on the server.

        Returns (status, negotiated_mode):
          * ('ok', 'classic' | 'streaming') — 201, transfer registered.
             negotiated_mode reflects what the server actually chose; may
             differ from the requested `mode` (server downgrades to
             classic when the recipient is offline, streamingEnabled is
             off, etc. — see docs/plans/streaming-improvement.md §gap
             10–11).
          * ('storage_full', None) — 507, recipient's quota exceeded;
             caller should keep retrying and show WAITING. Classic path
             only — streaming init skips the projected reservation.
          * ('too_large', None) — 413, transfer alone exceeds the
             server's quota. Terminal.
          * ('failed', None) — anything else (network exception, 4xx,
             5xx) — caller decides retry budget.

        Old callers that pass no `mode` get classic behaviour; the
        returned negotiated_mode is always 'classic' on success.
        """
        payload = {
            "transfer_id": transfer_id,
            "recipient_id": recipient_id,
            "encrypted_meta": encrypted_meta,
            "chunk_count": chunk_count,
        }
        if mode != "classic":
            payload["mode"] = mode
        resp = self.conn.request("POST", "/api/transfers/init", json=payload)
        if resp is None:
            return "failed", None
        if resp.status_code == 201:
            negotiated = "classic"
            try:
                body = resp.json()
                if isinstance(body, dict):
                    nm = body.get("negotiated_mode")
                    if isinstance(nm, str) and nm in ("classic", "streaming"):
                        negotiated = nm
            except (ValueError, AttributeError):
                pass
            return "ok", negotiated
        if resp.status_code == 507:
            return "storage_full", None
        if resp.status_code == 413:
            # Transfer itself exceeds the server's quota — terminal, no
            # amount of waiting makes it fit. Caller bails immediately
            # instead of entering WAITING / retry loops.
            return "too_large", None
        return "failed", None

    def _init_transfer_with_retry(self, transfer_id: str, recipient_id: str,
                                   encrypted_meta: str, chunk_count: int,
                                   on_progress: callable = None,
                                   *, mode: str = "classic") -> str | None:
        """Drive init with different retry semantics per failure mode:

        * 507 storage_full: retry indefinitely. The recipient's quota
          is occupied by earlier transfers that will drain as the phone
          downloads them. Caller sees the row in WAITING state. The
          ConnectionManager notes the storage-pressure condition so the
          tray / HomeScreen banner can surface it. (Streaming init
          never returns 507 — the projected-size check is skipped and
          quota is enforced per-chunk instead; see `_upload_stream`.)

        * Network exception / other 5xx: retry on the same 5s cadence
          as chunk upload, capped at CHUNK_MAX_FAILURE_WINDOW_S (2 min),
          then give up — matches the chunk-upload tolerance window.

        * 201 ok: proceed to chunk upload.

        ``on_progress`` signals init-time state via sentinel chunk
        indices — this is the classic path's state channel, kept
        separate from streaming's ``on_stream_progress``:

          * ``(tid, 0, N)``   — initial row placeholder (fired by
                                 send_file before calling this helper).
          * ``(tid, -1, N)``  — 507 storage_full: flip row to WAITING.
                                 Fires once on first 507, then on each
                                 subsequent retry timestamp refresh.
          * ``(tid, -2, N)``  — 413 too_large: terminal, tag row with
                                 failure_reason='too_large'.

        These sentinels pre-date the typed ``on_stream_progress``
        (C.4) and are still live because classic init waiting is a
        distinct flow from streaming mid-stream waiting. They're NOT
        dead code — removing them requires a parallel cleanup of the
        classic callers in windows.py and runners/send_runner.py.
        Left in place; documented here so the contract stays visible.

        Returns the server-negotiated mode string ("classic" or
        "streaming") on success, or None on failure. Callers that only
        need a bool can treat None as False.
        """
        first_failure_at: float | None = None
        waiting_started_at: float | None = None
        signaled_waiting = False
        while True:
            status = "failed"
            negotiated_mode: str | None = None
            try:
                status, negotiated_mode = self.init_transfer(
                    transfer_id, recipient_id, encrypted_meta, chunk_count,
                    mode=mode,
                )
            except (requests.RequestException, OSError, ValueError) as e:
                log.warning("transfer.init.failed transfer_id=%s error_kind=%s",
                            transfer_id[:12], type(e).__name__)
                status = "failed"

            if status == "ok":
                if signaled_waiting:
                    # Release the storage-full flag so the banner clears
                    # the moment this transfer finally lands.
                    self.conn.clear_storage_full()
                return negotiated_mode or "classic"

            if status == "too_large":
                # 413 — no retry. Server's quota is smaller than this
                # single transfer; nothing the client can do but surface
                # the error. Attach the reason via on_progress so the
                # caller can tag the history row.
                log.error("transfer.init.too_large transfer_id=%s", transfer_id[:12])
                if on_progress:
                    try:
                        on_progress(transfer_id, -2, chunk_count)
                    except Exception:
                        log.exception("too_large on_progress failed")
                return None

            if status == "storage_full":
                self.conn.mark_storage_full()
                if not signaled_waiting and on_progress:
                    try:
                        on_progress(transfer_id, -1, chunk_count)
                    except Exception:
                        log.exception("waiting-state on_progress failed")
                    signaled_waiting = True
                    waiting_started_at = time.monotonic()
                # Bounded wait on storage-full: if the recipient quota
                # doesn't free up in STORAGE_FULL_MAX_WINDOW_S (30 min)
                # we give up and surface Failed. Prevents a long-dead
                # send-files subprocess from accidentally keeping the
                # row alive forever.
                if waiting_started_at is not None and \
                        time.monotonic() - waiting_started_at >= STORAGE_FULL_MAX_WINDOW_S:
                    log.warning(
                        "transfer.init.waiting.timed_out transfer_id=%s elapsed=%ds",
                        transfer_id[:12], int(STORAGE_FULL_MAX_WINDOW_S),
                    )
                    return None
                log.info("transfer.init.waiting transfer_id=%s reason=storage_full",
                         transfer_id[:12])
                time.sleep(CHUNK_RETRY_DELAY_S)
                continue

            # status == "failed" — apply 2-min cap.
            now = time.monotonic()
            if first_failure_at is None:
                first_failure_at = now
                log.warning("transfer.init.failed transfer_id=%s reason=retry_in_%ds",
                            transfer_id[:12], int(CHUNK_RETRY_DELAY_S))
            elif now - first_failure_at >= CHUNK_MAX_FAILURE_WINDOW_S:
                return None
            time.sleep(CHUNK_RETRY_DELAY_S)
