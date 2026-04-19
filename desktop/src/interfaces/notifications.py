from __future__ import annotations

from pathlib import Path
from typing import Protocol


class NotificationBackend(Protocol):
    def notify(self, title: str, body: str, icon: str = "dialog-information") -> None:
        ...

    def notify_file_received(self, filepath: Path) -> None:
        ...

    def notify_connection_lost(self) -> None:
        ...

    def notify_connection_restored(self) -> None:
        ...
