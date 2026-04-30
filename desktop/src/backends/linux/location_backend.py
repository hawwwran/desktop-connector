"""GeoClue2 location backend for Linux (M.9).

Speaks D-Bus to ``org.freedesktop.GeoClue2`` to obtain the desktop's
last known location. Best-effort: returns ``None`` from
:meth:`get_current_fix` whenever GeoClue isn't reachable, the user
hasn't granted permission, or a fix isn't yet available. Production
callers (``FindDeviceResponder``'s heartbeat) must treat ``None`` as
"no coords this tick — keep sending state-only heartbeat".

Privacy: this module never logs raw lat/lng. The accuracy radius is
loggable; coordinates are not. The most recent fix is held in memory
on a single instance; nothing is persisted.

Thread safety: a background ``GLib.MainLoop`` thread receives signal
callbacks and updates ``_last_fix`` under a lock.
:meth:`get_current_fix` reads the cached fix under the same lock so
the responder's heartbeat thread doesn't race the GLib thread.

GeoClue dependency notes (documented for the runbook in
``docs/plans/desktop-multi-device-support.md`` M.9 hardening):

* Distros ship GeoClue under different agent gates. Headless runs
  (no desktop session, no Mozilla Location Service network access)
  see ``OutOfRange`` indefinitely.
* Sandboxed AppImages need the ``location`` portal proxied through
  ``xdg-desktop-portal``; without it, GeoClue rejects the client. We
  fail-soft to "no coords" rather than papering over with a stub.
* GNOME's "Location Services" toggle in Settings → Privacy disables
  GeoClue at the daemon level; same fail-soft path.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from ...interfaces.location import LocationFix, LocationProvider

log = logging.getLogger("desktop-connector.find-device")

GEOCLUE_BUS_NAME = "org.freedesktop.GeoClue2"
GEOCLUE_MANAGER_PATH = "/org/freedesktop/GeoClue2/Manager"
GEOCLUE_MANAGER_INTERFACE = "org.freedesktop.GeoClue2.Manager"
GEOCLUE_CLIENT_INTERFACE = "org.freedesktop.GeoClue2.Client"
GEOCLUE_LOCATION_INTERFACE = "org.freedesktop.GeoClue2.Location"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

# Accuracy levels — see GeoClue's accuracy_level enum. We request
# Exact when allowed; the daemon may downgrade based on the agent
# policy.
ACCURACY_LEVEL_EXACT = 8

DESKTOP_ID = "desktop-connector"


if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class GeoClueLocationProvider(LocationProvider):
    """Lazy GeoClue2 client that exposes the most recent fix.

    Construction is cheap (no D-Bus traffic). The first call to
    :meth:`get_current_fix` triggers the lazy connect, which schedules
    a ``Manager.GetClient`` → ``Client.Start`` round-trip on a
    background thread. From there, the GLib loop receives
    ``LocationUpdated`` signals and refreshes ``_last_fix``.

    On any error during connect/start, the provider switches to a
    permanent "no fix" state — :meth:`get_current_fix` returns
    ``None`` for the rest of the process lifetime. We don't retry on
    a timer because GeoClue's failure modes (no daemon, no portal,
    Location Services off) don't change without a user action.
    """

    def __init__(self, *, desktop_id: str = DESKTOP_ID) -> None:
        self._desktop_id = desktop_id
        self._lock = threading.Lock()
        self._last_fix: LocationFix | None = None
        self._connect_attempted = False
        self._connect_succeeded = False
        self._gi_modules: tuple[Any, Any, Any] | None = None
        self._client_proxy: Any = None
        self._loop_thread: threading.Thread | None = None

    def get_current_fix(self) -> LocationFix | None:
        with self._lock:
            if not self._connect_attempted:
                self._connect_attempted = True
                self._try_connect_locked()
            return self._last_fix

    # --- connect / start --------------------------------------------

    def _try_connect_locked(self) -> None:
        """Best-effort GeoClue connect.

        Caller holds ``self._lock``. We import gi/dbus inside this
        function so module import never fails on systems missing
        python3-gi. The full handshake runs synchronously here; the
        signal subscription and main loop run on a background thread
        afterwards so :meth:`get_current_fix` never blocks.
        """
        try:
            import gi  # noqa: PLC0415
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib  # noqa: PLC0415
        except Exception as exc:
            log.info(
                "findphone.location.unavailable reason=gi_import_failed err=%s",
                type(exc).__name__,
            )
            return

        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            manager = Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                GEOCLUE_BUS_NAME,
                GEOCLUE_MANAGER_PATH,
                GEOCLUE_MANAGER_INTERFACE,
                None,
            )
        except Exception as exc:
            log.info(
                "findphone.location.unavailable reason=geoclue_unreachable err=%s",
                type(exc).__name__,
            )
            return

        try:
            client_path = manager.call_sync(
                "GetClient",
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            ).unpack()[0]

            client = Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                GEOCLUE_BUS_NAME,
                client_path,
                GEOCLUE_CLIENT_INTERFACE,
                None,
            )
            props = Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                GEOCLUE_BUS_NAME,
                client_path,
                DBUS_PROPERTIES_INTERFACE,
                None,
            )
            props.call_sync(
                "Set",
                GLib.Variant("(ssv)", (
                    GEOCLUE_CLIENT_INTERFACE,
                    "DesktopId",
                    GLib.Variant("s", self._desktop_id),
                )),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            props.call_sync(
                "Set",
                GLib.Variant("(ssv)", (
                    GEOCLUE_CLIENT_INTERFACE,
                    "RequestedAccuracyLevel",
                    GLib.Variant("u", ACCURACY_LEVEL_EXACT),
                )),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            client.call_sync(
                "Start",
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except Exception as exc:
            log.info(
                "findphone.location.unavailable reason=geoclue_start_failed err=%s",
                type(exc).__name__,
            )
            return

        # Subscribe to LocationUpdated; pull the initial location if
        # one is already available.
        client.connect("g-signal", self._on_client_signal)
        self._client_proxy = client
        self._gi_modules = (Gio, GLib, bus)
        self._connect_succeeded = True

        # Start a private GLib main loop thread so signals fire even
        # when the rest of the desktop runs without a GTK loop. The
        # thread is daemonized; it dies when the process exits.
        loop = GLib.MainLoop()
        self._loop = loop
        thread = threading.Thread(
            target=loop.run, daemon=True, name="geoclue-loop",
        )
        self._loop_thread = thread
        thread.start()

        log.info("findphone.location.connected backend=geoclue")

    # --- signal handler ---------------------------------------------

    def _on_client_signal(
        self,
        _proxy: Any,
        _sender: str,
        signal_name: str,
        params: Any,
    ) -> None:
        if signal_name != "LocationUpdated":
            return
        try:
            _old_path, new_path = params.unpack()
        except Exception:
            log.debug("findphone.location.signal_unpack_failed", exc_info=True)
            return
        try:
            self._refresh_from_path(new_path)
        except Exception:
            log.debug("findphone.location.refresh_failed", exc_info=True)

    def _refresh_from_path(self, location_path: str) -> None:
        if not self._gi_modules:
            return
        Gio, GLib, bus = self._gi_modules
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            GEOCLUE_BUS_NAME,
            location_path,
            GEOCLUE_LOCATION_INTERFACE,
            None,
        )
        latitude = proxy.get_cached_property("Latitude")
        longitude = proxy.get_cached_property("Longitude")
        accuracy = proxy.get_cached_property("Accuracy")
        if latitude is None or longitude is None:
            return
        fix = LocationFix(
            lat=float(latitude.unpack()),
            lng=float(longitude.unpack()),
            accuracy=(
                float(accuracy.unpack()) if accuracy is not None else None
            ),
        )
        with self._lock:
            self._last_fix = fix
        # Never log lat/lng; accuracy is fine.
        log.info(
            "findphone.location.fix_updated accuracy=%s",
            f"{fix.accuracy:.1f}" if fix.accuracy is not None else "None",
        )
