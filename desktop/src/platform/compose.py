from __future__ import annotations

import sys

from .contract import DesktopPlatform
from .linux.compose import compose_linux_platform


def compose_desktop_platform() -> DesktopPlatform:
    """Compose the active desktop platform implementation for this runtime."""
    if sys.platform.startswith("linux"):
        return compose_linux_platform()

    # Keep non-Linux fallback explicit until a Windows implementation exists.
    return compose_linux_platform()
