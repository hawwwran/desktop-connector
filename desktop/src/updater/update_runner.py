"""Run AppImageUpdate against the live $APPIMAGE (P.6b).

The :command:`appimageupdatetool` CLI ships bundled inside our AppImage at
``$APPDIR/opt/appimageupdate/appimageupdatetool.AppImage``. It reads the
zsync URL embedded in our AppImage at build time (via ``zsyncmake -u …``
in the release workflow), fetches the matching ``.zsync`` file, and
downloads only the missing blocks — typically a few hundred KB to a few
MB even when the full AppImage is ~110 MB.

The running AppImage's file at ``$APPIMAGE`` is replaced in place, with
a ``.zs-old.AppImage`` backup written next to it. The currently-running
process keeps its mmap'd file descriptor on the OLD content; the user
has to relaunch from the new file at the original path to actually run
the new version. P.6b's tray callback handles that hand-off — spawn the
new AppImage as a detached child, then quit.

No GTK / GUI deps here. Progress is exposed as a callback so the tray
(or anything else) can show it however suits — desktop notifications,
status text in the menu, a Gtk progress bar in a subprocess window.
Module is import-safe outside an AppImage; the actual ``run_update``
call returns ``False`` with a clear log line if the bundled tool isn't
findable.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Inside the AppImage, AppRun sets $APPDIR to the FUSE mount root. We
# stash the bundled appimageupdatetool there. Outside an AppImage
# ($APPDIR unset) the runner returns failure — apt-pip and dev-tree
# installs aren't expected to call this path, since P.6a's check_for_update
# returns None for them.
APPIMAGEUPDATETOOL_RELATIVE = "opt/appimageupdate/appimageupdatetool.AppImage"

ProgressCallback = Callable[[str], None]


def appimageupdatetool_path() -> Path | None:
    """Resolve the bundled appimageupdatetool. None if not running inside our AppImage."""
    appdir = os.environ.get("APPDIR")
    if not appdir:
        return None
    candidate = Path(appdir) / APPIMAGEUPDATETOOL_RELATIVE
    return candidate if candidate.exists() else None


def appimage_path() -> Path | None:
    """Resolve $APPIMAGE if set + readable. None means we're not running from
    an AppImage and there's nothing to update in place."""
    p = os.environ.get("APPIMAGE")
    if not p:
        return None
    path = Path(p)
    return path if path.exists() else None


def run_update(*, on_status: ProgressCallback | None = None) -> bool:
    """Invoke ``appimageupdatetool $APPIMAGE`` and stream its output.

    Returns True iff the tool exited 0 AND $APPIMAGE was successfully
    updated in place. False otherwise (tool missing, $APPIMAGE missing,
    network failure, no-update-available, signature mismatch, …) — the
    failure mode is in the log + the on_status callback's last message.

    Streams each output line to ``on_status`` so the caller can surface
    progress (e.g. notification + tray-menu status text). Output is also
    log.info'd at module level for debuggability when something goes wrong.
    """
    tool = appimageupdatetool_path()
    if tool is None:
        msg = "update_runner.tool_missing — appimageupdatetool not bundled"
        log.warning(msg)
        if on_status:
            on_status("Update tool not bundled in this AppImage")
        return False

    target = appimage_path()
    if target is None:
        msg = "update_runner.appimage_missing — $APPIMAGE unset or path gone"
        log.warning(msg)
        if on_status:
            on_status("AppImage path not available; cannot update")
        return False

    log.info("update_runner.started target=%s tool=%s", target, tool)
    if on_status:
        on_status("Starting update…")

    # The bundled appimageupdatetool itself is an AppImage. Most distros
    # have FUSE; if not, --appimage-extract-and-run is appimagetool's own
    # fallback that extracts to /tmp and runs from there. Slower but works
    # without FUSE — passing it unconditionally costs us ~1 s on FUSE-OK
    # systems too, but the user only does this once per release.
    cmd = [str(tool), "--appimage-extract-and-run", str(target)]
    return _stream_subprocess(cmd, on_status=on_status)


def _stream_subprocess(cmd: list[str], *, on_status: ProgressCallback | None) -> bool:
    """Run ``cmd``, forward each stdout line to ``on_status`` + log.info."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        )
    except OSError as e:
        log.warning("update_runner.spawn_failed error=%s", e)
        if on_status:
            on_status(f"Could not start update tool: {e}")
        return False

    assert proc.stdout is not None
    last_line = ""
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        log.info("update_runner.out %s", line)
        last_line = line
        if on_status:
            on_status(line)
    rc = proc.wait()
    log.info("update_runner.finished rc=%d last=%s", rc, last_line)
    if rc != 0 and on_status:
        on_status(f"Update failed (exit {rc})")
    return rc == 0
