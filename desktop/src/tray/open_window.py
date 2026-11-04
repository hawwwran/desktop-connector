"""GTK4 window launchers + small "open external thing" actions.

GTK4 windows run as subprocesses to avoid the GTK3/4 conflict (pystray
loads GTK3). Inside an AppImage we re-enter via ``$APPIMAGE`` so the
child gets the bundled GTK4 / libadwaita / WebKitGTK stack and
survives the parent's FUSE mount lifetime.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_DESKTOP_DIR = Path(__file__).resolve().parents[2]


class OpenWindowMixin:
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

    def _pair(self, *_) -> None:
        self._open_gtk4_window("pairing")

    def _open_folder(self, *_) -> None:
        if self.platform.shell.open_folder(self.save_dir):
            log.info("platform.open_folder.succeeded")
