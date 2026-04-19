from __future__ import annotations

from collections.abc import Callable

from .message_model import DeviceMessage
from .message_types import MessageType

MessageHandler = Callable[[DeviceMessage], None]


class MessageDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[MessageType, MessageHandler] = {}

    def register(self, message_type: MessageType, handler: MessageHandler) -> None:
        self._handlers[message_type] = handler

    def dispatch(self, message: DeviceMessage) -> bool:
        handler = self._handlers.get(message.type)
        if handler is None:
            return False
        handler(message)
        return True
