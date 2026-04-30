"""
Server poller: checks for pending transfers, downloads, decrypts, and saves.
"""

import base64
import errno
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import requests as _raw_requests
from cryptography.exceptions import InvalidTag

from .api_client import (
    ApiClient,
    DOWNLOAD_ABORTED,
    DOWNLOAD_OK,
    DOWNLOAD_TOO_EARLY,
)
from .config import Config, FAST_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, FAST_POLL_DURATION
from .connection import ConnectionManager, ConnectionState
from .crypto import KeyManager
from .history import TransferHistory, TransferStatus
from .messaging import FnTransferAdapter, MessageDispatcher, MessageType
from .platform import DesktopPlatform
from .receive_actions import (
    RECEIVE_KIND_OTHER,
    ReceiveActionBatch,
    ReceiveActionFloodSummary,
    ReceiveActionLimiter,
    apply_receive_action,
    apply_receive_text_actions,
    classify_received_file,
    extract_received_urls,
)

log = logging.getLogger(__name__)

# Delivery tracker: after this many seconds with no chunks_downloaded advancement,
# stop fast-polling a given transfer. Transfer stays as sent/undelivered; long-poll
# inline sent_status and app-restart delivery check still catch eventual delivery.
DELIVERY_STALL_TIMEOUT = 2 * 60

# Orphan partials (.incoming_*.part) older than this are swept on poller start.
# Matches Android's STALE_PART_TTL_MS.
STALE_PART_TTL_S = 24 * 60 * 60

# Per-chunk download retry policy (kept simple and local; mirrors current behavior).
CHUNK_DOWNLOAD_ATTEMPTS = 3

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


def _clipboard_image_extension(mime_type: str | None, data: bytes) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    by_mime = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/svg+xml": ".svg",
    }
    if normalized in by_mime:
        return by_mime[normalized]

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"

    return ".png"


def _clipboard_image_filename(mime_type: str | None, data: bytes) -> str:
    return f"clipboard-image{_clipboard_image_extension(mime_type, data)}"


