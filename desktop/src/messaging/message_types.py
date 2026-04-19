from __future__ import annotations

from enum import Enum


class MessageType(str, Enum):
    CLIPBOARD_TEXT = "clipboard.text"
    CLIPBOARD_IMAGE = "clipboard.image"
    PAIRING_UNPAIR = "pairing.unpair"
    FIND_PHONE_START = "find_phone.start"
    FIND_PHONE_STOP = "find_phone.stop"
    FIND_PHONE_LOCATION_UPDATE = "find_phone.location_update"


class MessageTransport(str, Enum):
    TRANSFER_FILE = "transfer_file"
    FASTTRACK = "fasttrack"
