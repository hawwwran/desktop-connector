from __future__ import annotations

from dataclasses import dataclass

from ...backends.linux.clipboard_backend import LinuxClipboardBackend
from ...backends.linux.dialog_backend import LinuxDialogBackend
from ...backends.linux.notification_backend import LinuxNotificationBackend
from ...backends.linux.shell_backend import LinuxShellBackend
from ...interfaces.clipboard import ClipboardBackend
from ...interfaces.dialogs import DialogBackend
from ...interfaces.notifications import NotificationBackend
from ...interfaces.shell import ShellBackend


@dataclass(frozen=True)
class DesktopBackends:
    clipboard: ClipboardBackend
    notifications: NotificationBackend
    dialogs: DialogBackend
    shell: ShellBackend


def compose_linux_backends() -> DesktopBackends:
    """Single startup-time composition point for Linux runtime backends."""
    return DesktopBackends(
        clipboard=LinuxClipboardBackend(),
        notifications=LinuxNotificationBackend(),
        dialogs=LinuxDialogBackend(),
        shell=LinuxShellBackend(),
    )
