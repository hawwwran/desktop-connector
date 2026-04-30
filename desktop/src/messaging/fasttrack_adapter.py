from __future__ import annotations

from .message_model import DeviceMessage
from .message_types import MessageTransport, MessageType

# D5: receivers accept both `find-phone` (legacy, Android-default) and
# `find-device` (new alias for the desktop-being-found path). Senders
# stay on `find-phone` until both platforms migrate.
_FIND_DEVICE_FNS = {"find-phone", "find-device"}


class FasttrackAdapter:
    @staticmethod
    def to_device_message(payload: dict, *, sender_id: str | None = None) -> DeviceMessage | None:
        fn = payload.get("fn")
        if fn not in _FIND_DEVICE_FNS:
            return None

        action = payload.get("action")
        if action == "start":
            msg_type = MessageType.FIND_PHONE_START
        elif action == "stop":
            msg_type = MessageType.FIND_PHONE_STOP
        elif payload.get("state") in {"ringing", "stopped"}:
            msg_type = MessageType.FIND_PHONE_LOCATION_UPDATE
        else:
            return None

        return DeviceMessage(
            type=msg_type,
            transport=MessageTransport.FASTTRACK,
            payload=dict(payload),
            sender_id=sender_id,
        )
