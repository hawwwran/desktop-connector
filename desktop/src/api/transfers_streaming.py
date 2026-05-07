"""Streaming sender state machine.

Per-chunk encrypt → upload with two interleaved retry budgets:
  * 507 quota: STREAM_QUOTA_BACKOFF_RAMP_S backoff up to STORAGE_FULL_MAX_WINDOW_S.
  * Network: classic 5-s cadence, CHUNK_MAX_FAILURE_WINDOW_S budget.

Returns transfer_id on success, None on terminal failure (aborted,
failed, quota_timeout). See docs/plans/streaming-improvement.md §3.1.
"""

import logging
import time
from pathlib import Path

import requests

from ..crypto import CHUNK_SIZE, KeyManager
from .constants import (
    CHUNK_MAX_FAILURE_WINDOW_S,
    CHUNK_RETRY_DELAY_S,
    STORAGE_FULL_MAX_WINDOW_S,
    STREAM_QUOTA_BACKOFF_RAMP_S,
    UPLOAD_ABORTED,
    UPLOAD_NETWORK_ERROR,
    UPLOAD_OK,
    UPLOAD_STORAGE_FULL,
)
from .outcomes import ChunkUploadOutcome

log = logging.getLogger(__name__)


class TransfersStreamingMixin:
    def _upload_stream(self, filepath: Path, transfer_id: str,
                       chunk_count: int, base_nonce: bytes,
                       symmetric_key: bytes,
                       on_stream_progress: callable = None) -> str | None:
        """Streaming upload state machine (see docs/plans/streaming-
        improvement.md §3.1 and desktop-streaming-relay-plan.md §C.4).

        Sequential per-chunk encrypt → upload. Outcomes:
          * UPLOAD_OK         → bump uploaded counter, fire
                                 on_stream_progress(state='sending').
          * UPLOAD_STORAGE_FULL (507) → enter waiting_stream, backoff
                                 per STREAM_QUOTA_BACKOFF_RAMP_S, retry
                                 the SAME chunk. On 30-min expiry →
                                 abort(sender_failed), state='failed'.
          * UPLOAD_ABORTED (410) → recipient_abort from the other side.
                                 State flips to 'aborted'; we do NOT
                                 DELETE (server already wiped).
          * network/other      → reuse the classic 2-min / 5s budget.
                                 On exhaustion → abort(sender_failed),
                                 state='failed'.

        Returns transfer_id on success, None on any terminal failure
        (aborted, failed, quota_timeout).

        On entry we fire on_stream_progress(tid, 0, N, 'sending') so the
        caller can flip the history row off the "uploading 0/N" placeholder
        written just before init.
        """
        # Initial state: uploaded 0 chunks, streaming negotiated. Caller
        # rewrites its row mode + status here.
        if on_stream_progress:
            on_stream_progress(transfer_id, 0, chunk_count, "sending")

        try:
            with open(filepath, "rb") as f:
                for index in range(chunk_count):
                    plaintext = f.read(CHUNK_SIZE)
                    encrypted = KeyManager.encrypt_chunk(
                        plaintext, base_nonce, index, symmetric_key)
                    result = self._upload_stream_chunk(
                        transfer_id, index, chunk_count, encrypted,
                        on_stream_progress,
                    )
                    if result == "ok":
                        if on_stream_progress:
                            on_stream_progress(transfer_id, index + 1,
                                               chunk_count, "sending")
                        continue
                    if result == "aborted":
                        if on_stream_progress:
                            on_stream_progress(transfer_id, index,
                                               chunk_count, "aborted")
                        log.info(
                            "transfer.stream.aborted_by_recipient transfer_id=%s "
                            "chunk_index=%d",
                            transfer_id[:12], index,
                        )
                        return None
                    if result == "failed":
                        # Client-side give-up: network budget exhausted
                        # OR 30-min quota waiting window expired. Tell
                        # the server so the recipient's row can flip to
                        # aborted, then surface failure locally.
                        self.abort_transfer(transfer_id, "sender_failed")
                        if on_stream_progress:
                            on_stream_progress(transfer_id, index,
                                               chunk_count, "failed")
                        log.error(
                            "transfer.upload.failed transfer_id=%s "
                            "chunk_index=%d mode=streaming",
                            transfer_id[:12], index,
                        )
                        return None
                    # Defensive: unknown result — bail like a failure.
                    log.error(
                        "transfer.upload.failed transfer_id=%s "
                        "reason=unknown_upload_result result=%s",
                        transfer_id[:12], result,
                    )
                    self.abort_transfer(transfer_id, "sender_failed")
                    if on_stream_progress:
                        on_stream_progress(transfer_id, index, chunk_count, "failed")
                    return None
        except OSError as e:
            log.error("transfer.upload.failed transfer_id=%s error_kind=%s "
                      "mode=streaming",
                      transfer_id[:12], type(e).__name__)
            self.abort_transfer(transfer_id, "sender_failed")
            if on_stream_progress:
                on_stream_progress(transfer_id, 0, chunk_count, "failed")
            return None

        log.info("transfer.upload.completed transfer_id=%s mode=streaming",
                 transfer_id[:12])
        return transfer_id

    def _upload_stream_chunk(self, transfer_id: str, index: int,
                              chunk_count: int, encrypted: bytes,
                              on_stream_progress: callable = None) -> str:
        """Upload a single streaming chunk with the full streaming retry
        policy. Returns one of 'ok' | 'aborted' | 'failed'.

        Two interleaved budgets:
          * 507 quota waiting: STORAGE_FULL_MAX_WINDOW_S (30 min) of
            continuous 507s → failed. Backoff follows
            STREAM_QUOTA_BACKOFF_RAMP_S (2→4→8→16→30s).
          * Network errors: CHUNK_MAX_FAILURE_WINDOW_S (2 min) of
            continuous non-507, non-410 errors → failed. 5-s cadence.

        Each successful upload (OK) resets both budgets. A non-507
        response also clears the storage-full flag in the connection
        so the tray banner hides.
        """
        quota_waiting_since: float | None = None
        quota_ramp_idx = 0
        network_started_at: float | None = None
        signaled_waiting = False

        while True:
            try:
                outcome = self.upload_chunk(transfer_id, index, encrypted)
            except (requests.RequestException, OSError, ValueError) as e:
                log.warning(
                    "transfer.chunk.failed transfer_id=%s chunk_index=%d "
                    "error_kind=%s",
                    transfer_id[:12], index, type(e).__name__,
                )
                outcome = ChunkUploadOutcome(status=UPLOAD_NETWORK_ERROR)

            if outcome.status == UPLOAD_OK:
                log.debug(
                    "transfer.chunk.uploaded transfer_id=%s chunk_index=%d/%d mode=streaming",
                    transfer_id[:12], index + 1, chunk_count,
                )
                if signaled_waiting:
                    # Drain observed — clear the global storage-full
                    # banner. The row status flips back to 'sending' via
                    # the on_stream_progress call in the caller's loop.
                    self.conn.clear_storage_full()
                return "ok"

            if outcome.status == UPLOAD_ABORTED:
                log.info(
                    "transfer.stream.aborted transfer_id=%s chunk_index=%d reason=%s",
                    transfer_id[:12], index, outcome.abort_reason or "unknown",
                )
                return "aborted"

            if outcome.status == UPLOAD_STORAGE_FULL:
                # Reset network streak (this isn't a network error).
                network_started_at = None
                now = time.monotonic()
                if quota_waiting_since is None:
                    quota_waiting_since = now
                    self.conn.mark_storage_full()
                    if not signaled_waiting and on_stream_progress:
                        # Flip caller's row to waiting_stream on first 507.
                        on_stream_progress(
                            transfer_id, index, chunk_count, "waiting_stream",
                        )
                        signaled_waiting = True
                elapsed = now - quota_waiting_since
                if elapsed >= STORAGE_FULL_MAX_WINDOW_S:
                    log.warning(
                        "transfer.stream.waiting_quota.timed_out "
                        "transfer_id=%s chunk_index=%d elapsed=%ds",
                        transfer_id[:12], index, int(elapsed),
                    )
                    return "failed"
                delay = STREAM_QUOTA_BACKOFF_RAMP_S[
                    min(quota_ramp_idx, len(STREAM_QUOTA_BACKOFF_RAMP_S) - 1)
                ]
                quota_ramp_idx = min(quota_ramp_idx + 1,
                                     len(STREAM_QUOTA_BACKOFF_RAMP_S) - 1)
                log.info(
                    "transfer.stream.waiting_quota transfer_id=%s chunk_index=%d "
                    "retry_in=%.1fs elapsed=%ds",
                    transfer_id[:12], index, delay, int(elapsed),
                )
                time.sleep(delay)
                continue

            # Everything else: network, auth, 404, generic server error.
            # Matches the classic helper's 2-min / 5-s cadence.
            now = time.monotonic()
            if network_started_at is None:
                network_started_at = now
                log.warning(
                    "transfer.chunk.failed transfer_id=%s chunk_index=%d/%d "
                    "status=%s retry_in=%ds",
                    transfer_id[:12], index + 1, chunk_count, outcome.status,
                    int(CHUNK_RETRY_DELAY_S),
                )
            elif now - network_started_at >= CHUNK_MAX_FAILURE_WINDOW_S:
                log.error(
                    "transfer.chunk.failed transfer_id=%s chunk_index=%d "
                    "final_status=%s elapsed=%ds",
                    transfer_id[:12], index, outcome.status,
                    int(now - network_started_at),
                )
                return "failed"
            # Non-507 reset of the quota ramp, but keep the quota_waiting_since
            # null — we already nulled it above if we were in quota mode.
            quota_ramp_idx = 0
            time.sleep(CHUNK_RETRY_DELAY_S)
