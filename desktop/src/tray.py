"""
System tray icon with status menu.
GTK4 windows run as subprocesses to avoid GTK3/4 conflict (pystray loads GTK3).
"""

import base64
import logging
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image, ImageDraw

from .api_client import ApiClient
from .config import Config
from .connection import ConnectionManager, ConnectionState
from .crypto import KeyManager
from .history import TransferHistory
from .interfaces.backends import DesktopBackends
from .poller import Poller

log = logging.getLogger(__name__)

_DESKTOP_DIR = Path(__file__).parent.parent


_ASSETS_DIR = Path(__file__).parent.parent / "assets"


def _load_icons() -> dict[str, Image.Image]:
    """Load all icon variants eagerly at import time. Thread-safe after init."""
    icons = {}
    for color in ("green", "green_yellow", "green_red", "red", "yellow", "blue"):
        png_path = _ASSETS_DIR / f"icon_{color}.png"
        if png_path.exists():
            img = Image.open(png_path)
            img.load()  # Force full pixel read (Image.open is lazy)
            icons[color] = img
    # Generate fallbacks for any missing icons
    for color, rgb in (("green", (34, 197, 94)), ("red", (239, 68, 68)),
                        ("yellow", (245, 158, 11)), ("blue", (59, 130, 246))):
        if color not in icons:
            size = 128
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse([16, 16, size - 16, size - 16], fill=rgb)
            icons[color] = img
    return icons


_icons = _load_icons()


def _make_icon(color: str) -> Image.Image:
    """Return a copy of a pre-loaded icon. Safe to call from any thread."""
    img = _icons.get(color) or _icons.get("green")
    return img.copy()


class TrayApp:

    def __init__(self, connection: ConnectionManager, poller: Poller,
                 api: ApiClient, config: Config, crypto: KeyManager,
                 history: TransferHistory, save_dir: Path,
                 backends: DesktopBackends):
        self.conn = connection
        self.poller = poller
        self.api = api
        self.config = config
        self.crypto = crypto
        self.history = history
        self.save_dir = save_dir
        self.backends = backends
        self._icon = None
        self._should_quit = threading.Event()
        self._was_uploading = False
        self._remote_online = False
        self._fcm_available = False
        self._fcm_checked = False

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
                    "Force Reconnect",
                    self._try_now,
                    visible=lambda _: self.conn.state != ConnectionState.CONNECTED,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Send Files...", self._send_files, visible=lambda _: self.config.is_paired),
                pystray.MenuItem("Send Clipboard", self._send_clipboard, visible=lambda _: self.config.is_paired),
                pystray.MenuItem("Find my Phone", self._find_phone,
                                 visible=lambda _: self.config.is_paired and self._fcm_available),
                pystray.MenuItem("Show History", self._show_history),
                pystray.MenuItem("Open Save Folder", self._open_folder),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Pair...", self._pair, visible=lambda _: not self.config.is_paired),
                pystray.MenuItem("Settings...", self._show_settings),
                pystray.MenuItem("Quit", self._quit),
            )

        self._icon = pystray.Icon(
            "desktop-connector",
            icon=self._get_state_icon(),
            title="Desktop Connector",
            menu=build_menu(),
        )

        self.conn.on_state_change(lambda state: self._update_icon())

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

                uploading = self._upload_status_file.exists()
                if uploading != self._was_uploading:
                    self._was_uploading = uploading
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

        self._icon.run()

    def stop(self) -> None:
        self._should_quit.set()
        if self._icon:
            self._icon.stop()

    def _get_state_icon(self) -> Image.Image:
        state = self.conn.state
        if state == ConnectionState.DISCONNECTED:
            return _make_icon("red")
        if self._was_uploading:
            return _make_icon("blue")
        if state == ConnectionState.RECONNECTING:
            return _make_icon("yellow")
        if not self._remote_online:
            return _make_icon("green_yellow")
        return _make_icon("green")

    def _update_icon(self) -> None:
        if self._icon:
            try:
                self._icon.icon = self._get_state_icon()
            except Exception:
                pass

    def _status_text(self) -> str:
        """Menu title text. Side effect: triggers a fresh ping when the menu
        is rendered and our last probe is older than 30s."""
        if self.conn.state != ConnectionState.CONNECTED:
            return "Offline"
        self._maybe_ping(30.0)
        return "Online"

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
        subprocess.Popen(
            [sys.executable, "-m", "src.windows", window_name,
             f"--config-dir={self.config.config_dir}"],
            cwd=str(_DESKTOP_DIR),
        )

    def _send_files(self, *_) -> None:
        self._open_gtk4_window("send-files")

    def _show_settings(self, *_) -> None:
        self._open_gtk4_window("settings")

    def _show_history(self, *_) -> None:
        self._open_gtk4_window("history")

    def _find_phone(self, *_) -> None:
        self._open_gtk4_window("find-phone")

    # --- Pairing ---

    def _pair(self, *_) -> None:
        self._open_gtk4_window("pairing")

    # --- Send clipboard ---

    def _send_clipboard(self, *_) -> None:
        threading.Thread(target=self._do_send_clipboard, daemon=True).start()

    def _do_send_clipboard(self) -> None:
        result = self.backends.clipboard.read_clipboard()
        if result is None:
            self.backends.notifications.notify("Clipboard empty", "Nothing to send")
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
            self.backends.notifications.notify("Clipboard sent", preview)
            # Upload logic cleans up its own progress fields; delivery tracker owns recipient_* from here.
            self.history.update(tid, status="complete", chunks_downloaded=0, chunks_total=0)
        else:
            if progress_tid[0]:
                self.history.update(progress_tid[0], status="failed")
            self.backends.notifications.notify("Send failed", "Could not send clipboard")

    # --- Misc ---

    def _open_folder(self, *_) -> None:
        if self.backends.shell.open_folder(self.save_dir):
            log.info("platform.open_folder.succeeded")

    def _try_now(self, *_) -> None:
        self.conn.try_now()
        self.poller.wake()

    def _quit(self, *_) -> None:
        self.poller.stop()
        self.stop()
