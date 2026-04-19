from __future__ import annotations

from .message_model import DeviceMessage
from .message_types import MessageTransport, MessageType


class FnTransferAdapter:
    @staticmethod
    def to_device_message(filename: str, payload: bytes, *, sender_id: str | None = None) -> DeviceMessage | None:
        parts = filename.split(".")
        if len(parts) < 3 or parts[1] != "fn":
            return None

        fn = parts[2]
        if fn == "clipboard":
            subtype = parts[3] if len(parts) > 3 else "text"
            if subtype == "text":
                return DeviceMessage(
                    type=MessageType.CLIPBOARD_TEXT,
                    transport=MessageTransport.TRANSFER_FILE,
                    payload={"text": payload.decode("utf-8", errors="replace")},
                    sender_id=sender_id,
                    metadata={"filename": filename},
                )
            if subtype == "image":
                return DeviceMessage(
                    type=MessageType.CLIPBOARD_IMAGE,
                    transport=MessageTransport.TRANSFER_FILE,
                    payload={"image_bytes": payload},
                    sender_id=sender_id,
                    metadata={"filename": filename},
                )
            return None

        if fn == "unpair":
            return DeviceMessage(
                type=MessageType.PAIRING_UNPAIR,
                transport=MessageTransport.TRANSFER_FILE,
                payload={},
                sender_id=sender_id,
                metadata={"filename": filename},
            )

        return None
