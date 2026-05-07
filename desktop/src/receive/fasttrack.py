"""Fasttrack consumer loop + find-device update sender.

Drains the fasttrack pending queue, decrypts each message via the
sender's symmetric key, and dispatches via the unified message
dispatcher. Co-exists with the GTK find-device window's own polling
loop — they race for messages, but FIND_PHONE_LOCATION_UPDATE
responses are sender-side ownership and are explicitly left unacked
here so the active sender UI can consume them.

``_send_find_device_update`` is the FindDeviceResponder's outbound
hook (state + GPS coords for a paired sender) — wired in __init__.
"""

import base64
import logging
import time

from cryptography.exceptions import InvalidTag

from ..connection import ConnectionState
from ..find_device_responder import encode_state_payload
from ..messaging import FasttrackAdapter, MessageType

log = logging.getLogger(__name__)

# Fasttrack receiver loop cadence. Slower than the sender-side GTK
# find-device window's 3 s tick so the GTK window usually wins races
# during an active sender session; after the user closes the window,
# this drains the pending queue within FASTTRACK_POLL_INTERVAL.
FASTTRACK_POLL_INTERVAL_S = 8.0


class FasttrackMixin:
    def _send_find_device_update(
        self,
        recipient_id: str,
        state: str,
        *,
        lat: float | None = None,
        lng: float | None = None,
        accuracy: float | None = None,
    ) -> bool:
        symmetric_key = self._resolve_symmetric_key(recipient_id)
        if symmetric_key is None:
            log.warning(
                "findphone.update.skipped reason=no_symkey peer=%s",
                recipient_id[:12],
            )
            return False
        plaintext = encode_state_payload(
            state, lat=lat, lng=lng, accuracy=accuracy,
        )
        try:
            encrypted = self.crypto.encrypt_blob(plaintext, symmetric_key)
        except Exception:
            log.exception(
                "findphone.update.encrypt_failed peer=%s",
                recipient_id[:12],
            )
            return False
        encrypted_b64 = base64.b64encode(encrypted).decode()
        try:
            msg_id = self.api.fasttrack_send(recipient_id, encrypted_b64)
        except Exception:
            log.exception(
                "findphone.update.fasttrack_send_failed peer=%s",
                recipient_id[:12],
            )
            return False
        return msg_id is not None

    def _fasttrack_consumer_loop(self) -> None:
        """Drain the fasttrack pending queue, decrypt, and dispatch.

        Cadence: ``FASTTRACK_POLL_INTERVAL_S`` while connected, paused
        while disconnected. Per-message work runs in this loop's
        thread; handlers must not block on UI. Each receiver-side
        message is ACK'd regardless of dispatch outcome — leaving an
        undispatched command in the queue would cause it to expire
        (10 min) and flood logs every poll.

        Coexists with the GTK find-device sender window's own polling
        loop — they race for messages, but FIND_PHONE_LOCATION_UPDATE
        responses are sender-side ownership and must be left unacked
        here so the active sender UI can consume them.
        """
        log.info("findphone.consumer.started")
        while self._running:
            if self.conn.state != ConnectionState.CONNECTED:
                # Sleep on the dedicated fasttrack event so stop() is
                # responsive without racing the main poll loop's
                # _wake_event clear/set cycle.
                self._fasttrack_wake_event.wait(timeout=FASTTRACK_POLL_INTERVAL_S)
                self._fasttrack_wake_event.clear()
                continue
            try:
                self._process_fasttrack_pending()
            except Exception:
                log.exception("findphone.consumer.tick_failed")
            self._fasttrack_wake_event.wait(timeout=FASTTRACK_POLL_INTERVAL_S)
            self._fasttrack_wake_event.clear()
        log.info("findphone.consumer.stopped")

    def _process_fasttrack_pending(self) -> None:
        try:
            messages = self.api.fasttrack_pending()
        except Exception:
            log.debug("findphone.consumer.poll_failed", exc_info=True)
            return
        if not messages:
            return
        for raw in messages:
            self._dispatch_fasttrack_message(raw)

    def _dispatch_fasttrack_message(self, raw: dict) -> None:
        msg_id = raw.get("id")
        sender_id = raw.get("sender_id") or ""
        encrypted_b64 = raw.get("encrypted_data") or ""
        should_ack = True

        # ACK malformed / unauthorized receiver-side messages on the way
        # out so they don't sit in the queue. Sender-side location
        # updates are explicitly left for the find-device window.
        try:
            if not sender_id:
                log.warning("findphone.consumer.dropped reason=no_sender_id")
                return
            symmetric_key = self._resolve_symmetric_key(sender_id)
            if symmetric_key is None:
                log.warning(
                    "findphone.consumer.dropped reason=unknown_sender peer=%s",
                    sender_id[:12],
                )
                return
            try:
                ciphertext = base64.b64decode(encrypted_b64)
            except Exception:
                log.warning(
                    "findphone.consumer.dropped reason=base64_decode peer=%s",
                    sender_id[:12],
                )
                return
            try:
                plaintext = self.crypto.decrypt_blob(ciphertext, symmetric_key)
            except (InvalidTag, Exception) as exc:
                log.warning(
                    "findphone.consumer.dropped reason=decrypt_failed peer=%s kind=%s",
                    sender_id[:12],
                    type(exc).__name__,
                )
                return
            try:
                import json as _json
                payload = _json.loads(plaintext)
            except Exception:
                log.warning(
                    "findphone.consumer.dropped reason=json_parse peer=%s",
                    sender_id[:12],
                )
                return
            if not isinstance(payload, dict):
                log.warning(
                    "findphone.consumer.dropped reason=non_dict_payload peer=%s",
                    sender_id[:12],
                )
                return

            message = FasttrackAdapter.to_device_message(
                payload, sender_id=sender_id,
            )
            if message is None:
                log.debug(
                    "findphone.consumer.unhandled fn=%s peer=%s",
                    payload.get("fn"),
                    sender_id[:12],
                )
                return
            if message.type == MessageType.FIND_PHONE_LOCATION_UPDATE:
                should_ack = False
                log.debug(
                    "findphone.consumer.left_sender_update_unacked peer=%s",
                    sender_id[:12],
                )
                return
            # Mark active only on inbound start/stop from a paired
            # sender (D2: directed device action).
            if message.type in (
                MessageType.FIND_PHONE_START,
                MessageType.FIND_PHONE_STOP,
            ):
                self._mark_active_device(sender_id, reason="find_device_incoming")
            self._message_dispatcher.dispatch(message)
        finally:
            if should_ack and msg_id is not None:
                try:
                    self.api.fasttrack_ack(int(msg_id))
                except Exception:
                    log.debug("findphone.consumer.ack_failed", exc_info=True)
