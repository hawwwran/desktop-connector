"""Streaming-mode download path (per-chunk pull-and-ack).

Pulls chunks sequentially, ACKs each one so the server can wipe its
blob immediately. Peak on-disk usage stays at the in-flight window
between the sender's write head and the recipient's read head.

Unlike classic, the transfer may surface in /pending before all chunks
are uploaded — GETs against a not-yet-stored chunk come back 425 Too
Early. Honours the server's hint + a bounded ramp, with a 5-min
dead-upstream budget per chunk.
"""

import logging
import os
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag

from ..api_client import DOWNLOAD_ABORTED, DOWNLOAD_OK, DOWNLOAD_TOO_EARLY
from ..crypto import KeyManager
from ..history import TransferStatus
from ..receive_actions import ReceiveActionBatch

log = logging.getLogger(__name__)

# Streaming recipient retry policy — see
# docs/plans/desktop-streaming-relay-plan.md §C.3 and
# docs/plans/streaming-improvement.md §5.1.
#
# 425 "too early" means "sender hasn't uploaded this chunk yet". We
# honour the server's Retry-After hint (ms precision via
# retry_after_ms, defaults to 1 s) but cap each sleep with our own
# ramp so a runaway hint can't pin us. After 5 min of continuous 425s
# with no successful chunk, we abort with reason=recipient_abort so
# the sender's row flips to aborted and both sides clean up.
STREAM_CHUNK_WAIT_BUDGET_S = 5 * 60
STREAM_CHUNK_WAIT_RAMP_S = (1.0, 2.0, 4.0, 8.0, 10.0)
# Non-425 errors (network flake, decrypt failure from a torn atomic
# rename window, server 5xx) reuse the classic 3-attempt budget with
# 2 s × attempt backoff. Separate counter from the 425 streak so a
# mix of error modes doesn't falsely look like a dead upstream.
STREAM_CHUNK_NETWORK_ATTEMPTS = 3


