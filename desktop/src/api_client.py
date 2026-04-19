"""
API client for communicating with the PHP relay server.
"""

import base64
import logging
import math
import time
import uuid
from pathlib import Path

import requests

from .connection import ConnectionManager
from .crypto import KeyManager, CHUNK_SIZE


CHUNK_RETRY_DELAY_S = 5.0
CHUNK_MAX_FAILURE_WINDOW_S = 120.0

log = logging.getLogger(__name__)


class ApiClient:
    """High-level API client wrapping ConnectionManager for server operations."""

    def __init__(self, connection: ConnectionManager, crypto: KeyManager):
        self.conn = connection
        self.crypto = crypto

    def register(self, server_url: str, device_type: str = "desktop") -> dict | None:
        """Register this device with the server. Returns {device_id, auth_token} or None."""
        try:
            resp = requests.post(
                f"{server_url}/api/devices/register",
                json={
                    "public_key": self.crypto.get_public_key_b64(),
                    "device_type": device_type,
                },
                timeout=10,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            log.error("Registration failed: %d %s", resp.status_code, resp.text)
        except requests.RequestException as e:
            log.error("Registration request failed: %s", e)
        return None

    def send_pairing_request(self, desktop_id: str, phone_pubkey: str) -> bool:
        """Send a pairing request (phone → desktop)."""
        resp = self.conn.request("POST", "/api/pairing/request", json={
            "desktop_id": desktop_id,
            "phone_pubkey": phone_pubkey,
        })
        return resp is not None and resp.status_code in (200, 201)

    def poll_pairing(self) -> list[dict]:
        """Poll for incoming pairing requests. Returns list of {id, phone_id, phone_pubkey}."""
        resp = self.conn.request("GET", "/api/pairing/poll")
        if resp and resp.status_code == 200:
            return resp.json().get("requests", [])
        return []

    def confirm_pairing(self, phone_id: str) -> bool:
        resp = self.conn.request("POST", "/api/pairing/confirm", json={"phone_id": phone_id})
        return resp is not None and resp.status_code == 200

    def init_transfer(self, transfer_id: str, recipient_id: str,
                      encrypted_meta: str, chunk_count: int) -> bool:
        """Initialize a transfer on the server."""
        resp = self.conn.request("POST", "/api/transfers/init", json={
            "transfer_id": transfer_id,
            "recipient_id": recipient_id,
            "encrypted_meta": encrypted_meta,
            "chunk_count": chunk_count,
        })
        return resp is not None and resp.status_code == 201

    def upload_chunk(self, transfer_id: str, chunk_index: int, data: bytes) -> dict | None:
        """Upload an encrypted chunk. Returns {chunks_received, complete} or None."""
        resp = self.conn.request(
            "POST",
            f"/api/transfers/{transfer_id}/chunks/{chunk_index}",
            data=data,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Device-ID": self.conn.device_id,
                "Authorization": f"Bearer {self.conn.auth_token}",
            },
        )
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def get_pending_transfers(self) -> list[dict]:
        """Get list of pending transfers for this device."""
        resp = self.conn.request("GET", "/api/transfers/pending")
        if resp and resp.status_code == 200:
            return resp.json().get("transfers", [])
        return []

    def download_chunk(self, transfer_id: str, chunk_index: int) -> bytes | None:
        """Download an encrypted chunk. Returns raw bytes or None."""
        resp = self.conn.request("GET", f"/api/transfers/{transfer_id}/chunks/{chunk_index}")
        if resp and resp.status_code == 200:
            return resp.content
        return None

    def ack_transfer(self, transfer_id: str) -> bool:
        """Acknowledge transfer receipt. Server will delete blobs."""
        resp = self.conn.request("POST", f"/api/transfers/{transfer_id}/ack")
        return resp is not None and resp.status_code == 200

    def send_file(self, filepath: Path, recipient_id: str, symmetric_key: bytes,
                  filename_override: str | None = None,
                  on_progress: callable = None) -> str | None:
        """
        Encrypt and upload a file to a recipient using a streaming pipeline:
        read one chunk → encrypt → upload → repeat. Memory is bounded by
        CHUNK_SIZE regardless of file size.

        Per-chunk retry: on failure, retry every 5 s. If the same chunk
        keeps failing for 120 s continuously, the transfer is aborted and
        send_file returns None. The retry timer resets on each success.

        filename_override: use this name in metadata instead of the actual file name.
        on_progress: callback(transfer_id, chunks_uploaded, total_chunks) called per chunk.
        Returns transfer_id on success, None on failure.
        """
        display = filename_override or filepath.name
        file_size = filepath.stat().st_size
        log.info("transfer.upload.started name=%s bytes=%d recipient=%s",
                 display, file_size, recipient_id[:12])

        chunk_count = max(1, math.ceil(file_size / CHUNK_SIZE))
        base_nonce = KeyManager.generate_base_nonce()
        encrypted_meta = KeyManager.build_encrypted_metadata(
            filename=display,
            mime_type=KeyManager.guess_mime(display),
            size=file_size,
            chunk_count=chunk_count,
            base_nonce=base_nonce,
            key=symmetric_key,
        )
        transfer_id = str(uuid.uuid4())

        if not self.init_transfer(transfer_id, recipient_id, encrypted_meta, chunk_count):
            log.error("transfer.init.failed transfer_id=%s", transfer_id[:12])
            return None
        log.info("transfer.init.accepted transfer_id=%s recipient=%s chunks=%d",
                 transfer_id[:12], recipient_id[:12], chunk_count)

        if on_progress:
            on_progress(transfer_id, 0, chunk_count)

        try:
            with open(filepath, "rb") as f:
                for index in range(chunk_count):
                    plaintext = f.read(CHUNK_SIZE)  # last chunk may be short; empty file → b""
                    encrypted = KeyManager.encrypt_chunk(
                        plaintext, base_nonce, index, symmetric_key)
                    err = self._upload_chunk_with_retry(
                        transfer_id, index, chunk_count, encrypted)
                    if err is not None:
                        log.error("transfer.upload.failed transfer_id=%s reason=%s",
                                  transfer_id[:12], err)
                        return None
                    if on_progress:
                        on_progress(transfer_id, index + 1, chunk_count)
        except OSError as e:
            log.error("transfer.upload.failed transfer_id=%s error_kind=%s",
                      transfer_id[:12], type(e).__name__)
            return None

        log.info("transfer.upload.completed transfer_id=%s name=%s",
                 transfer_id[:12], display)
        return transfer_id

    def _upload_chunk_with_retry(self, transfer_id: str, index: int,
                                  chunk_count: int, encrypted: bytes) -> str | None:
        """Upload one chunk with 5 s retry cadence. Returns None on success,
        or an error string if the same chunk has been failing continuously
        for longer than CHUNK_MAX_FAILURE_WINDOW_S."""
        # Connection errors surface as upload_chunk returning None (handled
        # by ConnectionManager.request). The narrow except below covers
        # the residual transient set: socket-level OSError that escapes
        # requests, JSON parse failures from a malformed 200 response.
        first_failure_at: float | None = None
        while True:
            try:
                if self.upload_chunk(transfer_id, index, encrypted) is not None:
                    log.debug("transfer.chunk.uploaded transfer_id=%s chunk_index=%d/%d",
                              transfer_id[:12], index + 1, chunk_count)
                    return None
            except (requests.RequestException, OSError, ValueError) as e:
                log.warning("transfer.chunk.failed transfer_id=%s chunk_index=%d error_kind=%s",
                            transfer_id[:12], index, type(e).__name__)
            now = time.monotonic()
            if first_failure_at is None:
                first_failure_at = now
                log.warning("transfer.chunk.failed transfer_id=%s chunk_index=%d/%d reason=retry_in_%ds",
                            transfer_id[:12], index + 1, chunk_count, int(CHUNK_RETRY_DELAY_S))
            elif now - first_failure_at >= CHUNK_MAX_FAILURE_WINDOW_S:
                return (f"Chunk {index + 1}/{chunk_count} failed continuously "
                        f"for {int(CHUNK_MAX_FAILURE_WINDOW_S)}s")
            time.sleep(CHUNK_RETRY_DELAY_S)

    def get_stats(self, paired_with: str | None = None) -> dict | None:
        """Get connection statistics from the server."""
        path = "/api/devices/stats"
        if paired_with:
            path += f"?paired_with={paired_with}"
        resp = self.conn.request("GET", path)
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def ping_device(self, recipient_id: str, timeout: float = 8.0) -> dict | None:
        """Probe paired device liveness. Server sends HIGH FCM and waits up to 5s
        for pong. Returns {online, last_seen_at, rtt_ms, via} or None on failure."""
        resp = self.conn.request(
            "POST", "/api/devices/ping",
            json={"recipient_id": recipient_id},
            timeout=timeout,
        )
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def get_sent_status(self, timeout: float = 30) -> list[dict]:
        """Get delivery status of transfers sent by this device."""
        resp = self.conn.request("GET", "/api/transfers/sent-status", timeout=timeout)
        if resp and resp.status_code == 200:
            return resp.json().get("transfers", [])
        return []

    # --- Fasttrack: lightweight encrypted message relay ---

    def check_fcm_available(self) -> bool:
        """Check if the server has FCM configured. Uses unauthenticated endpoint."""
        try:
            resp = requests.get(
                f"{self.conn.server_url}/api/fcm/config",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("available", False)
        except requests.RequestException:
            pass
        return False

    def fasttrack_send(self, recipient_id: str, encrypted_data: str) -> int | None:
        """Send an encrypted fasttrack message. Returns message_id or None."""
        log.info("fasttrack.message.send_started recipient=%s size=%d",
                 recipient_id[:12], len(encrypted_data))
        resp = self.conn.request("POST", "/api/fasttrack/send", json={
            "recipient_id": recipient_id,
            "encrypted_data": encrypted_data,
        })
        if resp and resp.status_code == 201:
            msg_id = resp.json().get("message_id")
            log.info("fasttrack.message.send_succeeded message_id=%s", msg_id)
            return msg_id
        log.error("fasttrack.message.send_failed status=%s",
                  resp.status_code if resp else "no_response")
        return None

    def fasttrack_pending(self) -> list[dict]:
        """Fetch pending fasttrack messages for this device."""
        resp = self.conn.request("GET", "/api/fasttrack/pending")
        if resp and resp.status_code == 200:
            msgs = resp.json().get("messages", [])
            if msgs:
                log.debug("fasttrack.message.pending_listed count=%d", len(msgs))
            return msgs
        return []

    def fasttrack_ack(self, message_id: int) -> bool:
        """Acknowledge and delete a fasttrack message."""
        resp = self.conn.request("POST", f"/api/fasttrack/{message_id}/ack")
        ok = resp is not None and resp.status_code == 200
        log.debug("fasttrack.message.acked message_id=%d outcome=%s",
                  message_id, "succeeded" if ok else "failed")
        return ok
