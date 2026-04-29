"""
System tray icon with status menu.
GTK4 windows run as subprocesses to avoid GTK3/4 conflict (pystray loads GTK3).
"""

import base64
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image, ImageDraw

from .api_client import ApiClient
from .brand import (
    DC_BLUE_400_RGB,
    DC_BLUE_800_RGB,
    DC_ORANGE_700_RGB,
    DC_YELLOW_500_RGB,
)
from .config import Config
from .connection import AuthFailureKind, ConnectionManager, ConnectionState
from .crypto import KeyManager
from .history import TransferHistory
from .platform import DesktopPlatform
from .poller import Poller
from .updater import update_runner, version_check

log = logging.getLogger(__name__)

_DESKTOP_DIR = Path(__file__).parent.parent

# If the server ping comes back online=false but reports the phone's
# last_seen_at within this window, we still consider it online. Longer
# than typical poll cadences (10s Android, 25s desktop long-poll) but
# short enough that a truly offline phone registers within a minute.
REMOTE_FRESH_WINDOW_S = 60


_ASSETS_DIR = Path(__file__).parent.parent / "assets"
_BRAND_DIR = _ASSETS_DIR / "brand"


# --- Sparkle-star tray compositing ------------------------------------------
#
# Binary connected/disconnected is carried by SHAPE (filled vs outline star)
# so it's still readable when the tray theme forces monochrome. Sub-state
# (uploading, reconnecting, remote offline) is carried by TINT.

def _load_master(name: str) -> Image.Image | None:
    p = _BRAND_DIR / name
    if not p.exists():
        return None
    img = Image.open(p).convert("RGBA")
    img.load()
    return img


