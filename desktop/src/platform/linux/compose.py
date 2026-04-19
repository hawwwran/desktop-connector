from __future__ import annotations

from ...backends.linux.clipboard_backend import LinuxClipboardBackend
from ...backends.linux.dialog_backend import LinuxDialogBackend
from ...backends.linux.notification_backend import LinuxNotificationBackend
from ...backends.linux.shell_backend import LinuxShellBackend
from ..contract import DesktopPlatform, PlatformCapabilities


def compose_linux_platform() -> DesktopPlatform:
    """Single startup-time composition point for Linux platform wiring."""
    return DesktopPlatform(
        name="linux",
        clipboard=LinuxClipboardBackend(),
        notifications=LinuxNotificationBackend(),
        dialogs=LinuxDialogBackend(),
        shell=LinuxShellBackend(),
        capabilities=PlatformCapabilities(
            clipboard_text=True,
            clipboard_image=True,
            notifications=True,
            tray=True,
            file_manager_integration=True,
            auto_open_urls=True,
            open_folder=True,
            installer_terminal=True,
        ),
    )
