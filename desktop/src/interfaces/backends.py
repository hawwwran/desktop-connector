from __future__ import annotations

from dataclasses import dataclass

from .clipboard import ClipboardBackend
from .dialogs import DialogBackend
from .notifications import NotificationBackend
from .shell import ShellBackend


@dataclass(frozen=True)
class DesktopBackends:
    clipboard: ClipboardBackend
    notifications: NotificationBackend
    dialogs: DialogBackend
    shell: ShellBackend
