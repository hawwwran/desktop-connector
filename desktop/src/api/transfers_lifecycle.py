"""Transfer-level operations: abort/cancel, list pending, sent-status."""

import logging

log = logging.getLogger(__name__)


class TransfersLifecycleMixin:
    def abort_transfer(self, transfer_id: str, reason: str | None = None) -> bool:
        """Either-party abort. Server wipes chunks + row and — for
        streaming transfers — notifies the other party via FCM so its
        long-poll / next chunk call returns 410.

        `reason` is one of:
          * 'sender_abort'     — caller is the sender, explicit cancel
          * 'sender_failed'    — caller is the sender, gave up after a
                                  retry budget (quota_timeout, network)
          * 'recipient_abort'  — caller is the recipient
          * None               — legacy sender-side cancel. Back-compat
                                  alias for 'sender_abort' but the
                                  server also accepts it without a body.

        Cross-role reasons (sender passing 'recipient_abort' or vice
        versa) are rejected server-side with 400. Returns True on any
        2xx. A 410 on a transfer the other side already aborted is
        still reported as False — the cleanup already happened and the
        caller doesn't need to retry.
        """
        body = {"reason": reason} if reason else None
        resp = self.conn.request(
            "DELETE",
            f"/api/transfers/{transfer_id}",
            json=body,
        )
        return resp is not None and 200 <= resp.status_code < 300

    def cancel_transfer(self, transfer_id: str) -> bool:
        """Back-compat alias for `abort_transfer(transfer_id, 'sender_abort')`.

        Preserved so older entry points (history window's cancel button,
        one-shot `--send` failure cleanup) keep working unchanged. New
        callers should use `abort_transfer` with an explicit reason so
        the opposite party sees the right UI label.
        """
        return self.abort_transfer(transfer_id, "sender_abort")

    def get_pending_transfers(self) -> list[dict]:
        """Get list of pending transfers for this device.

        Guards resp.json() — under load a shared-hosting server can
        return 200 OK with an empty/partial body (PHP dies mid-response).
        Falling through to `[]` is the right fallback: we'll try again
        on the next poll tick without crashing the poll loop."""
        resp = self.conn.request("GET", "/api/transfers/pending")
        if resp and resp.status_code == 200:
            try:
                return resp.json().get("transfers", [])
            except ValueError:  # requests' JSONDecodeError subclasses ValueError
                log.warning("transfer.pending.malformed body_length=%d",
                            len(resp.content or b""))
                return []
        return []

    def get_sent_status(self, timeout: float = 30, *,
                        track_state: bool = True) -> list[dict]:
        """Get delivery status of transfers sent by this device.

        ``track_state=False`` is for the 500 ms delivery-tracker loop where
        a timeout on a single 750 ms poll shouldn't be interpreted as
        "server is down" and shouldn't trigger the exponential backoff
        (which caused a CONNECTED⇄DISCONNECTED thrash under any slow
        network)."""
        resp = self.conn.request("GET", "/api/transfers/sent-status",
                                  timeout=timeout, track_state=track_state)
        if resp and resp.status_code == 200:
            return resp.json().get("transfers", [])
        return []
