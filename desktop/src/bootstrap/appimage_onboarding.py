"""First-launch GTK4 onboarding for AppImage users (P.4a).

When the desktop client is first launched from an AppImage with no
server URL configured (`server_url` absent from config.json), this
module spawns the GTK4 onboarding dialog as a subprocess so the user
can enter a relay URL and pick an autostart preference before the
tray boots.

Why a subprocess: pystray's `_appindicator` backend force-imports
`Gtk-3.0` at dep-check time (the parent process probes
`__import__("pystray")` to detect missing deps), which then locks
GObject Introspection to GTK 3.0 in the parent. The dialog has to be
GTK4 + libadwaita 1.5+ for visual parity with the rest of the windows,
so it can only run in a fresh process. Same reason all other GTK4
windows (settings, history, send-files, find-phone, pairing) are
subprocesses.

Idempotency / triggering:
- Trigger condition: `$APPIMAGE` set, not `--headless`, AND
  `"server_url"` key absent from config.json. Mirrors install.sh's
  prompt logic (line 262).
- Cancel: leaves config untouched, so re-launch fires the dialog
  again. The tray boots in unconfigured mode (caller treats register
  failure as soft for this code path, so the tray is "usable but
  unconfigured" per the plan).
- Save: writes server_url, optionally drops the .no-autostart marker,
  so the install hook (P.3b) sees the user's choice.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
import sys
from pathlib import Path

import requests

from ..config import Config

log = logging.getLogger("desktop-connector")

NO_AUTOSTART_MARKER = ".no-autostart"
HEALTH_PROBE_TIMEOUT_S = 5.0


class OnboardingResult(enum.Enum):
    NOT_NEEDED = "not_needed"
    SAVED = "saved"
    CANCELLED = "cancelled"


def needs_onboarding(config: Config, *, headless: bool = False) -> bool:
    """True iff this is a first launch from an AppImage with no URL set.

    Headless callers (server-side / scripted runs) skip the dialog —
    no GUI is appropriate; they fail loudly downstream if the URL is
    unset.
    """
    if headless:
        return False
    if not os.environ.get("APPIMAGE"):
        return False
    return "server_url" not in config._data


def run_onboarding_if_needed(
    config: Config, *, headless: bool = False
) -> OnboardingResult:
    """Show the onboarding dialog when triggered, otherwise no-op.

    Returns the user's outcome so the caller can adjust register_device
    behaviour (Cancel → tray-only soft-fail; Save → normal register).
    """
    if not needs_onboarding(config, headless=headless):
        return OnboardingResult.NOT_NEEDED

    log.info("appimage.onboarding.shown")
    _spawn_onboarding_subprocess(config.config_dir)
    config.reload()
    if "server_url" in config._data:
        log.info("appimage.onboarding.saved server_url=%s", config.server_url)
        return OnboardingResult.SAVED
    log.info("appimage.onboarding.cancelled")
    return OnboardingResult.CANCELLED


def commit_onboarding_settings(
    config_dir: Path, *, server_url: str, autostart_enabled: bool
) -> None:
    """Persist the user's onboarding choices.

    Writes ``server_url`` into the config (so subsequent launches skip
    onboarding via the ``"server_url" in config._data`` gate in
    :func:`needs_onboarding`) and toggles the
    ``~/.config/desktop-connector/.no-autostart`` marker file based on
    whether the user wants the AppImage's install hook to drop an
    autostart entry on first launch.

    Extracted from the GTK4 dialog's commit-button closure so it's
    unit-testable without spinning up GTK4.
    """
    config = Config(config_dir)
    config.server_url = server_url
    marker = config_dir / NO_AUTOSTART_MARKER
    if autostart_enabled:
        if marker.exists():
            marker.unlink()
    else:
        marker.touch()


def probe_server(server_url: str) -> bool:
    """GET <server>/api/health and check the response shape.

    Mirrors install.sh:276 — looks for a `{"status": "ok"}` JSON
    response (or the legacy `"ok": true` shape). Any network error
    (DNS, refused, timeout, non-200) returns False.
    """
    url = server_url.rstrip("/") + "/api/health"
    try:
        resp = requests.get(url, timeout=HEALTH_PROBE_TIMEOUT_S)
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return body.get("status") == "ok" or body.get("ok") is True


def _spawn_onboarding_subprocess(config_dir: Path) -> None:
    """Block on the GTK4 onboarding dialog running as a child process.

    Inside an AppImage we re-enter via `$APPIMAGE --gtk-window=onboarding`
    (AppRun dispatches that to `python -m src.windows onboarding`).
    Outside an AppImage, fall back to the dev-tree invocation — useful
    when manually testing the dialog with `python -m src.main` against
    a sandboxed config dir.
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        cmd = [appimage, "--gtk-window=onboarding", f"--config-dir={config_dir}"]
        cwd = None
    else:
        cmd = [
            sys.executable, "-m", "src.windows", "onboarding",
            f"--config-dir={config_dir}",
        ]
        cwd = str(Path(__file__).resolve().parents[2])

    try:
        # 10-min timeout — if the dialog hasn't been dismissed by then,
        # the user has either acted or abandoned. Without a timeout, a
        # GTK4 init stall (Wayland-quirk, locked-down XDG_RUNTIME_DIR,
        # missing fonts that block Pango) blocks the tray boot
        # indefinitely on every fresh-machine launch.
        subprocess.run(cmd, cwd=cwd, check=False, timeout=600)
    except subprocess.TimeoutExpired:
        # subprocess.run has already SIGKILL'd the child by now.
        log.warning("appimage.onboarding.timeout treated_as=cancelled")
    except OSError as e:
        # Spawn failed (missing binary, ENOEXEC). Don't crash the parent —
        # log and let the caller treat it as cancellation.
        log.warning("appimage.onboarding.spawn_failed error=%s", e)
