"""Desktop platform contracts and composition."""

from .compose import compose_desktop_platform
from .contract import DesktopPlatform, PlatformCapabilities

__all__ = ["DesktopPlatform", "PlatformCapabilities", "compose_desktop_platform"]
