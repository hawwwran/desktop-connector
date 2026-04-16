"""
Server poller: checks for pending transfers, downloads, decrypts, and saves.
"""

import base64
import logging
import threading
import time

import requests as _raw_requests

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
        self._poll_status_file = config.config_dir / "poll_status.json"

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

    def run(self) -> None:
        """Main polling loop. Uses long polling after connection is confirmed."""
        log.info("Poller started")
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
                        log.info("Long poll retry requested")
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
                        log.info("Long poll %s", "available" if long_poll_available else "not available")

                    if long_poll_available:
                        notified = self._long_poll(last_check_time)
                        last_check_time = int(time.time())
                        if notified is True:
                            self._poll_once()
                        elif notified is False:
                            self._check_delivery_status()
                        else:
                            # Long poll broke mid-session
                            long_poll_available = False
                            self._write_poll_status("unavailable")
                            self._poll_once()
                            self._sleep(self._current_interval())
                    else:
                        self._poll_once()
                        self._sleep(self._current_interval())
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

    def _long_poll(self, since: int) -> bool | None:
        """
        Long poll the server. Returns:
        - True: new data available
        - False: timed out, nothing new
        - None: endpoint not available or error (fall back to regular polling)
        Uses raw requests — does NOT affect connection state machine.
        """
        url = f"{self.conn.server_url}/api/transfers/notify?since={since}"
        try:
            resp = _raw_requests.get(url, headers=self.conn.auth_headers(), timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("pending", False) or data.get("delivered", False)
            return None
        except _raw_requests.RequestException:
            return None

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

        # Decrypt metadata early to get filename for progress display
        meta_json = self.crypto.decrypt_metadata(encrypted_meta, symmetric_key)
        if meta_json is None:
            log.error("Failed to decrypt metadata for %s", transfer_id[:12])
            return
        filename = meta_json["filename"]
        is_fn = filename.startswith(".fn.")

        log.info("Downloading transfer %s from %s (%d chunks): %s",
                 transfer_id[:12], sender_id[:12], chunk_count, filename)

        # Insert into history before downloading (skip .fn. system transfers)
        if not is_fn:
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
            )

        # Download all chunks with progress updates
        encrypted_chunks = []
        for i in range(chunk_count):
            log.info("Downloading chunk %d/%d", i + 1, chunk_count)
            chunk_data = self.api.download_chunk(transfer_id, i)
            if chunk_data is None:
                log.error("Failed to download chunk %d of transfer %s", i, transfer_id[:12])
                if not is_fn:
                    self.history.update(transfer_id, status="failed")
                return
            encrypted_chunks.append(chunk_data)
            if not is_fn:
                self.history.update(transfer_id, chunks_downloaded=i + 1)

        # Decrypt and save
        try:
            save_path = self.crypto.decrypt_chunks_to_file(
                encrypted_meta, encrypted_chunks, symmetric_key, self.config.save_directory
            )
            log.info("Saved: %s", save_path)
        except Exception:
            log.exception("Failed to decrypt transfer %s", transfer_id[:12])
            if not is_fn:
                self.history.update(transfer_id, status="failed")
            return

        # Check for special .fn. transfers
        if is_fn:
            self._handle_fn_transfer(save_path)

        # Acknowledge
        if self.api.ack_transfer(transfer_id):
            log.info("Transfer %s acknowledged", transfer_id[:12])
        else:
            log.warning("Failed to acknowledge transfer %s", transfer_id[:12])

        # Finalize history
        if is_fn:
            pass  # .fn. transfers are logged in _handle_fn_transfer
        else:
            self.history.update(
                transfer_id,
                status="complete",
                size=save_path.stat().st_size,
                content_path=str(save_path),
                delivered=True,
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