class StreamingDownloadMixin:
    def _receive_streaming_transfer(self, transfer_id: str, sender_id: str,
                                    filename: str, chunk_count: int,
                                    base_nonce: bytes, symmetric_key: bytes, *,
                                    receive_action_batch: ReceiveActionBatch | None = None) -> None:
        """Streaming-relay recipient: pull chunks sequentially, ACK each
        one so the server can wipe its blob immediately.

        Key differences from the classic path:
          * Transfer may surface in /pending before all chunks are
            uploaded — GETs against a not-yet-stored chunk come back
            425 Too Early with a retry hint. We honour the hint +
            our own ramp, with a 5-min dead-upstream budget per chunk.
          * Per-chunk ACK (POST .../chunks/{i}/ack) replaces the single
            transfer-level ACK. The server deletes the blob on ack,
            so peak on-disk use stays at the in-flight window between
            sender's write head and our read head.
          * No transfer-level ack at the end — the final per-chunk ack
            is what marks delivery server-side (flips downloaded=1).

        On unrecoverable failure (5-min 425 budget, 3-attempt network
        exhaustion, decrypt loop) we DELETE the transfer with
        reason=recipient_abort so the sender's row flips to aborted.
        The server returning 410 mid-stream means the other side already
        aborted; we clean up locally without re-calling DELETE.
        """
        self.history.add(
            filename=filename,
            display_label=filename,
            direction="received",
            size=0,
            transfer_id=transfer_id,
            sender_id=sender_id,
            status=TransferStatus.DOWNLOADING,
            chunks_downloaded=0,
            chunks_total=chunk_count,
            mode="streaming",
            peer_device_id=sender_id,
        )

        try:
            save_dir = self.config.save_directory
            parts_dir = save_dir / ".parts"
            parts_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Cannot prepare save / .parts directories")
            self.history.update(transfer_id, status=TransferStatus.FAILED,
                                chunks_downloaded=0, chunks_total=0)
            # Server-side cleanup: DELETE with recipient_abort so the
            # sender doesn't sit on a dead transfer.
            self.api.abort_transfer(transfer_id, "recipient_abort")
            return

        temp_path = parts_dir / f".incoming_{transfer_id}.part"

        success = False
        upstream_aborted = False
        upstream_abort_reason: str | None = None
        try:
            # `wb` truncates any stale partial from a prior aborted attempt.
            # No resume across app restarts — see streaming-improvement §9.
            with open(temp_path, "wb") as out:
                for i in range(chunk_count):
                    state, payload = self._stream_download_chunk(
                        transfer_id, i, chunk_count, symmetric_key)
                    if state == "aborted":
                        upstream_aborted = True
                        # Server may have 410'd with no reason
                        # (e.g. "Chunk already acknowledged and wiped"
                        # on a restart-mid-stream), in which case we
                        # render the row as plain "Aborted".
                        upstream_abort_reason = payload  # may be None
                        log.info(
                            "transfer.stream.aborted_by_upstream transfer_id=%s "
                            "chunk_index=%d reason=%s",
                            transfer_id[:12], i,
                            upstream_abort_reason or "unspecified",
                        )
                        return
                    if state != "ok":
                        # "failed" — budget exhausted, decrypt loop, etc.
                        # Tell the server we're done and mark locally.
                        log.error(
                            "transfer.stream.recipient_failed transfer_id=%s "
                            "chunk_index=%d reason=%s",
                            transfer_id[:12], i, payload or "unknown",
                        )
                        self.api.abort_transfer(transfer_id, "recipient_abort")
                        self.history.update(transfer_id,
                                            status=TransferStatus.FAILED,
                                            chunks_downloaded=0,
                                            chunks_total=0)
                        return
                    out.write(payload)
                    # Flush chunk bytes into the page cache BEFORE the
                    # ack tells the server to delete its blob. A crash
                    # between these two still loses the chunk (server
                    # deletes the source of truth), but the in-RAM
                    # buffer staying in buffer makes that window slightly
                    # larger; flush shrinks it to just the fsync
                    # barrier.
                    out.flush()

                    if not self.api.ack_chunk(transfer_id, i):
                        # ACK failure mid-stream is a client-side
                        # network glitch: we've written plaintext to
                        # .part but the server still holds the blob.
                        # Abort cleanly — retrying ack would require
                        # tracking per-chunk ack state and we'd rather
                        # keep the simple sequential model for C.3.
                        log.error(
                            "transfer.chunk.ack_failed transfer_id=%s chunk_index=%d",
                            transfer_id[:12], i,
                        )
                        self.api.abort_transfer(transfer_id, "recipient_abort")
                        self.history.update(transfer_id,
                                            status=TransferStatus.FAILED,
                                            chunks_downloaded=0,
                                            chunks_total=0)
                        return
                    log.debug(
                        "transfer.chunk.acked_and_deleted transfer_id=%s chunk_index=%d/%d",
                        transfer_id[:12], i + 1, chunk_count,
                    )
                    self.history.update(transfer_id, chunks_downloaded=i + 1)
                out.flush()
                os.fsync(out.fileno())
            success = True
        except OSError:
            log.exception("Streaming write failed for %s", transfer_id[:12])
            self.history.update(transfer_id, status=TransferStatus.FAILED,
                                chunks_downloaded=0, chunks_total=0)
            self.api.abort_transfer(transfer_id, "recipient_abort")
            return
        finally:
            if not success:
                self._delete_quietly(temp_path)
                if upstream_aborted:
                    self.history.update(
                        transfer_id,
                        status=TransferStatus.ABORTED,
                        abort_reason=upstream_abort_reason,
                        chunks_downloaded=0,
                        chunks_total=0,
                    )

        final_path = self._finalize_temp_to_unique(temp_path, save_dir, filename)
        if final_path is None:
            self.history.update(transfer_id, status=TransferStatus.FAILED,
                                chunks_downloaded=0, chunks_total=0)
            # Blobs are already wiped on server — no abort needed.
            return

        final_size = final_path.stat().st_size
        log.info(
            "transfer.download.completed transfer_id=%s bytes=%d name=%s mode=streaming",
            transfer_id[:12], final_size, final_path.name,
        )

        # No transfer-level ACK in streaming mode — the final per-chunk
        # ack already flipped the server row to downloaded=1.
        self.history.update(
            transfer_id,
            status=TransferStatus.COMPLETE,
            size=final_size,
            content_path=str(final_path),
            delivered=True,
            chunks_downloaded=0,
            chunks_total=0,
        )
        action_ran = self._apply_receive_file_action(
            final_path,
            receive_action_batch=receive_action_batch,
        )
        if not action_ran:
            try:
                self.platform.notifications.notify_file_received(final_path)
            except Exception:
                log.exception("notify_file_received failed")
        for cb in self._on_file_received:
            try:
                cb(final_path)
            except Exception:
                log.exception("File received callback error")

    def _stream_download_chunk(self, transfer_id: str, index: int,
                               chunk_count: int,
                               symmetric_key: bytes) -> tuple[str, object]:
        """Streaming per-chunk download with typed outcomes.

        Returns ``(state, payload)``:
          * ``("ok", plaintext_bytes)`` — ready to write and ack.
          * ``("aborted", abort_reason_or_None)`` — server returned 410;
             transfer is dead on the server side. ``abort_reason`` may
             be ``None`` when the 410 was "chunk already acked and wiped"
             rather than a real abort.
          * ``("failed", human_reason)`` — we exhausted our retry budget
             (5-min 425 wait OR 3 network attempts OR decrypt loop).

        Policy per ``docs/plans/streaming-improvement.md §5.1`` and
        ``docs/plans/desktop-streaming-relay-plan.md §C.3``.
        """
        network_attempts = 0
        wait_started_at: float | None = None
        wait_ramp_idx = 0

        while True:
            outcome = self.api.download_chunk(transfer_id, index)

            if outcome.status == DOWNLOAD_OK and outcome.data is not None:
                try:
                    plaintext = KeyManager.decrypt_chunk(outcome.data, symmetric_key)
                    return ("ok", plaintext)
                except InvalidTag:
                    # Belt-and-suspenders: the server's atomic rename
                    # should prevent torn reads, but if we somehow
                    # observe one, re-download via the network-retry
                    # path. Reset the 425 streak (this wasn't a 425),
                    # increment the network streak.
                    log.warning(
                        "transfer.chunk.too_early transfer_id=%s chunk_index=%d "
                        "reason=decrypt_failed (retry as network error)",
                        transfer_id[:12], index,
                    )
                    # Fall through to the generic retry branch.
                    retry_hint = "decrypt_failed"
                    wait_started_at = None
                    wait_ramp_idx = 0
                    network_attempts += 1
                    if network_attempts >= STREAM_CHUNK_NETWORK_ATTEMPTS:
                        return ("failed",
                                f"chunk_{index}_decrypt_failed")
                    time.sleep(2.0 * network_attempts)
                    continue

            if outcome.status == DOWNLOAD_TOO_EARLY:
                # Reset network streak — this isn't a network error.
                network_attempts = 0
                now = time.monotonic()
                if wait_started_at is None:
                    wait_started_at = now
                elapsed = now - wait_started_at
                if elapsed >= STREAM_CHUNK_WAIT_BUDGET_S:
                    log.warning(
                        "transfer.chunk.too_early.budget_exhausted "
                        "transfer_id=%s chunk_index=%d elapsed=%.0fs",
                        transfer_id[:12], index, elapsed,
                    )
                    return ("failed", f"chunk_{index}_upstream_too_slow")
                # Server's hint in seconds; clamp to our ramp so a
                # runaway hint can't pin us to a multi-minute sleep.
                server_hint_s = (outcome.retry_after_ms or 1000) / 1000.0
                our_cap_s = STREAM_CHUNK_WAIT_RAMP_S[
                    min(wait_ramp_idx, len(STREAM_CHUNK_WAIT_RAMP_S) - 1)
                ]
                wait_s = min(max(server_hint_s, 0.5), our_cap_s)
                wait_ramp_idx = min(wait_ramp_idx + 1,
                                    len(STREAM_CHUNK_WAIT_RAMP_S) - 1)
                log.debug(
                    "transfer.chunk.too_early transfer_id=%s chunk_index=%d "
                    "wait=%.1fs server_hint=%.1fs",
                    transfer_id[:12], index, wait_s, server_hint_s,
                )
                time.sleep(wait_s)
                continue

            if outcome.status == DOWNLOAD_ABORTED:
                return ("aborted", outcome.abort_reason)

            # network_error / not_found / auth_error / failed / any
            # non-200-non-425-non-410 response.
            network_attempts += 1
            wait_started_at = None
            wait_ramp_idx = 0
            if network_attempts >= STREAM_CHUNK_NETWORK_ATTEMPTS:
                log.error(
                    "transfer.chunk.failed transfer_id=%s chunk_index=%d "
                    "attempts=%d final_status=%s",
                    transfer_id[:12], index, network_attempts, outcome.status,
                )
                return ("failed",
                        f"chunk_{index}_{outcome.status}")
            backoff = 2.0 * network_attempts
            log.warning(
                "transfer.chunk.failed transfer_id=%s chunk_index=%d "
                "attempt=%d/%d status=%s retry_in=%.1fs",
                transfer_id[:12], index, network_attempts,
                STREAM_CHUNK_NETWORK_ATTEMPTS, outcome.status, backoff,
            )
            time.sleep(backoff)
