"""Desktop platform contract.

Core modules depend only on the contract types — ``DesktopPlatform`` and
``PlatformCapabilities`` — so that importing them does not transitively pull in
any concrete (platform-specific) implementation. Bootstrap code instantiates
the active platform by importing ``compose_desktop_platform`` directly from
``.compose``.
"""

from .contract import DesktopPlatform, PlatformCapabilities

__all__ = ["DesktopPlatform", "PlatformCapabilities"]
