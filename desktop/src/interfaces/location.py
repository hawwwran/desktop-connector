"""Best-effort desktop location provider (M.9).

Only used by the find-device receiver path. The desktop fasttrack
heartbeat normally goes out as state-only; when a provider supplies
a fix, the heartbeat additionally carries ``lat`` / ``lng`` /
``accuracy``.

Privacy: never log raw coordinates. Only ``accuracy`` (a meters
radius) is loggable. Implementations and the responder both honour
this rule. The provider may cache the most recent fix in memory; it
must never persist it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LocationFix:
    lat: float
    lng: float
    accuracy: float | None = None


class LocationProvider(Protocol):
    """Returns the most recent fix, or ``None`` if no fix is available.

    Implementations must be safe to call from any thread. Real
    backends (GeoClue, future Wayland portal, etc.) may serve a
    cached fix and refresh asynchronously; the responder calls this
    each heartbeat without rate-limiting the implementation itself.
    """

    def get_current_fix(self) -> LocationFix | None: ...


class NullLocationProvider:
    """Default provider — always returns ``None``.

    Used in headless runs, when GeoClue isn't reachable, when the
    user denied location permission, and as the unit-test default
    when a test doesn't care about coordinates.
    """

    def get_current_fix(self) -> LocationFix | None:  # noqa: D401
        return None
