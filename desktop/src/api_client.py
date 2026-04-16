"""
API client for communicating with the PHP relay server.
"""

import base64
import logging
import uuid
from pathlib import Path

import requests

from .connection import ConnectionManager
from .crypto import KeyManager

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
                  filename_override: str | None = None) -> str | None:
        """
        Encrypt and upload a file to a recipient.
        filename_override: use this name in metadata instead of the actual file name.
        Returns transfer_id on success, None on failure.
        """
        display = filename_override or filepath.name
        log.info("Sending file: %s (%d bytes)", display, filepath.stat().st_size)

        encrypted_meta, base_nonce, chunks = self.crypto.encrypt_file_to_chunks(
            filepath, symmetric_key, filename_override=filename_override)
        transfer_id = str(uuid.uuid4())

        if not self.init_transfer(transfer_id, recipient_id, encrypted_meta, len(chunks)):
            log.error("Failed to init transfer")
            return None

        for i, chunk_data in enumerate(chunks):
            log.info("Uploading chunk %d/%d", i + 1, len(chunks))
            result = self.upload_chunk(transfer_id, i, chunk_data)
            if result is None:
                log.error("Failed to upload chunk %d", i)
                return None

        log.info("File sent successfully: %s (transfer_id=%s)", filepath.name, transfer_id)
        return transfer_id

    def get_stats(self, paired_with: str | None = None) -> dict | None:
        """Get connection statistics from the server."""
        path = "/api/devices/stats"
        if paired_with:
            path += f"?paired_with={paired_with}"
        resp = self.conn.request("GET", path)
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def get_sent_status(self) -> list[dict]:
        """Get delivery status of transfers sent by this device."""
        resp = self.conn.request("GET", "/api/transfers/sent-status")
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
        log.info("Fasttrack send to %s (%d bytes)", recipient_id[:12], len(encrypted_data))
        resp = self.conn.request("POST", "/api/fasttrack/send", json={
            "recipient_id": recipient_id,
            "encrypted_data": encrypted_data,
        })
        if resp and resp.status_code == 201:
            msg_id = resp.json().get("message_id")
            log.info("Fasttrack sent, message_id=%s", msg_id)
            return msg_id
        log.error("Fasttrack send failed: %s", resp.status_code if resp else "no response")
        return None

    def fasttrack_pending(self) -> list[dict]:
        """Fetch pending fasttrack messages for this device."""
        resp = self.conn.request("GET", "/api/fasttrack/pending")
        if resp and resp.status_code == 200:
            msgs = resp.json().get("messages", [])
            if msgs:
                log.info("Fasttrack pending: %d message(s)", len(msgs))
            return msgs
        return []

    def fasttrack_ack(self, message_id: int) -> bool:
        """Acknowledge and delete a fasttrack message."""
        resp = self.conn.request("POST", f"/api/fasttrack/{message_id}/ack")
        ok = resp is not None and resp.status_code == 200
        log.debug("Fasttrack ack %d: %s", message_id, "ok" if ok else "failed")
        return ok
