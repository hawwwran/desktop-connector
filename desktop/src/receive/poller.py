"""Poller orchestrator: lifecycle, run loop, transfer-mode dispatch.

Owns the persistent state (locks, queues, running flag, history,
config, api, crypto, platform). Per-topic behaviour lives in mixins
under ``receive/`` — Poller composes them and adds the small bits
(``run``, ``_poll_once``, ``_download_transfer``) that glue the
pending-transfer dispatch together.
"""

import base64
import logging
import threading
import time
from pathlib import Path

from ..api_client import ApiClient
from ..config import (
    Config,
    DEFAULT_POLL_INTERVAL,
    FAST_POLL_DURATION,
    FAST_POLL_INTERVAL,
)
from ..connection import ConnectionManager, ConnectionState
from ..crypto import KeyManager
from ..find_device_responder import (
    FindDeviceAlert,
    FindDeviceResponder,
    InitialTickRunner,
    NoopAlert,
    threaded_initial_tick_runner,
)
from ..history import TransferHistory
from ..messaging import MessageDispatcher, MessageType
from ..platform import DesktopPlatform
from ..receive_actions import ReceiveActionLimiter
from .classic_download import ClassicDownloadMixin
from .delivery_tracker import DeliveryTrackerMixin
from .device_helpers import DeviceHelpersMixin
from .fasttrack import FasttrackMixin
from .finalize import FinalizeMixin
from .flood_summary import FloodSummaryMixin
from .fn_transfer import FnTransferMixin
from .long_poll import LongPollMixin
from .streaming_download import StreamingDownloadMixin

log = logging.getLogger(__name__)


class Poller(
    LongPollMixin,
    DeliveryTrackerMixin,
    ClassicDownloadMixin,
    StreamingDownloadMixin,
    FinalizeMixin,
    FnTransferMixin,
    FasttrackMixin,
    FloodSummaryMixin,
    DeviceHelpersMixin,
):
    """Polls server for pending transfers and downloads them."""

    def __init__(self, config: Config, connection: ConnectionManager,
                 api: ApiClient, crypto: KeyManager, history: TransferHistory,
                 platform: DesktopPlatform,
                 *,
                 find_device_alert: FindDeviceAlert | None = None,
                 initial_tick_runner: InitialTickRunner = threaded_initial_tick_runner):
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
        self._find_device_responder = FindDeviceResponder(
            alert=find_device_alert or NoopAlert(),
            send_update=self._send_find_device_update,
            device_name_lookup=self._lookup_device_name,
            location_provider=platform.location.get_current_fix,
            initial_tick_runner=initial_tick_runner,
        )
        self._message_dispatcher.register(
            MessageType.FIND_PHONE_START,
            self._find_device_responder.handle_message,
        )
        self._message_dispatcher.register(
            MessageType.FIND_PHONE_STOP,
            self._find_device_responder.handle_message,
        )
        self._running = True
        self._wake_event = threading.Event()
        self._fasttrack_wake_event = threading.Event()
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

    def on_file_received(self, callback) -> None:
        """Register callback: callback(filepath)."""
        self._on_file_received.append(callback)

    def stop(self) -> None:
        self._running = False
        self._wake_event.set()
        self._fasttrack_wake_event.set()

    def wake(self) -> None:
        """Wake both the main poll loop and the fasttrack consumer."""
        self._wake_event.set()
        self._fasttrack_wake_event.set()

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
        threading.Thread(target=self._fasttrack_consumer_loop, daemon=True).start()
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

    def _download_transfer(self, transfer: dict, *,
                           receive_action_batch=None) -> None:
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

    def local_unpair(self, scope: str, *, notify_title: str | None = None,
                     notify_body: str | None = None) -> None:
        """
        Wipe local pairing (and optionally device credentials) and surface a
        notification. Used by the AUTH_INVALID re-pair flow triggered from
        the tray; sender-scoped .fn.unpair messages remove only that peer.

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

    def _current_interval(self) -> float:
        if time.time() < self._fast_poll_until:
            return FAST_POLL_INTERVAL
        return DEFAULT_POLL_INTERVAL

    def _sleep(self, seconds: float) -> None:
        self._wake_event.clear()
        self._wake_event.wait(timeout=seconds)
