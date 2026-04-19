from __future__ import annotations

from .message_model import DeviceMessage
from .message_types import MessageTransport, MessageType


class FasttrackAdapter:
    @staticmethod
    def to_device_message(payload: dict, *, sender_id: str | None = None) -> DeviceMessage | None:
        fn = payload.get("fn")
        if fn != "find-phone":
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
