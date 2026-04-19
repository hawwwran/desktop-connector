from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformCapabilities:
    """Platform feature/capability flags consumed by desktop core."""

    clipboard_text: bool = True
    clipboard_image: bool = True
    notifications: bool = True
    tray: bool = True
    file_manager_integration: bool = False
    auto_open_urls: bool = True
    open_folder: bool = True
    installer_terminal: bool = True
