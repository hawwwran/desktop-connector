"""Fasttrack: lightweight encrypted message relay (128 KB ceiling).

For commands too small for the full transfer pipeline. Server is
function-agnostic — payload is opaque ciphertext. Used by Find my
Device, .fn.* command sync, etc.
"""

import logging

log = logging.getLogger(__name__)


class FasttrackMixin:
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