class Poller:
    """Polls server for pending transfers and downloads them."""

    def __init__(self, config: Config, connection: ConnectionManager,
                 api: ApiClient, crypto: KeyManager, history: TransferHistory,
                 platform: DesktopPlatform):
        self.config = config
        self.conn = connection
        self.api = api
        self.crypto = crypto
        self.history = history
        self.platform = platform
        self._message_dispatcher = MessageDispatcher()
        self._message_dispatcher.register(MessageType.CLIPBOARD_TEXT, self._handle_message_clipboard_text)
        self._message_dispatcher.register(MessageType.CLIPBOARD_IMAGE, self._handle_message_clipboard_image)
        self._message_dispatcher.register(MessageType.PAIRING_UNPAIR, self._handle_message_unpair)
        self._running = True
        self._wake_event = threading.Event()
        self._poll_interval = DEFAULT_POLL_INTERVAL
        self._fast_poll_until = 0.0
        self._on_file_received: list = []
        self._poll_status_file = config.config_dir / "poll_status.json"
        self._last_delivery_check = 0.0  # timestamp of last delivery status check
        # Delivery tracker state (protected by _tracker_state_lock)
        self._tracker_state_lock = threading.Lock()
        self._tracker_last_progress: dict[str, tuple[int, float]] = {}  # tid -> (chunks_downloaded, monotonic_ts)
        self._tracker_gave_up: set[str] = set()
        self._receive_action_limiter = ReceiveActionLimiter(config)

    def _write_poll_status(self, status: str) -> None:
        """Write long poll status: 'active', 'unavailable', 'testing', 'offline'."""
        try:
            import json
            self._poll_status_file.write_text(json.dumps({"long_poll": status}))
        except Exception:
            pass

    def _test_long_poll(self) -> bool:
        """Quick test if /notify endpoint exists. Returns True if available."""
        try:
            resp = _raw_requests.get(
                f"{self.conn.server_url}/api/transfers/notify?test=1",
                headers=self.conn.auth_headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def retry_long_poll(self) -> None:
        """Reset long poll state to re-test on next cycle."""
        self._write_poll_status("testing")
        self._wake_event.set()

    def on_file_received(self, callback) -> None:
        """Register callback: callback(filepath)."""
        self._on_file_received.append(callback)

    def stop(self) -> None:
        self._running = False
        self._wake_event.set()

    def wake(self) -> None:
        """Wake the poller to check immediately."""
        self._wake_event.set()

    def has_live_outgoing(self) -> bool:
        """True iff any sent transfer is still flowing — not yet delivered and
        not given up on by the stall safeguard. Covers the full outgoing arc
        (uploading → delivering → delivered)."""
        try:
            undelivered = set(self.history.get_undelivered_transfer_ids())
        except Exception:
            return False
        if not undelivered:
            return False
        with self._tracker_state_lock:
            return bool(undelivered - self._tracker_gave_up)

    def run(self) -> None:
        """Main polling loop. Uses long polling after connection is confirmed."""
        log.info("Poller started")
        try:
            self._sweep_stale_parts(self.config.save_directory)
        except OSError:
            log.warning("Could not sweep stale parts on startup", exc_info=True)
        threading.Thread(target=self._delivery_tracker_loop, daemon=True).start()
        last_check_time = 0
        long_poll_available = None  # None = untested, True/False = tested
        self._write_poll_status("offline")
        while self._running:
            # Check for retry signal from settings
            try:
                if self._poll_status_file.exists():
                    import json
                    status = json.loads(self._poll_status_file.read_text())
                    if status.get("long_poll") == "testing":
                        log.info("poll.notify.retry_requested")
                        long_poll_available = None
            except Exception:
                pass

            if self.conn.state == ConnectionState.CONNECTED:
                try:
                    # Test long poll availability if untested
                    if long_poll_available is None:
                        self._write_poll_status("testing")
                        long_poll_available = self._test_long_poll()
                        self._write_poll_status("active" if long_poll_available else "unavailable")
                        if long_poll_available:
                            log.info("poll.notify.available")
                        else:
                            log.warning("poll.notify.unavailable")

                    # Skip long poll while outgoing transfers in progress (avoids blocking single-threaded PHP server)
                    upload_active = (self.config.config_dir / "upload_active.json").exists()
                    has_undelivered = bool(self.history.get_undelivered_transfer_ids())
                    busy_outgoing = upload_active or has_undelivered

                    if long_poll_available and not busy_outgoing:
                        result = self._long_poll(last_check_time)
                        last_check_time = int(time.time())
                        if isinstance(result, dict):
                            if result.get("pending"):
                                self._poll_once()
                            # Use inline sent_status if available (no second request)
                            if "sent_status" in result:
                                self._process_delivery_statuses(result["sent_status"])
                            elif result.get("delivered") or result.get("download_progress"):
                                self._check_delivery_status()
                        elif result is False:
                            self._check_delivery_status()
                        else:
                            # Long poll broke mid-session
                            long_poll_available = False
                            self._write_poll_status("unavailable")
                            self._poll_once()
                            self._sleep(self._current_interval())
                    else:
                        self._poll_once()
                        self._sleep(0.5 if busy_outgoing else self._current_interval())
                except Exception:
                    log.exception("Error during poll")
                    self._sleep(self._current_interval())
            else:
                long_poll_available = None
                self._write_poll_status("offline")
                self.conn.wait_for_retry()
                if self._running:
                    self.conn.check_connection()
        log.info("Poller stopped")

    def _long_poll(self, since: int) -> dict | bool | None:
        """
        Long poll the server. Returns:
        - dict: response data (something happened)
        - False: timed out, nothing new
        - None: endpoint not available or error (fall back to regular polling)
        Uses raw requests — does NOT affect connection state machine.
        """
        url = f"{self.conn.server_url}/api/transfers/notify?since={since}"
        try:
            resp = _raw_requests.get(url, headers=self.conn.auth_headers(), timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("pending") or data.get("delivered") or data.get("download_progress"):
                    return data
                return False
            return None
        except _raw_requests.RequestException:
            return None

    def _poll_once(self) -> None:
        # Delivery tracker (separate thread) owns sent-status polling during
        # active deliveries. This path only handles incoming.
        transfers = self.api.get_pending_transfers()
        if not transfers:
            return

        log.info("transfer.pending.found count=%d", len(transfers))
        self._fast_poll_until = time.time() + FAST_POLL_DURATION

        receive_action_batch = self._receive_action_limiter.start_batch(len(transfers))
        try:
            for transfer in transfers:
                if not self._running:
                    break
                self._download_transfer(
                    transfer,
                    receive_action_batch=receive_action_batch,
                )
        finally:
            summary = self._receive_action_limiter.finish_batch(receive_action_batch)
            self._notify_receive_action_flood_summary(summary)

    def _notify_receive_action_flood_summary(
        self,
        summary: ReceiveActionFloodSummary,
    ) -> None:
        if not summary.has_suppressed:
            return

        item_word = "item" if summary.batch_size == 1 else "items"
        action_word = "action" if summary.total_suppressed == 1 else "actions"
        body = (
            f"Received {summary.batch_size} {item_word}. "
            f"Skipped {summary.total_suppressed} automatic {action_word} "
            "to prevent flooding."
        )
        log.info(
            "receive_action.flood_limited batch_size=%d suppressed=%d",
            summary.batch_size,
            summary.total_suppressed,
        )
        try:
            self.platform.notifications.notify("Receive actions limited", body)
        except Exception:
            log.exception("receive_action.flood_notification.failed")

    def _download_transfer(self, transfer: dict, *,
                           receive_action_batch: ReceiveActionBatch | None = None) -> None:
        transfer_id = transfer["transfer_id"]
        sender_id = transfer["sender_id"]
        encrypted_meta = transfer["encrypted_meta"]
        chunk_count = transfer["chunk_count"]

        # Find the symmetric key for this sender
        paired = self.config.paired_devices.get(sender_id)
        if not paired:
            log.warning("Transfer from unknown device %s, skipping", sender_id)
            return

        symmetric_key = base64.b64decode(paired["symmetric_key_b64"])

        # Decrypt metadata early to get filename and base nonce
        meta_json = self.crypto.decrypt_metadata(encrypted_meta, symmetric_key)
        if meta_json is None:
            log.error("Failed to decrypt metadata for %s", transfer_id[:12])
            return
        filename = meta_json["filename"]
        mime_type = meta_json.get("mime_type", "application/octet-stream")
        base_nonce = base64.b64decode(meta_json["base_nonce"])

        log.info("transfer.download.started transfer_id=%s sender=%s chunks=%d name=%s",
                 transfer_id[:12], sender_id[:12], chunk_count, filename)
        self._mark_active_device(sender_id, reason="incoming")

        # Streaming is negotiated at init on the sender side; the
        # recipient sees the decision here via the `mode` field on the
        # pending-list row. Missing / unknown values default to classic
        # so an old server (no mode field) keeps working unchanged.
        # `.fn.*` transfers always take the classic path — see
        # docs/plans/streaming-improvement.md §9 non-goals.
        mode = transfer.get("mode", "classic")
        if filename.startswith(".fn."):
            self._receive_fn_transfer(
                transfer_id, sender_id, filename, chunk_count, symmetric_key,
                mime_type=mime_type,
                receive_action_batch=receive_action_batch)
        elif mode == "streaming":
            self._receive_streaming_transfer(
                transfer_id, sender_id, filename, chunk_count,
                base_nonce, symmetric_key,
                receive_action_batch=receive_action_batch)
        else:
            self._receive_file_transfer(
                transfer_id, sender_id, filename, chunk_count,
                base_nonce, symmetric_key,
                receive_action_batch=receive_action_batch)

    def _receive_fn_transfer(self, transfer_id: str, sender_id: str, filename: str,
                             chunk_count: int, symmetric_key: bytes, *,
                             mime_type: str = "application/octet-stream",
                             receive_action_batch: ReceiveActionBatch | None = None) -> None:
        """Handle command-style .fn.* transfers: tiny payloads, in-memory path,
        write to a tmp file under the save_dir, dispatch, ACK."""
        plaintext_parts: list[bytes] = []
        for i in range(chunk_count):
            # .fn transfers are always classic (streaming is forbidden for
            # them per the plan), so the only outcomes we care about are
            # "200 with bytes" vs "anything else → bail". The streaming
            # 425/410 paths are handled in the file-transfer branch.
            outcome = self.api.download_chunk(transfer_id, i)
            if outcome.status != DOWNLOAD_OK or outcome.data is None:
                log.error("Failed to download .fn chunk %d/%d of transfer %s status=%s",
                          i + 1, chunk_count, transfer_id[:12], outcome.status)
                return
            try:
                plaintext_parts.append(KeyManager.decrypt_chunk(outcome.data, symmetric_key))
            except Exception:
                log.exception("Failed to decrypt .fn chunk %d of transfer %s",
                              i, transfer_id[:12])
                return

        save_dir = Path(self.config.save_directory)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Cannot create save directory for .fn transfer")
            return

        # .fn payload is consumed and unlinked immediately by _handle_fn_transfer.
        save_path = save_dir / filename
        try:
            save_path.write_bytes(b"".join(plaintext_parts))
        except OSError:
            log.exception("Failed to write .fn payload for %s", transfer_id[:12])
            return

        self._handle_fn_transfer(
            save_path,
            sender_id=sender_id,
            transfer_id=transfer_id,
            mime_type=mime_type,
            receive_action_batch=receive_action_batch,
        )

        if self.api.ack_transfer(transfer_id):
            log.info("delivery.acked transfer_id=%s", transfer_id[:12])
        else:
            log.warning("delivery.acked transfer_id=%s reason=server_rejected",
                        transfer_id[:12])

    def _receive_file_transfer(self, transfer_id: str, sender_id: str,
                               filename: str, chunk_count: int,
                               base_nonce: bytes, symmetric_key: bytes, *,
                               receive_action_batch: ReceiveActionBatch | None = None) -> None:
        """Stream chunks to a temp file under {save_dir}/.parts/, then
        atomic-finalize to the destination via os.link (race-free) +
        unlink. Memory bounded by CHUNK_SIZE. ACK is sent only after the
        durable finalize; the file is kept on ACK failure (sender will
        eventually time out delivery)."""
        # History row first, with 0/N progress so the bar appears immediately.
        self.history.add(
            filename=filename,
            display_label=filename,
            direction="received",
            size=0,
            transfer_id=transfer_id,
            sender_id=sender_id,
            status="downloading",
            chunks_downloaded=0,
            chunks_total=chunk_count,
            peer_device_id=sender_id,
        )

        try:
            save_dir = self.config.save_directory  # property mkdirs the dir
            parts_dir = save_dir / ".parts"
            parts_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Cannot prepare save / .parts directories")
            self.history.update(transfer_id, status="failed",
                                chunks_downloaded=0, chunks_total=0)
            return

        temp_path = parts_dir / f".incoming_{transfer_id}.part"

        success = False
        try:
            # `wb` truncates any stale partial from a prior aborted attempt.
            with open(temp_path, "wb") as out:
                for i in range(chunk_count):
                    plaintext = self._download_and_decrypt_chunk(
                        transfer_id, i, chunk_count, symmetric_key)
                    if plaintext is None:
                        self.history.update(transfer_id, status="failed",
                                            chunks_downloaded=0, chunks_total=0)
                        return
                    out.write(plaintext)
                    self.history.update(transfer_id, chunks_downloaded=i + 1)
                out.flush()
                os.fsync(out.fileno())
            success = True
        except OSError:
            log.exception("Streaming write failed for %s", transfer_id[:12])
            self.history.update(transfer_id, status="failed",
                                chunks_downloaded=0, chunks_total=0)
            return
        finally:
            if not success:
                self._delete_quietly(temp_path)

        final_path = self._finalize_temp_to_unique(temp_path, save_dir, filename)
        if final_path is None:
            self.history.update(transfer_id, status="failed",
                                chunks_downloaded=0, chunks_total=0)
            return

        final_size = final_path.stat().st_size
        log.info("transfer.download.completed transfer_id=%s bytes=%d name=%s",
                 transfer_id[:12], final_size, final_path.name)

        # ACK-after-durable-write: the file is on disk under its final name.
        # If ACK fails the sender will eventually stop seeing "delivering",
        # but we MUST NOT delete a fully received file just because the
        # network hiccupped before we could tell the server.
        ack_ok = self.api.ack_transfer(transfer_id)
        if ack_ok:
            log.info("delivery.acked transfer_id=%s", transfer_id[:12])
        else:
            log.warning("delivery.acked transfer_id=%s reason=keeping_file_after_ack_failure",
                        transfer_id[:12])

        # Download logic cleans up its own progress fields on completion.
        self.history.update(
            transfer_id,
            status="complete",
            size=final_size,
            content_path=str(final_path),
            delivered=True,
            chunks_downloaded=0,
            chunks_total=0,
        )
        self._apply_receive_file_action(
            final_path,
            receive_action_batch=receive_action_batch,
        )
        for cb in self._on_file_received:
            try:
                cb(final_path)
            except Exception:
                log.exception("File received callback error")

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
        self._apply_receive_file_action(
            final_path,
            receive_action_batch=receive_action_batch,
        )
        for cb in self._on_file_received:
            try:
                cb(final_path)
            except Exception:
                log.exception("File received callback error")

    def _apply_receive_file_action(
        self,
        filepath: Path,
        *,
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> None:
        kind = classify_received_file(filepath)
        if kind == RECEIVE_KIND_OTHER:
            return
        if not apply_receive_action(
            self.config,
            self.platform,
            kind,
            path=filepath,
            limiter=self._receive_action_limiter,
            batch=receive_action_batch,
        ):
            log.warning(
                "receive_action.file.failed kind=%s",
                kind,
            )

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

    @classmethod
    def _finalize_temp_to_unique(cls, temp_path: Path, save_dir: Path,
                                 filename: str) -> Path | None:
        """Atomically link temp_path under save_dir using the first
        non-colliding name (filename, filename_1, ...), then unlink the
        temp source. os.link is atomic and FileExistsError-safe, so no
        TOCTOU race with another writer claiming the same name.

        Falls back to a probe + shutil.move on cross-FS or when the FS
        does not support hard links (FAT, exFAT). The fallback retains
        the small unique-name race, accepted as the degenerate case."""
        base = save_dir / filename
        stem = base.stem
        suffix = base.suffix
        counter = 0
        while True:
            candidate = base if counter == 0 else save_dir / f"{stem}_{counter}{suffix}"
            try:
                os.link(temp_path, candidate)
            except FileExistsError:
                counter += 1
                continue
            except OSError as e:
                if e.errno in (errno.EXDEV, errno.EPERM, errno.ENOSYS):
                    return cls._fallback_move_unique(temp_path, save_dir, filename)
                log.exception("os.link finalize failed for %s", temp_path)
                cls._delete_quietly(temp_path)
                return None
            cls._delete_quietly(temp_path)
            return candidate

    @classmethod
    def _fallback_move_unique(cls, temp_path: Path, save_dir: Path,
                              filename: str) -> Path | None:
        """Cross-FS finalize fallback: probe for a free name, then move.
        Small TOCTOU race accepted (cross-FS deployments are rare and
        single-user)."""
        base = save_dir / filename
        stem = base.stem
        suffix = base.suffix
        counter = 0
        while True:
            candidate = base if counter == 0 else save_dir / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                break
            counter += 1
        try:
            shutil.move(str(temp_path), str(candidate))
            return candidate
        except OSError:
            log.exception("Cross-FS finalize failed for %s", temp_path)
            cls._delete_quietly(temp_path)
            return None

    @staticmethod
    def _delete_quietly(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Failed to delete %s", path, exc_info=True)

    def _download_and_decrypt_chunk(self, transfer_id: str, index: int,
                                     chunk_count: int, symmetric_key: bytes) -> bytes | None:
        """Download + decrypt one chunk. Retries the download on either a
        missing body or an AES-GCM auth failure (InvalidTag). The
        latter defends against server-side races where a concurrent upload
        causes the reader to see partial bytes — the server's atomic rename
        is the primary fix; this is belt-and-suspenders. Returns plaintext
        bytes or None if the chunk cannot be recovered after 3 attempts.

        Classic-mode helper. The streaming receive loop (C.3) handles
        425 / 410 directly and does not route through here.
        """
        for attempt in range(1, CHUNK_DOWNLOAD_ATTEMPTS + 1):
            outcome = self.api.download_chunk(transfer_id, index)
            if outcome.status != DOWNLOAD_OK or outcome.data is None:
                log.warning("Chunk %d/%d download returned no body (attempt %d/%d) status=%s",
                            index + 1, chunk_count, attempt, CHUNK_DOWNLOAD_ATTEMPTS,
                            outcome.status)
            else:
                try:
                    return KeyManager.decrypt_chunk(outcome.data, symmetric_key)
                except InvalidTag:
                    log.warning("Chunk %d/%d decrypt failed (attempt %d/%d), "
                                "re-downloading", index + 1, chunk_count,
                                attempt, CHUNK_DOWNLOAD_ATTEMPTS)
            if attempt < CHUNK_DOWNLOAD_ATTEMPTS:
                time.sleep(2.0 * attempt)
        log.error("Chunk %d/%d failed after %d attempts on %s",
                  index + 1, chunk_count, CHUNK_DOWNLOAD_ATTEMPTS,
                  transfer_id[:12])
        return None

    def _sweep_stale_parts(self, save_dir: Path) -> None:
        """Delete orphaned .incoming_*.part files left behind by aborted
        receives (force-quit, OOM, power loss). Runs once on poller start."""
        parts_dir = save_dir / ".parts"
        if not parts_dir.is_dir():
            return
        cutoff = time.time() - STALE_PART_TTL_S
        removed = 0
        try:
            for entry in parts_dir.iterdir():
                if not entry.name.startswith(".incoming_"):
                    continue
                if not entry.name.endswith(".part"):
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            log.warning("Parts sweep failed", exc_info=True)
            return
        if removed:
            log.info("Cleaned up %d stale .part file(s)", removed)

    def _handle_fn_transfer(
        self,
        filepath: Path,
        *,
        sender_id: str | None = None,
        transfer_id: str = "",
        mime_type: str = "application/octet-stream",
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> None:
        """Handle special .fn. transfers through the unified message dispatcher."""
        name = filepath.name
        try:
            message = FnTransferAdapter.to_device_message(name, filepath.read_bytes(), sender_id=sender_id)
            if message is None:
                log.warning("fasttrack.command.unknown fn=%s", name)
                return
            if sender_id:
                self._mark_active_device(sender_id, reason="incoming")

            if message.type == MessageType.CLIPBOARD_TEXT:
                self._handle_message_clipboard_text(
                    message,
                    receive_action_batch=receive_action_batch,
                )
                return

            if message.type == MessageType.CLIPBOARD_IMAGE:
                self._handle_message_clipboard_image(
                    message,
                    source_path=filepath,
                    sender_id=sender_id,
                    transfer_id=transfer_id,
                    mime_type=mime_type,
                    receive_action_batch=receive_action_batch,
                )
                return

            if not self._message_dispatcher.dispatch(message):
                log.warning("fasttrack.command.unknown type=%s", message.type.value)
        except Exception as e:
            log.error("command.dispatch.failed filename=%s error_kind=%s", name, type(e).__name__)
        finally:
            filepath.unlink(missing_ok=True)

    def _handle_message_clipboard_text(
        self,
        message,
        *,
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> None:
        text = str(message.payload.get("text", ""))

        urls = extract_received_urls(text)
        preview = text if len(urls) == 1 else (text[:60] + "..." if len(text) > 60 else text)
        self.platform.notifications.notify("Clipboard received", preview[:60])
        self.history.add(filename=message.metadata.get("filename", ".fn.clipboard.text"),
                         display_label=preview, direction="received", size=len(text),
                         sender_id=message.sender_id or "",
                         peer_device_id=message.sender_id or "")
        if not apply_receive_text_actions(
            self.config,
            self.platform,
            text,
            limiter=self._receive_action_limiter,
            batch=receive_action_batch,
        ):
            log.warning("receive_action.text.failed length=%d", len(text))

    def _handle_message_clipboard_image(
        self,
        message,
        *,
        source_path: Path | None = None,
        sender_id: str | None = None,
        transfer_id: str = "",
        mime_type: str = "application/octet-stream",
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> None:
        data = message.payload.get("image_bytes", b"")
        if not isinstance(data, (bytes, bytearray)):
            log.warning("clipboard.image.save_failed reason=invalid_payload")
            return

        image_bytes = bytes(data)
        save_dir = Path(self.config.save_directory)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Cannot create save directory for clipboard image")
            return

        temp_path = source_path
        if temp_path is None:
            parts_dir = save_dir / ".parts"
            try:
                parts_dir.mkdir(parents=True, exist_ok=True)
                temp_path = parts_dir / f".incoming_clipboard_image_{time.monotonic_ns()}.part"
                temp_path.write_bytes(image_bytes)
            except OSError:
                log.exception("Failed to stage clipboard image")
                return

        filename = _clipboard_image_filename(mime_type, image_bytes)
        final_path = self._finalize_temp_to_unique(temp_path, save_dir, filename)
        if final_path is None:
            log.warning("clipboard.image.save_failed")
            return

        final_size = final_path.stat().st_size
        log.info("clipboard.image.saved bytes=%d name=%s", final_size, final_path.name)
        self.history.add(
            filename=final_path.name,
            display_label=final_path.name,
            direction="received",
            size=final_size,
            content_path=str(final_path),
            sender_id=sender_id or "",
            peer_device_id=sender_id or message.sender_id or "",
            transfer_id=transfer_id,
        )
        self._apply_receive_file_action(
            final_path,
            receive_action_batch=receive_action_batch,
        )
        for cb in self._on_file_received:
            try:
                cb(final_path)
            except Exception:
                log.exception("File received callback error")

    def _handle_message_unpair(self, _message) -> None:
        log.info("pairing.unpair.received")
        self.local_unpair(
            scope="pairing_only",
            notify_title="Unpaired",
            notify_body="Paired device disconnected",
        )

    def _mark_active_device(self, device_id: str, *, reason: str) -> None:
        try:
            self.config.active_device_id = device_id
            log.info(
                "device.active.changed peer=%s reason=%s",
                device_id[:8],
                reason,
            )
        except Exception:
            log.debug(
                "device.active.update_failed peer=%s reason=%s",
                device_id[:8],
                reason,
                exc_info=True,
            )

    def local_unpair(self, scope: str, *, notify_title: str | None = None,
                     notify_body: str | None = None) -> None:
        """
        Wipe local pairing (and optionally device credentials) and surface a
        notification. Shared by the .fn.unpair message handler and the
        AUTH_INVALID re-pair flow triggered from the tray.

        See Config.wipe_credentials() for scope semantics.
        """
        self.config.wipe_credentials(scope)
        if scope == "full":
            try:
                self.crypto.reset_keys()
            except Exception:
                log.exception("crypto.reset_keys failed")
        if notify_title:
            try:
                self.platform.notifications.notify(notify_title, notify_body or "")
            except Exception:
                log.exception("notification during local_unpair failed")

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

    def _current_interval(self) -> float:
        if time.time() < self._fast_poll_until:
            return FAST_POLL_INTERVAL
        return DEFAULT_POLL_INTERVAL

    def _sleep(self, seconds: float) -> None:
        self._wake_event.clear()
        self._wake_event.wait(timeout=seconds)
