from __future__ import annotations

from pathlib import Path

from ...interfaces.notifications import NotificationBackend
from ...notifications import (
    notify,
    notify_connection_lost,
    notify_connection_restored,
    notify_file_received,
)


class LinuxNotificationBackend(NotificationBackend):
    """Linux notifications backed by notify-send wrapper utilities."""

    def notify(self, title: str, body: str, icon: str = "dialog-information") -> None:
        notify(title, body, icon)

    def notify_file_received(self, filepath: Path) -> None:
        notify_file_received(filepath)

    def notify_connection_lost(self) -> None:
        notify_connection_lost()

    def notify_connection_restored(self) -> None:
        notify_connection_restored()
