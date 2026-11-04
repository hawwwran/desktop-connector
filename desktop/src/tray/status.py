"""Tray icon state-key + visible status text.

``_current_state_key`` maps the connection state machine onto the icon
asset keys; ``_update_icon`` swaps the indicator pixbuf via the stable
on-disk path baked at boot (no file churn → no default-icon flash).
``_status_text`` and ``_auth_banner_text`` produce the menu-title and
auth-failure banner strings.
"""

from ..connection import AuthFailureKind, ConnectionState
from .icon_assets import _make_icon


class StatusMixin:
    def _current_state_key(self) -> str:
        # Use effective_state so a latched 401/403 paints the orange
        # disconnected sparkle, not the blue connected one.
        state = self.conn.effective_state
        # Server connection state wins: DISCONNECTED → orange,
        # RECONNECTING → yellow. The "uploading" / "remote_offline" /
        # "connected" overlays only make sense while we can actually talk
        # to the server — painting them during a network outage was
        # misleading (and kept showing a blue-sparkle + yellow-diamond
        # "uploading" icon during a disconnect with a pending transfer).
        if state == ConnectionState.DISCONNECTED:
            return "disconnected"
        if state == ConnectionState.RECONNECTING:
            return "reconnecting"
        if getattr(self, "_was_uploading", False):
            return "uploading"
        if not getattr(self, "_remote_online", False):
            return "remote_offline"
        return "connected"

    def _update_icon(self) -> None:
        if not self._icon:
            return
        key = self._current_state_key()
        # No-op if the state didn't actually change.
        if key == self._last_applied_state:
            return
        path = getattr(self, "_state_paths", {}).get(key)
        indicator = getattr(self._icon, "_appindicator", None)
        if path and indicator is not None:
            # AppIndicator backend: swap by stable path — no file churn,
            # no theme relookup, no default-icon flash. Must run on the GTK
            # main thread (icon_poll lives on a daemon thread).
            try:
                from gi.repository import GLib
                GLib.idle_add(lambda p=path: (indicator.set_icon(p), False)[1])
                self._last_applied_state = key
                return
            except Exception:
                pass
        # Fallback (gtk / xorg backends, or appindicator failed): pystray's
        # default path, which will re-encode and rewrite the temp file.
        try:
            self._icon.icon = _make_icon(key)
            self._last_applied_state = key
        except Exception:
            pass

    def _status_text(self) -> str:
        """Menu title text. Side effect: triggers a fresh ping when the menu
        is rendered and our last probe is older than 30s."""
        if self.conn.auth_failure_kind is not None:
            return "Pairing lost"
        if self.conn.state != ConnectionState.CONNECTED:
            return "Offline"
        self._maybe_ping(30.0)
        return "Online"

    def _auth_banner_text(self) -> str:
        kind = self.conn.auth_failure_kind
        if kind == AuthFailureKind.CREDENTIALS_INVALID:
            return "⚠ Server doesn't recognise this device — click to re-pair"
        if kind == AuthFailureKind.PAIRING_MISSING:
            return "⚠ Pairing lost on server — click to re-pair"
        return "⚠ Click to re-pair"
