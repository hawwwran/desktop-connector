from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .message_types import MessageTransport, MessageType


@dataclass(slots=True)
class DeviceMessage:
    type: MessageType
    transport: MessageTransport
    payload: dict[str, Any] = field(default_factory=dict)
    sender_id: str | None = None
    recipient_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
