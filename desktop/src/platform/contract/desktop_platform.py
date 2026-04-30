from __future__ import annotations

from dataclasses import dataclass, field

from ...interfaces.clipboard import ClipboardBackend
from ...interfaces.dialogs import DialogBackend
from ...interfaces.location import LocationProvider, NullLocationProvider
from ...interfaces.notifications import NotificationBackend
from ...interfaces.shell import ShellBackend
from .capabilities import PlatformCapabilities


@dataclass(frozen=True)
class DesktopPlatform:
    """First-class platform contract used by desktop runtime/core."""

    name: str
    clipboard: ClipboardBackend
    notifications: NotificationBackend
    dialogs: DialogBackend
    shell: ShellBackend
    capabilities: PlatformCapabilities
    location: LocationProvider = field(default_factory=NullLocationProvider)
