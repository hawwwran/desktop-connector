from __future__ import annotations

from ...backends.linux.clipboard_backend import LinuxClipboardBackend
from ...backends.linux.dialog_backend import LinuxDialogBackend
from ...backends.linux.notification_backend import LinuxNotificationBackend
from ...backends.linux.shell_backend import LinuxShellBackend
from ...interfaces.backends import DesktopBackends


def compose_linux_backends() -> DesktopBackends:
    """Single startup-time composition point for Linux runtime backends."""
    return DesktopBackends(
        clipboard=LinuxClipboardBackend(),
        notifications=LinuxNotificationBackend(),
        dialogs=LinuxDialogBackend(),
        shell=LinuxShellBackend(),
    )
