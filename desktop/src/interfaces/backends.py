"""Compatibility shim for older imports.

Refactor #10 promotes ``DesktopPlatform`` as the first-class boundary.
"""

from __future__ import annotations

from ..platform.contract import DesktopPlatform

DesktopBackends = DesktopPlatform

__all__ = ["DesktopBackends"]
