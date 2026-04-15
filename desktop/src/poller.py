"""
Server poller: checks for pending transfers, downloads, decrypts, and saves.
"""

import base64
import logging
import threading
import time

from .api_client import ApiClient
from .clipboard import write_clipboard_text, write_clipboard_image
from .config import Config, FAST_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, FAST_POLL_DURATION
from .connection import ConnectionManager, ConnectionState
from .crypto import KeyManager
from .history import TransferHistory

log = logging.getLogger(__name__)


class Poller:
    """Polls server for pending transfers and downloads them."""

    def __init__(self, config: Config, connection: ConnectionManager,
                 api: ApiClient, crypto: KeyManager, history: TransferHistory):
        self.config = config
        self.conn = connection
        self.api = api
        self.crypto = crypto
        self.history = history
        self._running = True
        self._wake_event = threading.Event()
        self._poll_interval = DEFAULT_POLL_INTERVAL
        self._fast_poll_until = 0.0
        self._on_file_received: list = []

    def on_file_received(self, callback) -> None:
        """Register callback: callback(filepath)."""
        self._on_file_received.append(callback)

    def stop(self) -> None:
        self._running = False
        self._wake_event.set()

    def wake(self) -> None:
        """Wake the poller to check immediately."""
        self._wake_event.set()

    def run(self) -> None:
        """Main polling loop. Runs in a background thread."""
        log.info("Poller started")
        while self._running:
            if self.conn.state == ConnectionState.CONNECTED:
                try:
                    self._poll_once()
                except Exception:
                    log.exception("Error during poll")
                self._sleep(self._current_interval())
            else:
                # Wait for backoff then try reconnecting
                self.conn.wait_for_retry()
                if self._running:
                    self.conn.check_connection()
        log.info("Poller stopped")

    def _poll_once(self) -> None:
        # Check delivery status of sent items
        self._check_delivery_status()

        transfers = self.api.get_pending_transfers()
        if not transfers:
            return

        log.info("Found %d pending transfer(s)", len(transfers))
        self._fast_poll_until = time.time() + FAST_POLL_DURATION

        for transfer in transfers:
            if not self._running:
                break
            self._download_transfer(transfer)

    def _download_transfer(self, transfer: dict) -> None:
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

        log.info("Downloading transfer %s from %s (%d chunks)",
                 transfer_id[:12], sender_id[:12], chunk_count)

        # Download all chunks
        encrypted_chunks = []
        for i in range(chunk_count):
            log.info("Downloading chunk %d/%d", i + 1, chunk_count)
            chunk_data = self.api.download_chunk(transfer_id, i)
            if chunk_data is None:
                log.error("Failed to download chunk %d of transfer %s", i, transfer_id[:12])
                return
            encrypted_chunks.append(chunk_data)

        # Decrypt and save
        try:
            save_path = self.crypto.decrypt_chunks_to_file(
                encrypted_meta, encrypted_chunks, symmetric_key, self.config.save_directory
            )
            log.info("Saved: %s", save_path)
        except Exception:
            log.exception("Failed to decrypt transfer %s", transfer_id[:12])
            return

        # Check for special .fn. transfers
        is_fn = save_path.name.startswith(".fn.")
        if is_fn:
            self._handle_fn_transfer(save_path)

        # Acknowledge
        if self.api.ack_transfer(transfer_id):
            log.info("Transfer %s acknowledged", transfer_id[:12])
        else:
            log.warning("Failed to acknowledge transfer %s", transfer_id[:12])

        # Log to history
        if is_fn:
            # .fn. transfers get a nice label from _handle_fn_transfer
            pass  # already logged in _handle_fn_transfer
        else:
            self.history.add(
                filename=save_path.name,
                display_label=save_path.name,
                direction="received",
                size=save_path.stat().st_size,
                content_path=str(save_path),
                sender_id=sender_id,
            )
            for cb in self._on_file_received:
                try:
                    cb(save_path)
                except Exception:
                    log.exception("File received callback error")

    def _handle_fn_transfer(self, filepath) -> None:
        """Handle special .fn. transfers (clipboard, etc.)."""
        name = filepath.name  # e.g. ".fn.clipboard.text"
        parts = name.split(".")  # ["", "fn", "clipboard", "text"]
        if len(parts) < 3:
            log.warning("Unknown .fn transfer: %s", name)
            return

        fn = parts[2]  # "clipboard"

        if fn == "clipboard":
            subtype = parts[3] if len(parts) > 3 else "text"
            try:
                if subtype == "text":
                    text = filepath.read_text(errors="replace")
                    if write_clipboard_text(text):
                        log.info("Clipboard text set (%d chars)", len(text))
                        from .notifications import notify
                        import re
                        urls = re.findall(r'https?://\S+', text)
                        if len(urls) == 1:
                            preview = text  # Keep full text for URL items
                        elif len(text) > 60:
                            preview = text[:60] + "..."
                        else:
                            preview = text
                        notify("Clipboard received", preview[:60])
                        self.history.add(filename=filepath.name, display_label=preview,
                                         direction="received", size=len(text))
                        # Auto-open link if enabled
                        if len(urls) == 1 and self.config.auto_open_links:
                            import subprocess
                            log.info("Auto-opening link: %s", urls[0])
                            subprocess.Popen(["xdg-open", urls[0]])
                    else:
                        log.warning("Failed to set clipboard text")
                elif subtype == "image":
                    data = filepath.read_bytes()
                    if write_clipboard_image(data):
                        log.info("Clipboard image set (%d bytes)", len(data))
                        from .notifications import notify
                        notify("Clipboard received", "Image copied to clipboard")
                        self.history.add(filename=filepath.name, display_label="Clipboard image",
                                         direction="received", size=len(data))
                    else:
                        log.warning("Failed to set clipboard image")
                else:
                    log.warning("Unknown clipboard subtype: %s", subtype)
            except Exception:
                log.exception("Error handling clipboard transfer")
            finally:
                filepath.unlink(missing_ok=True)
        elif fn == "unpair":
            log.info("Received unpair request from paired device")
            # Remove the sender from paired devices
            devices = self.config.paired_devices
            # Find and remove the device that sent this
            for did in list(devices.keys()):
                del devices[did]
            self.config._data["paired_devices"] = devices
            self.config.save()
            filepath.unlink(missing_ok=True)
            from .notifications import notify
            notify("Unpaired", "Paired device disconnected")
        else:
            log.warning("Unknown .fn function: %s", fn)

    def _check_delivery_status(self) -> None:
        """Check if any sent transfers have been delivered."""
        undelivered = self.history.get_undelivered_transfer_ids()
        if not undelivered:
            return

        statuses = self.api.get_sent_status()
        delivered_ids = {s["transfer_id"] for s in statuses if s.get("status") == "delivered"}

        for tid in undelivered:
            if tid in delivered_ids:
                if self.history.mark_delivered(tid):
                    log.info("Transfer %s delivered", tid[:12])

    def _current_interval(self) -> float:
        if time.time() < self._fast_poll_until:
            return FAST_POLL_INTERVAL
        return DEFAULT_POLL_INTERVAL

    def _sleep(self, seconds: float) -> None:
        self._wake_event.clear()
        self._wake_event.wait(timeout=seconds)
