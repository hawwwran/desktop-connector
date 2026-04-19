from __future__ import annotations

from ..contract import DesktopPlatform
from .platform import LinuxDesktopPlatform


def compose_linux_platform() -> DesktopPlatform:
    """Single startup-time composition point for Linux platform wiring."""
    return LinuxDesktopPlatform()