def _tint(mask: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Recolor a black-on-transparent mask to `rgb`, preserving alpha."""
    alpha = mask.split()[-1]
    tinted = Image.new("RGBA", mask.size, (*rgb, 255))
    tinted.putalpha(alpha)
    return tinted


def _crop_and_pad(img: Image.Image, bbox: tuple[int, int, int, int],
                  pad_ratio: float = 0.02) -> Image.Image:
    """Crop `img` to `bbox`, then center it on a padded square canvas.
    All masks sharing the same `bbox` + `pad_ratio` stay pixel-aligned."""
    cropped = img.crop(bbox)
    w, h = cropped.size
    side = max(w, h)
    pad = max(1, int(side * pad_ratio))
    canvas = side + 2 * pad
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.alpha_composite(cropped, (pad + (side - w) // 2, pad + (side - h) // 2))
    return out


# Tray icons are rendered by pystray at 22–48 px (64 px on HiDPI). The PIL
# image is PNG-encoded on every swap and written to a temp file the indicator
# re-reads; at 600 px that's ~35 ms, long enough for the indicator to fall back
# to the default app icon while reloading. 128 px encodes in ~3 ms, below one
# frame, so the swap looks instant.
_TRAY_RENDER_SIZE = 128


def _load_icons() -> dict[str, Image.Image]:
    """
    Build one PIL image per tray state. Composited once at import time.
    Keys match _get_state_icon() below.
    """
    full_raw = _load_master("star-full-bw.png")
    center_raw = _load_master("star-center-bw.png")
    # Anchor center to the full-star bbox so the inner diamond stays
    # geometrically centered inside the outer star after trimming.
    if full_raw is not None:
        star_bbox = full_raw.split()[-1].getbbox()
        full = _crop_and_pad(full_raw, star_bbox)
        center = _crop_and_pad(center_raw, star_bbox) if center_raw is not None else None
    else:
        full = center = None

    icons: dict[str, Image.Image] = {}

    if full is not None:
        # Shape is always a filled star; color scale carries state:
        #   dark blue  = fully connected (server + phone)
        #   sky blue   = half-connected  (server ok, phone offline)
        #   yellow     = reconnecting    (server handshake in progress)
        #   orange     = offline         (server unreachable)
        icons["connected"] = _tint(full, DC_BLUE_800_RGB)
        icons["remote_offline"] = _tint(full, DC_BLUE_400_RGB)
        icons["reconnecting"] = _tint(full, DC_YELLOW_500_RGB)
        icons["disconnected"] = _tint(full, DC_ORANGE_700_RGB)

        if center is not None:
            upload = _tint(full, DC_BLUE_800_RGB)
            inner = _tint(center, DC_YELLOW_500_RGB)
            upload.alpha_composite(inner)
            icons["uploading"] = upload
        else:
            icons["uploading"] = _tint(full, DC_YELLOW_500_RGB)

    if not icons:
        # Installer didn't ship brand/, or running from an old checkout.
        # Draw flat-colored circles as a bare fallback so the tray still works.
        size = _TRAY_RENDER_SIZE
        for state, rgb in (("connected", DC_BLUE_800_RGB),
                           ("remote_offline", DC_BLUE_400_RGB),
                           ("uploading", DC_YELLOW_500_RGB),
                           ("reconnecting", DC_YELLOW_500_RGB),
                           ("disconnected", DC_ORANGE_700_RGB)):
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse([16, 16, size - 16, size - 16], fill=rgb)
            icons[state] = img

    # Downsample once at load time so icon swaps don't re-encode 600 px PNGs.
    for key, img in icons.items():
        if img.size != (_TRAY_RENDER_SIZE, _TRAY_RENDER_SIZE):
            icons[key] = img.resize(
                (_TRAY_RENDER_SIZE, _TRAY_RENDER_SIZE), Image.LANCZOS)

    return icons


_icons = _load_icons()


def _make_icon(state: str) -> Image.Image:
    """Return a copy of a pre-composited state icon. Safe from any thread."""
    img = _icons.get(state) or _icons.get("connected") or next(iter(_icons.values()))
    return img.copy()


def _bake_state_paths(cache_dir: Path) -> dict[str, str]:
    """
    Write each state icon to a stable file in `cache_dir` once. Returns
    {state: absolute_path}.

    pystray's default _update_icon deletes the current temp file, mktemps a
    new random path, then calls AppIndicator.set_icon(new_path). The delete +
    rename forces a theme/path lookup in the tray frontend (GNOME Shell's
    AppIndicator ext, KDE systray, xfce4-indicator-plugin), which briefly
    renders the stock application icon until the new path resolves — that's
    the ~500ms "burger" flash. Writing each state once to a stable path
    lets us call AppIndicator.set_icon(same_path) without file churn, so
    the frontend sees only a pixbuf-reload and skips the fallback render.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for state, img in _icons.items():
        p = cache_dir / f"tray-{state}.png"
        img.save(p, format="PNG")
        paths[state] = str(p)
    return paths


class TrayApp:

    def __init__(self, connection: ConnectionManager, poller: Poller,
                 api: ApiClient, config: Config, crypto: KeyManager,
                 history: TransferHistory, save_dir: Path,
                 platform: DesktopPlatform):
        self.conn = connection
        self.poller = poller
        self.api = api
        self.config = config
        self.crypto = crypto
        self.history = history
        self.save_dir = save_dir
        self.platform = platform
        self._icon = None
        self._should_quit = threading.Event()
        self._was_uploading = False
        self._remote_online = False
        self._fcm_available = False
        self._fcm_checked = False
        # Update-check state (P.6b). _update_info is the latest result from
        # version_check.check_for_update(); _running_appimage caches whether
        # we're inside an AppImage so the menu visibility lambdas don't
        # re-read os.environ on every menu open.
        self._update_info: version_check.UpdateInfo | None = None
        self._running_appimage = bool(os.environ.get("APPIMAGE"))

    def run(self) -> None:
        try:
            import pystray
        except ImportError:
            log.warning("pystray not available, running without tray icon")
            self._should_quit.wait()
            return

        def build_menu():
            # Refresh icon on every menu open to fix stale icons
            self._update_icon()
            return pystray.Menu(
                pystray.MenuItem(
                    lambda _: self._status_text(),
                    None,
                    enabled=False,
                ),
                pystray.MenuItem(
                    lambda _: self._auth_banner_text(),
                    self._repair,
                    visible=lambda _: self.conn.auth_failure_kind is not None,
                ),
                pystray.MenuItem(
                    lambda _: "⚠ Server storage full — delivery waiting",
                    None,
                    enabled=False,
                    visible=lambda _: (self.conn.storage_full
                                      and self.conn.auth_failure_kind is None),
                ),
                # H.5: rendered when the OS keyring isn't reachable and
                # secrets are sitting in plaintext config.json. Click
                # opens an explainer window with what / why / how-to-fix.
                pystray.MenuItem(
                    lambda _: "⚠ Secrets in plaintext — click for info",
                    self._show_secret_storage_warning,
                    visible=lambda _: not self.config.is_secret_storage_secure(),
                ),
                pystray.MenuItem(
                    "Force Reconnect",
                    self._try_now,
                    visible=lambda _: (self.conn.state != ConnectionState.CONNECTED
                                      and self.conn.auth_failure_kind is None
                                      and not self.conn.storage_full),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Send Files...", self._send_files, visible=lambda _: self.config.is_paired),
                pystray.MenuItem(
                    "Send Clipboard",
                    self._send_clipboard,
                    visible=lambda _: self.config.is_paired and self.platform.capabilities.clipboard_text,
                ),
                pystray.MenuItem("Find my Phone", self._find_phone,
                                 visible=lambda _: self.config.is_paired and self._fcm_available),
                pystray.MenuItem("Show History", self._show_history),
                pystray.MenuItem(
                    "Open Save Folder",
                    self._open_folder,
                    visible=lambda _: self.platform.capabilities.open_folder,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Pair...", self._pair, visible=lambda _: not self.config.is_paired),
                pystray.MenuItem("Settings...", self._show_settings),
                # Update items appear only inside an AppImage — apt-pip and
                # dev-tree installs can't act on an in-app update anyway.
                pystray.MenuItem(
                    lambda _: f"Update available → {self._update_info.latest_version}"
                              if self._update_info else "",
                    pystray.Menu(
                        pystray.MenuItem("Install update", self._install_update),
                        pystray.MenuItem("View release notes", self._open_release_notes),
                        pystray.MenuItem("Dismiss this version", self._dismiss_update),
                    ),
                    visible=lambda _: self._has_pending_update(),
                ),
                pystray.MenuItem(
                    "Check for updates",
                    self._manual_update_check,
                    visible=lambda _: self._running_appimage,
                ),
                pystray.MenuItem("Quit", self._quit),
            )

        self._icon = pystray.Icon(
            "desktop-connector",
            icon=_make_icon(self._current_state_key()),
            title="Desktop Connector",
            menu=build_menu(),
        )

        # Stable on-disk paths keyed by state. Bypasses pystray's delete +
        # mktemp path churn on every swap, which is what produced the
        # default-icon flash between state transitions.
        self._state_paths = _bake_state_paths(self.config.config_dir / "tray")
        self._last_applied_state: str | None = None

        self.conn.on_state_change(lambda state: self._update_icon())

        def _on_auth_failure(kind):
            log.warning("auth.failure.surface kind=%s", kind.value)
            # effective_state just flipped — repaint the tray icon (blue → orange)
            # alongside the menu banner.
            self._update_icon()
            try:
                self._icon.update_menu()
            except Exception:
                pass
            try:
                self.platform.notifications.notify(
                    "Pairing lost",
                    "Server no longer recognises this device. Click the tray icon to re-pair.",
                )
            except Exception:
                log.exception("auth failure notification error")
        self.conn.on_auth_failure(_on_auth_failure)

        def _on_storage_full(flagged: bool):
            try:
                self._icon.update_menu()
            except Exception:
                pass
            if flagged:
                try:
                    self.platform.notifications.notify(
                        "Server storage full",
                        "A send is waiting until the recipient has finished downloading earlier transfers.",
                    )
                except Exception:
                    log.exception("storage full notification error")
        self.conn.on_storage_full_change(_on_storage_full)

        # Poll for state changes that affect icon or menu
        self._upload_status_file = self.config.config_dir / "upload_active.json"
        self._was_uploading = False
        self._was_paired = self.config.is_paired
        self._remote_online = False
        self._last_ping_time = 0.0
        self._ping_in_flight = False
        self._ping_lock = threading.Lock()
        # 5 min between probes: icon may be stale, but phone battery stays near-zero.
        # On-connect triggers an immediate ping regardless.
        self._ping_interval = 300.0
        import threading as _t
        def icon_poll():
            import time
            was_connected = False
            while not self._should_quit.is_set():
                changed = False

                # "Outgoing" = uploading OR delivering. Covers the full
                # uploading → delivering → delivered arc.
                outgoing = (self._upload_status_file.exists()
                            or self.poller.has_live_outgoing())
                if outgoing != self._was_uploading:
                    self._was_uploading = outgoing
                    self._update_icon()
                    changed = True

                paired = self.config.is_paired
                if paired != self._was_paired:
                    self._was_paired = paired
                    changed = True

                # One-time FCM availability check on first connection
                if not self._fcm_checked and self.conn.state == ConnectionState.CONNECTED:
                    try:
                        self._fcm_available = self.api.check_fcm_available()
                        self._fcm_checked = True
                        if self._fcm_available:
                            changed = True
                    except Exception:
                        pass

                connected = self.conn.state == ConnectionState.CONNECTED
                if connected:
                    just_connected = not was_connected
                    min_age = 0.0 if just_connected else self._ping_interval
                    self._maybe_ping(min_age)
                elif self._remote_online:
                    self._remote_online = False
                    self._update_icon()
                was_connected = connected

                if changed:
                    try:
                        self._icon.update_menu()
                    except Exception:
                        pass

                time.sleep(2)
        _t.Thread(target=icon_poll, daemon=True).start()

        # In-app updater (P.6b): one check on boot + once every 24 h.
        # version_check itself caches at the same TTL, so the network is
        # only hit when truly due. Outside an AppImage the loop is a no-op.
        if self._running_appimage:
            _t.Thread(target=self._update_check_loop, daemon=True).start()

        self._icon.run()

    def stop(self) -> None:
        self._should_quit.set()
        if self._icon:
            self._icon.stop()

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

    def _repair(self, *_) -> None:
        """User tapped the banner. Wipe the appropriate scope locally, then
        launch the pairing subprocess. ConnectionManager's flag is cleared so
        the banner disappears; the pairing flow restores creds.

        On full-wipe (401 Credentials Invalid), re-register with the newly
        generated keypair BEFORE the pairing subprocess starts — the pairing
        QR embeds the desktop's device_id, and the phone's sendPairingRequest
        will fail if that device_id doesn't exist in the server's devices
        table yet."""
        kind = self.conn.auth_failure_kind
        scope = "full" if kind == AuthFailureKind.CREDENTIALS_INVALID else "pairing_only"
        log.info("auth.repair.started scope=%s kind=%s", scope,
                 kind.value if kind else "none")
        try:
            self.poller.local_unpair(scope, notify_title="Re-pair started",
                                      notify_body="Follow the QR flow to reconnect.")
        except Exception:
            log.exception("local_unpair during repair failed")
        if scope == "full":
            if not self._reregister_after_wipe():
                # Let the user retry by leaving the latched flag visible.
                log.error("auth.repair.reregister.failed")
                try:
                    self.platform.notifications.notify(
                        "Re-pair failed",
                        "Couldn't re-register with the server. Check network and try again.",
                    )
                except Exception:
                    pass
                return
        self.conn.clear_auth_failure()
        self._update_icon()
        try:
            self._icon.update_menu()
        except Exception:
            pass
        # Launch the pairing subprocess (same path as "Pair..." menu entry).
        self._pair()

    def _reregister_after_wipe(self) -> bool:
        """Register the freshly generated keypair with the server and update
        the in-memory ConnectionManager. Returns True on success."""
        try:
            result = self.api.register(self.config.server_url)
        except Exception:
            log.exception("register call raised")
            return False
        if not result or "device_id" not in result or "auth_token" not in result:
            return False
        new_device_id = result["device_id"]
        new_auth_token = result["auth_token"]
        self.config.device_id = new_device_id
        self.config.auth_token = new_auth_token
        # ConnectionManager caches creds on construction — refresh under its
        # lock so an in-flight request on another thread can't observe a
        # half-updated (device_id, auth_token) pair.
        self.conn.update_credentials(new_device_id, new_auth_token)
        log.info("auth.repair.reregistered device_id=%s", new_device_id[:12])
        return True

    def _maybe_ping(self, min_age_sec: float) -> None:
        """Atomic check-and-fire: under _ping_lock, confirm we're idle and
        stale enough, then claim the slot before spawning the worker. Prevents
        the icon_poll / _status_text race where both threads could pass the
        gate simultaneously and fire two pings."""
        import time
        if not self.config.is_paired:
            return
        paired = self.config.get_first_paired_device()
        if not paired:
            return
        target_id, _ = paired
        with self._ping_lock:
            if self._ping_in_flight:
                return
            if (time.monotonic() - self._last_ping_time) < min_age_sec:
                return
            self._last_ping_time = time.monotonic()
            self._ping_in_flight = True

        def run():
            try:
                log.debug("ping.request.sent recipient=%s", target_id[:12])
                result = self.api.ping_device(target_id)
                if result is None:
                    return
                online = bool(result.get("online"))
                # Without FCM on the server, the ping handler can only say
                # "online" via the "fresh" shortcut (phone talked to the
                # server this second) — which mostly doesn't fire for
                # phones on 10–25 s poll cadences. Fall back to the
                # server-reported last_seen_at freshness: if the phone
                # contacted the server within REMOTE_FRESH_WINDOW_S it's
                # functionally alive, regardless of what FCM says.
                if not online:
                    last_seen = result.get("last_seen_at") or 0
                    if last_seen and (time.time() - last_seen) <= REMOTE_FRESH_WINDOW_S:
                        online = True
                if online != self._remote_online:
                    log.info("ping.response.received recipient=%s online=%s via=%s rtt_ms=%s",
                             target_id[:12], online, result.get("via"), result.get("rtt_ms"))
                    self._remote_online = online
                    self._update_icon()
            except Exception as e:
                log.warning("ping.request.failed error_kind=%s", type(e).__name__)
            finally:
                with self._ping_lock:
                    self._ping_in_flight = False
        threading.Thread(target=run, daemon=True).start()

    # --- GTK4 windows (subprocess to avoid GTK3/4 conflict) ---

    def _open_gtk4_window(self, window_name: str) -> None:
        log.info("platform.subprocess.spawned window=%s", window_name)
        appimage_path = os.environ.get("APPIMAGE")
        if appimage_path:
            # Inside an AppImage: re-enter via $APPIMAGE so the child gets
            # the bundled GTK4 / libadwaita / WebKitGTK and survives the
            # parent's FUSE mount lifetime. AppRun's --gtk-window=<NAME>
            # dispatch routes to `python -m src.windows <NAME>` inside.
            cmd = [
                appimage_path,
                f"--gtk-window={window_name}",
                f"--config-dir={self.config.config_dir}",
            ]
            cwd = None
        else:
            # Dev tree: run the source-tree windows entrypoint directly.
            cmd = [
                sys.executable, "-m", "src.windows", window_name,
                f"--config-dir={self.config.config_dir}",
            ]
            cwd = str(_DESKTOP_DIR)
        subprocess.Popen(cmd, cwd=cwd)

    def _send_files(self, *_) -> None:
        self._open_gtk4_window("send-files")

    def _show_settings(self, *_) -> None:
        self._open_gtk4_window("settings")

    def _show_history(self, *_) -> None:
        self._open_gtk4_window("history")

    def _find_phone(self, *_) -> None:
        self._open_gtk4_window("find-phone")

    def _show_secret_storage_warning(self, *_) -> None:
        # H.5: log an event each time the user clicks the warning so
        # the diagnostic trail records "user was warned visually".
        log.warning("config.secrets.user_warned surface=tray")
        self._open_gtk4_window("secret-storage-warning")

    # --- Pairing ---

    def _pair(self, *_) -> None:
        self._open_gtk4_window("pairing")

    # --- Send clipboard ---

    def _send_clipboard(self, *_) -> None:
        threading.Thread(target=self._do_send_clipboard, daemon=True).start()

    def _do_send_clipboard(self) -> None:
        result = self.platform.clipboard.read_clipboard()
        if result is None:
            self.platform.notifications.notify("Clipboard empty", "Nothing to send")
            return

        filename, data, mime_type = result
        paired = self.config.get_first_paired_device()
        if not paired:
            return

        target_id, target_info = paired
        symmetric_key = base64.b64decode(target_info["symmetric_key_b64"])

        import tempfile
        tmp = Path(tempfile.mktemp(suffix="_" + filename))
        tmp.write_bytes(data)

        if mime_type.startswith("text/"):
            import re
            text = data.decode("utf-8", errors="replace")
            urls = re.findall(r'https?://\S+', text)
            if len(urls) == 1:
                preview = text
            elif len(text) > 40:
                preview = text[:40] + "..."
            else:
                preview = text
        else:
            preview = "Clipboard image"

        # Add to history before uploading so it appears immediately
        progress_tid = [None]
        def upload_progress(transfer_id, uploaded, total_chunks):
            if uploaded == 0:
                progress_tid[0] = transfer_id
                self.history.add(filename=filename, display_label=preview,
                                 direction="sent", size=len(data), content_path=str(tmp),
                                 transfer_id=transfer_id, status="uploading",
                                 chunks_downloaded=0, chunks_total=total_chunks)
            else:
                self.history.update(transfer_id,
                                    chunks_downloaded=uploaded, chunks_total=total_chunks)

        tid = self.api.send_file(tmp, target_id, symmetric_key,
                                 filename_override=filename, on_progress=upload_progress)
        if tid:
            # Never log the preview — it's decrypted clipboard content.
            log.info("Clipboard sent (len=%d)", len(preview))
            self.platform.notifications.notify("Clipboard sent", preview)
            # Upload logic cleans up its own progress fields; delivery tracker owns recipient_* from here.
            self.history.update(tid, status="complete", chunks_downloaded=0, chunks_total=0)
        else:
            if progress_tid[0]:
                self.history.update(progress_tid[0], status="failed")
            self.platform.notifications.notify("Send failed", "Could not send clipboard")

    # --- Misc ---

    def _open_folder(self, *_) -> None:
        if self.platform.shell.open_folder(self.save_dir):
            log.info("platform.open_folder.succeeded")

    def _try_now(self, *_) -> None:
        self.conn.try_now()
        self.poller.wake()

    def _quit(self, *_) -> None:
        self.poller.stop()
        self.stop()

    # --- Updates (P.6b) ---

    def _has_pending_update(self) -> bool:
        """Surface "Update available" iff inside an AppImage, network said
        a newer version exists, AND user hasn't dismissed that exact version."""
        if not self._running_appimage:
            return False
        info = self._update_info
        if info is None or not info.is_newer:
            return False
        if version_check.is_version_dismissed(info.latest_version):
            return False
        return True

    def _update_check_loop(self) -> None:
        """Boot + 24-hour periodic update check. Runs in a daemon thread."""
        import time as _time
        while not self._should_quit.is_set():
            self._refresh_update_info(force=False)
            # Wait 24 h, but break out promptly on shutdown.
            self._should_quit.wait(timeout=24 * 3600)

    def _refresh_update_info(self, *, force: bool) -> None:
        try:
            info = version_check.check_for_update(force=force)
        except Exception:
            log.exception("update_check.unexpected_error")
            return
        # Only update + repaint if the surfaced state actually changed,
        # to avoid menu flicker.
        prev = self._update_info
        self._update_info = info
        if (prev is None) != (info is None) or (
            info is not None and prev is not None
            and (info.latest_version != prev.latest_version
                 or info.is_newer != prev.is_newer)
        ):
            try:
                self._icon.update_menu()
            except Exception:
                pass
        if info and info.is_newer and not version_check.is_version_dismissed(info.latest_version):
            log.info("update_check.surfaced latest=%s current=%s",
                     info.latest_version, info.current_version)

    def _manual_update_check(self, *_) -> None:
        """User clicked "Check for updates"."""
        try:
            self.platform.notifications.notify(
                "Checking for updates…", "Talking to GitHub.",
            )
        except Exception:
            pass
        # Fire-and-forget; check_for_update has its own timeouts.
        threading.Thread(target=lambda: self._do_manual_check(), daemon=True).start()

    def _do_manual_check(self) -> None:
        self._refresh_update_info(force=True)
        info = self._update_info
        if info is None:
            msg = "Couldn't reach the update server."
        elif not info.is_newer:
            msg = f"You're on the latest version ({info.current_version})."
        else:
            msg = f"Update {info.latest_version} is available — see tray menu."
        try:
            self.platform.notifications.notify("Update check", msg)
        except Exception:
            pass

    def _install_update(self, *_) -> None:
        """User picked "Install update" from the submenu."""
        info = self._update_info
        if info is None:
            return
        target_version = info.latest_version
        try:
            self.platform.notifications.notify(
                "Updating Desktop Connector",
                f"Downloading {target_version}…",
            )
        except Exception:
            pass
        threading.Thread(
            target=self._do_install_update,
            args=(target_version,),
            daemon=True,
        ).start()

    def _do_install_update(self, target_version: str) -> None:
        last_status = ["Starting…"]
        def on_status(line: str) -> None:
            last_status[0] = line
            log.info("update_runner.status %s", line)

        outcome = update_runner.run_update(on_status=on_status)

        if outcome is update_runner.UpdateOutcome.FAILED:
            try:
                self.platform.notifications.notify(
                    "Update failed",
                    f"{last_status[0]} — try again later.",
                )
            except Exception:
                pass
            return

        if outcome is update_runner.UpdateOutcome.NO_CHANGE:
            # Tool ran but the AppImage on disk is byte-identical — the user
            # was already on the latest version (likely manually clicked
            # "Check for updates" / "Install update"). Notify and stay put.
            try:
                self.platform.notifications.notify(
                    "Already up to date",
                    f"Desktop Connector is on the latest version ({target_version}).",
                )
            except Exception:
                pass
            return

        # UPDATED: new bytes on disk at the original path. The running
        # process is still on the OLD content (mmap'd before the swap), so
        # we relaunch from $APPIMAGE and quit. Pairings/history live in
        # ~/.config/desktop-connector/ and survive untouched.
        new_path = os.environ.get("APPIMAGE")
        try:
            self.platform.notifications.notify(
                "Update applied",
                f"Restarting on {target_version}…",
            )
        except Exception:
            pass
        if new_path:
            try:
                subprocess.Popen([new_path], start_new_session=True)
            except Exception:
                log.exception("update_runner.relaunch_failed")
        self._quit()

    def _dismiss_update(self, *_) -> None:
        info = self._update_info
        if info is None:
            return
        version_check.dismiss_version(info.latest_version)
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _open_release_notes(self, *_) -> None:
        info = self._update_info
        if info is None or not info.release_url:
            return
        # Use the same shell open path as "Open Save Folder".
        try:
            subprocess.Popen(["xdg-open", info.release_url], start_new_session=True)
        except Exception:
            log.exception("update_runner.open_url_failed url=%s", info.release_url)
