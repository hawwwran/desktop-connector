"""First-launch system integration for AppImage runs.

When the desktop client runs from inside an AppImage ($APPIMAGE set),
this hook drops the .desktop menu entry and autostart entry so the app
shows up in the user's app menu and auto-launches on login — without
the user ever invoking install.sh.

Idempotency model:
- On first ever launch (config flag absent): create both entries.
- On every launch: rewrite the Exec= line if $APPIMAGE has moved.
  Survives the AppImage being relocated between ~/Downloads,
  ~/Applications, etc.
- Files the user has explicitly removed are NOT recreated. Tracked via
  the `appimage_install_hook_done` flag in config.json: set to true
  after the first successful create pass, never re-checked for absent
  files afterwards.
- Autostart additionally honours `~/.config/desktop-connector/.no-autostart`
  for parity with classic install.sh behaviour.

No-op when $APPIMAGE is unset (dev tree, classic apt-pip install).
File-manager scripts ride on the same hook in P.3c.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..config import Config

log = logging.getLogger("desktop-connector")

APP_NAME = "desktop-connector"
APP_DISPLAY_NAME = "Desktop Connector"
APP_COMMENT = "E2E encrypted file and clipboard sharing"
APP_WM_CLASS = "com.desktopconnector.Desktop"
APP_CATEGORIES = "Network;Utility;"

_HOME = Path.home()
DESKTOP_ENTRY_PATH = _HOME / ".local/share/applications" / f"{APP_NAME}.desktop"
AUTOSTART_ENTRY_PATH = _HOME / ".config/autostart" / f"{APP_NAME}.desktop"

NO_AUTOSTART_MARKER = ".no-autostart"


def ensure_appimage_integration(config: Config) -> None:
    """Install / refresh the AppImage's desktop integration.

    Safe to call on every launch. Returns silently when not running
    inside an AppImage.
    """
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        return

    appimage_path = Path(appimage)
    if not appimage_path.exists():
        log.warning("appimage.install_hook.skipped reason=appimage_path_missing")
        return

    first_run = not config.appimage_install_hook_done
    no_autostart = (config.config_dir / NO_AUTOSTART_MARKER).exists()

    _ensure_desktop_entry(appimage_path, first_run)
    if not no_autostart:
        _ensure_autostart_entry(appimage_path, first_run)

    if first_run:
        config.appimage_install_hook_done = True
        log.info("appimage.install_hook.first_run_complete")


def _desktop_entry_text(appimage_path: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_DISPLAY_NAME}\n"
        f"Comment={APP_COMMENT}\n"
        f"Exec={appimage_path}\n"
        f"Icon={APP_NAME}\n"
        "Terminal=false\n"
        f"Categories={APP_CATEGORIES}\n"
        "StartupNotify=false\n"
        f"StartupWMClass={APP_WM_CLASS}\n"
    )


def _autostart_entry_text(appimage_path: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_DISPLAY_NAME}\n"
        f"Exec={appimage_path}\n"
        f"Icon={APP_NAME}\n"
        "Hidden=false\n"
        "NoDisplay=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        f"StartupWMClass={APP_WM_CLASS}\n"
    )


def _ensure_desktop_entry(appimage_path: Path, first_run: bool) -> None:
    _write_or_update_entry(
        DESKTOP_ENTRY_PATH,
        _desktop_entry_text(appimage_path),
        appimage_path,
        first_run,
        kind="menu_entry",
    )


def _ensure_autostart_entry(appimage_path: Path, first_run: bool) -> None:
    _write_or_update_entry(
        AUTOSTART_ENTRY_PATH,
        _autostart_entry_text(appimage_path),
        appimage_path,
        first_run,
        kind="autostart",
    )


def _write_or_update_entry(
    path: Path, content: str, appimage_path: Path, first_run: bool, *, kind: str
) -> None:
    """Write `content` to `path` on first run, or rewrite if Exec= moved.

    On non-first-run, missing files are NOT recreated — the user has
    removed them deliberately.
    """
    if path.exists():
        try:
            existing = path.read_text()
        except OSError:
            existing = ""
        if _exec_line(existing) != str(appimage_path):
            _atomic_write(path, content)
            log.info("appimage.install_hook.%s.rewritten path=%s", kind, path)
        return

    if not first_run:
        return

    _atomic_write(path, content)
    log.info("appimage.install_hook.%s.created path=%s", kind, path)


def _exec_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("Exec="):
            return line[len("Exec=") :].strip()
    return None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)
