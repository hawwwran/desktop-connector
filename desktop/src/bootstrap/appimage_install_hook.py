"""First-launch system integration for AppImage runs.

When the desktop client runs from inside an AppImage ($APPIMAGE set),
this hook drops the .desktop menu entry, autostart entry, and
file-manager "Send to Phone" scripts (Nautilus/Nemo) plus a Dolphin
service menu so the app shows up in the user's app menu, auto-launches
on login, and exposes a right-click action — without the user ever
invoking install.sh.

Idempotency model:
- On first ever launch (config flag absent): create all entries.
- On every launch: rewrite the Exec= line (or embedded APPIMAGE
  constant) if $APPIMAGE has moved. Survives the AppImage being
  relocated between ~/Downloads, ~/Applications, etc.
- Files the user has explicitly removed are NOT recreated. Tracked via
  the `appimage_install_hook_done` flag in config.json: set to true
  after the first successful create pass, never re-checked for absent
  files afterwards.
- Autostart additionally honours `~/.config/desktop-connector/.no-autostart`
  for parity with classic install.sh behaviour.

No-op when $APPIMAGE is unset (dev tree, classic apt-pip install).
"""

from __future__ import annotations

import logging
import os
import shutil
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

# File-manager integration paths (P.3c). Nautilus/Nemo discover scripts
# under ~/.local/share/{nautilus,nemo}/scripts; KDE5 Dolphin reads
# service menus from ~/.local/share/kservices5/ServiceMenus.
NAUTILUS_SCRIPT_PATH = _HOME / ".local/share/nautilus/scripts" / "Send to Phone"
NEMO_SCRIPT_PATH = _HOME / ".local/share/nemo/scripts" / "Send to Phone"
DOLPHIN_SERVICE_PATH = (
    _HOME / ".local/share/kservices5/ServiceMenus" / f"{APP_NAME}-send.desktop"
)

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
    _ensure_file_manager_integrations(appimage_path, first_run)

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
    """Return just the executable path from a `.desktop`'s ``Exec=`` line.

    Strips off any trailing args. Without this, an entry whose Exec is
    e.g. ``Exec={appimage} --headless --send=%f`` (Dolphin service menu)
    would always compare unequal to the bare AppImage path and the
    "unchanged-path" idempotency check would re-rewrite the file on
    every launch.
    """
    for line in text.splitlines():
        if line.startswith("Exec="):
            rest = line[len("Exec=") :].strip()
            return rest.split(None, 1)[0] if rest else ""
    return None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


# --- File-manager integrations (P.3c) --------------------------------------
#
# Auto-generated scripts embed the AppImage path as a string constant.
# We detect "AppImage moved" by grepping for the path in the existing
# script content rather than parsing an Exec= line.

_NAUTILUS_NEMO_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""Send selected files to phone via Desktop Connector (AppImage).

Auto-installed by Desktop Connector's first-launch hook. Reads selected
file paths from NAUTILUS_SCRIPT_SELECTED_FILE_PATHS or
NEMO_SCRIPT_SELECTED_FILE_PATHS, then invokes the AppImage with
--headless --send=<path> for each one.
"""
import os
import subprocess
import sys

APPIMAGE = "{appimage_path}"


def main():
    paths_str = os.environ.get("NAUTILUS_SCRIPT_SELECTED_FILE_PATHS", "")
    if not paths_str.strip():
        paths_str = os.environ.get("NEMO_SCRIPT_SELECTED_FILE_PATHS", "")
    if not paths_str.strip():
        paths = sys.argv[1:]
    else:
        paths = [p for p in paths_str.strip().split("\\n") if p]

    if not paths:
        subprocess.run(["notify-send", "-a", "Desktop Connector", "No files selected"])
        return

    files = [p for p in paths if os.path.isfile(p)]
    folders = [p for p in paths if os.path.isdir(p)]

    for path in files:
        subprocess.Popen([APPIMAGE, "--headless", "--send=" + path])

    if folders:
        word = "folder" if len(folders) == 1 else "folders"
        subprocess.run([
            "notify-send", "-a", "Desktop Connector", "-i", "dialog-warning",
            "Folder transport is not supported",
            f"Skipped {{len(folders)}} {{word}}. Send individual files instead.",
        ])

    if files:
        subprocess.run([
            "notify-send", "-a", "Desktop Connector",
            "Sending to phone",
            f"{{len(files)}} file(s) queued",
        ])


if __name__ == "__main__":
    main()
'''


def _nautilus_nemo_script_text(appimage_path: Path) -> str:
    return _NAUTILUS_NEMO_SCRIPT_TEMPLATE.format(appimage_path=str(appimage_path))


def _dolphin_service_text(appimage_path: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Service\n"
        "ServiceTypes=KonqPopupMenu/Plugin\n"
        "MimeType=application/octet-stream;\n"
        "Actions=sendToPhone\n"
        "\n"
        "[Desktop Action sendToPhone]\n"
        "Name=Send to Phone\n"
        f"Icon={APP_NAME}\n"
        f"Exec={appimage_path} --headless --send=%f\n"
    )


def _ensure_file_manager_integrations(appimage_path: Path, first_run: bool) -> None:
    """Drop Nautilus/Nemo scripts + Dolphin service menu when the matching
    file manager is installed. Same idempotency rules as the .desktop entries.
    """
    if shutil.which("nautilus") is not None:
        _write_or_update_script(
            NAUTILUS_SCRIPT_PATH,
            _nautilus_nemo_script_text(appimage_path),
            appimage_path,
            first_run,
            kind="nautilus_script",
        )
    if shutil.which("nemo") is not None:
        _write_or_update_script(
            NEMO_SCRIPT_PATH,
            _nautilus_nemo_script_text(appimage_path),
            appimage_path,
            first_run,
            kind="nemo_script",
        )
    if shutil.which("dolphin") is not None:
        _write_or_update_entry(
            DOLPHIN_SERVICE_PATH,
            _dolphin_service_text(appimage_path),
            appimage_path,
            first_run,
            kind="dolphin_service",
        )


def _write_or_update_script(
    path: Path, content: str, appimage_path: Path, first_run: bool, *, kind: str
) -> None:
    """Write a Python wrapper script. Same idempotency as the .desktop
    helper above, except path-match is via embedded APPIMAGE constant
    rather than an Exec= line, and the file is chmod +x after writing.
    """
    if path.exists():
        try:
            existing = path.read_text()
        except OSError:
            existing = ""
        if str(appimage_path) not in existing:
            _atomic_write(path, content)
            path.chmod(0o755)
            log.info("appimage.install_hook.%s.rewritten path=%s", kind, path)
        return

    if not first_run:
        return

    _atomic_write(path, content)
    path.chmod(0o755)
    log.info("appimage.install_hook.%s.created path=%s", kind, path)
