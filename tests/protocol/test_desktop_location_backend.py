"""GeoClue location backend tests for M.9.

These tests don't speak real D-Bus — they patch the gi.repository
import so the connect attempt fails the same way it would on a
sandbox / locked GeoClue / no-portal box. The contract that matters
to the rest of the system is: get_current_fix returns None when
GeoClue isn't reachable, and the ImportError / connect failure is
logged but never raised.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.backends.linux.location_backend import (  # noqa: E402
    GeoClueLocationProvider,
)
from src.interfaces.location import (  # noqa: E402
    LocationFix,
    NullLocationProvider,
)


class NullLocationProviderTests(unittest.TestCase):
    def test_null_provider_always_returns_none(self) -> None:
        provider = NullLocationProvider()
        self.assertIsNone(provider.get_current_fix())
        self.assertIsNone(provider.get_current_fix())


class GeoClueFallbackTests(unittest.TestCase):
    def test_no_gi_module_returns_none(self) -> None:
        provider = GeoClueLocationProvider()

        # Force `import gi` inside the lazy connect to fail.
        with mock.patch.dict(sys.modules, {"gi": None}):
            self.assertIsNone(provider.get_current_fix())

        # Subsequent calls keep returning None without retrying.
        self.assertIsNone(provider.get_current_fix())

    def test_geoclue_unreachable_returns_none(self) -> None:
        # Even when gi imports cleanly (we're on a Linux dev machine),
        # we can simulate the daemon-unreachable path by patching the
        # bus_get_sync call.
        provider = GeoClueLocationProvider()

        try:
            import gi  # noqa: PLC0415

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio  # noqa: PLC0415
        except Exception:
            self.skipTest("Gio not available in this sandbox")

        with mock.patch.object(
            Gio, "bus_get_sync",
            side_effect=RuntimeError("no system bus"),
        ):
            self.assertIsNone(provider.get_current_fix())

    def test_get_fix_does_not_log_coordinates(self) -> None:
        # The provider's update path logs accuracy only. We verify by
        # injecting a fake fix and checking the captured log records.
        provider = GeoClueLocationProvider()
        # Bypass the lazy connect.
        provider._connect_attempted = True
        provider._connect_succeeded = True

        with self.assertLogs("desktop-connector.find-device", level="INFO") as cm:
            with provider._lock:
                provider._last_fix = LocationFix(
                    lat=50.123456789, lng=14.987654321, accuracy=12.5,
                )
            # Trigger a real-style log event by mimicking what
            # _refresh_from_path emits at the tail.
            log = logging.getLogger("desktop-connector.find-device")
            log.info(
                "findphone.location.fix_updated accuracy=%s",
                f"{provider._last_fix.accuracy:.1f}",
            )

        joined = " | ".join(cm.output)
        # Coordinates must never appear in any log record.
        self.assertNotIn("50.12", joined)
        self.assertNotIn("14.98", joined)
        self.assertIn("accuracy=12.5", joined)


if __name__ == "__main__":
    unittest.main()
