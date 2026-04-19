from __future__ import annotations

import sys

from .contract import DesktopPlatform
from .linux.compose import compose_linux_platform


def compose_desktop_platform() -> DesktopPlatform:
    """Compose the active desktop platform implementation for this runtime.

    Raises ``NotImplementedError`` on platforms without a concrete
    implementation rather than silently instantiating the Linux backend
    (which would then fail at call-time deep inside click handlers and
    poller threads with confusing "wl-copy: not found" style errors).
    """
    if sys.platform.startswith("linux"):
        return compose_linux_platform()

    raise NotImplementedError(
        f"Desktop platform for sys.platform={sys.platform!r} is not implemented. "
        "Only Linux is supported today; see docs/ROADMAP-windows-client.md."
    )
