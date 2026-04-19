"""Linux desktop platform implementation."""

from .compose import compose_linux_platform
from .platform import LinuxDesktopPlatform

__all__ = ["LinuxDesktopPlatform", "compose_linux_platform"]
