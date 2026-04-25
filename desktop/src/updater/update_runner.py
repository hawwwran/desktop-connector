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
call returns ``UpdateOutcome.FAILED`` with a clear log line if the
bundled tool isn't findable.

The outcome enum distinguishes ``UPDATED`` (new bytes on disk, caller
should relaunch), ``NO_CHANGE`` (we ran but the file didn't change —
user manually clicked "Check for updates" while already on the latest
version), and ``FAILED`` (tool missing, network down, etc.). Without
this distinction, manually checking-while-current would needlessly
notify "Update applied — Restarting" and bounce the tray.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
import re
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


class UpdateOutcome(enum.Enum):
    """What ``run_update`` did. The tray uses this to decide whether to
    notify "Update applied" + relaunch, or "Already up to date" and
    leave the tray running."""

    UPDATED = "updated"      # bytes on disk changed → relaunch
    NO_CHANGE = "no_change"  # tool ran but no update available
    FAILED = "failed"        # tool missing, network down, signature mismatch, …


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


def run_update(*, on_status: ProgressCallback | None = None) -> UpdateOutcome:
    """Invoke ``appimageupdatetool $APPIMAGE`` and report the outcome.

    Returns ``UpdateOutcome.UPDATED`` when the tool exits 0 AND the
    AppImage's bytes on disk actually changed. Returns ``NO_CHANGE``
    when the tool exits 0 but the AppImage is unchanged (the user is
    already on the latest version — appimageupdatetool exits 0 in
    both cases, so we sha256-compare to disambiguate). Returns
    ``FAILED`` for tool-missing / spawn-failed / non-zero exit /
    appimage-missing — the failure mode is in the log + the
    on_status callback's last message.

    Streams each output line to ``on_status`` so the caller can surface
    progress (e.g. notification + tray-menu status text). Output is also
    log.info'd at module level for debuggability when something goes wrong.
    """
    tool = appimageupdatetool_path()
    if tool is None:
        log.warning(
            "update_runner.tool_missing reason=appimageupdatetool_not_bundled"
        )
        if on_status:
            on_status("Update tool not bundled in this AppImage")
        return UpdateOutcome.FAILED

    target = appimage_path()
    if target is None:
        log.warning(
            "update_runner.appimage_missing reason=appimage_env_unset_or_path_gone"
        )
        if on_status:
            on_status("AppImage path not available; cannot update")
        return UpdateOutcome.FAILED

    log.info("update_runner.started target=%s tool=%s", target, tool)
    if on_status:
        on_status("Starting update…")

    # Snapshot the AppImage's sha256 before + after so we can tell
    # "successfully updated" from "no update needed" — appimageupdatetool
    # exits 0 in both cases. Hash cost: ~100ms / 100 MB on SSD; the
    # user clicks Update at most once per release, so the extra second
    # is negligible.
    pre_sha = _file_sha256(target)

    # The bundled appimageupdatetool itself is an AppImage. Most distros
    # have FUSE; if not, --appimage-extract-and-run is appimagetool's own
    # fallback that extracts to /tmp and runs from there. Slower but works
    # without FUSE — passing it unconditionally costs us ~1 s on FUSE-OK
    # systems too, but the user only does this once per release.
    cmd = [str(tool), "--appimage-extract-and-run", str(target)]
    success, new_file = _stream_subprocess(cmd, on_status=on_status)
    if not success:
        return UpdateOutcome.FAILED

    # appimageupdatetool writes the new bytes at the .zsync's `Filename:`
    # header, which is the published asset name (e.g.
    # "desktop-connector-0.2.1-x86_64.AppImage") — not necessarily our
    # canonical install path ("desktop-connector.AppImage"). When the two
    # differ, the running file isn't actually replaced. Detect via the
    # parsed "New file created" path and move it over $APPIMAGE so the
    # install hook's stable path keeps pointing at the current bytes.
    if new_file is not None and new_file.resolve() != target.resolve():
        if not _relocate_new_file(new_file, target, on_status=on_status):
            return UpdateOutcome.FAILED

    post_sha = _file_sha256(target)
    if pre_sha is not None and post_sha is not None and pre_sha == post_sha:
        log.info("update_runner.no_change sha=%s", pre_sha[:12])
        if on_status:
            on_status("Already up to date.")
        return UpdateOutcome.NO_CHANGE

    log.info(
        "update_runner.updated pre_sha=%s post_sha=%s",
        (pre_sha or "?")[:12],
        (post_sha or "?")[:12],
    )
    return UpdateOutcome.UPDATED


def _relocate_new_file(new_file: Path, target: Path, *, on_status: ProgressCallback | None) -> bool:
    """Move ``new_file`` onto ``target``, backing up the current target.

    appimageupdatetool may write to a path different from the running
    AppImage when the published asset name doesn't match the local
    install filename. Linux's open-file semantics let us rename the
    in-use target out from under the running process — its mmap'd
    inode persists for the rest of the session; future launches load
    the new bytes from the canonical path.
    """
    if not new_file.exists():
        log.warning("update_runner.relocate_missing path=%s", new_file)
        if on_status:
            on_status(f"Update tool reported a new file at {new_file} but it's gone")
        return False
    backup = target.with_name(target.name + ".zs-old")
    try:
        if backup.exists():
            backup.unlink()
        target.rename(backup)
        new_file.rename(target)
        target.chmod(target.stat().st_mode | 0o111)
    except OSError as e:
        log.warning("update_runner.relocate_failed error=%s", e)
        if on_status:
            on_status(f"Update downloaded but couldn't replace running file: {e}")
        return False
    log.info(
        "update_runner.relocated from=%s to=%s backup=%s",
        new_file, target, backup,
    )
    return True


def _file_sha256(path: Path) -> str | None:
    """Hex sha256 of ``path``. None on read error (caller treats as
    "couldn't sample" — falls through to UPDATED on the post-side or
    skips the comparison on the pre-side)."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(1 << 20)  # 1 MiB chunks
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        log.warning("update_runner.sha_failed path=%s error=%s", path, e)
        return None


_NEW_FILE_RE = re.compile(r"New file created:\s*(.+)$")


def _stream_subprocess(
    cmd: list[str], *, on_status: ProgressCallback | None
) -> tuple[bool, Path | None]:
    """Run ``cmd``, forward each stdout line to ``on_status`` + log.info.

    Returns ``(success, new_file_path)``. ``new_file_path`` is parsed from
    the tool's "Update successful. New file created: <PATH>" line — the
    caller uses it to relocate when appimageupdatetool wrote to a path
    different from the running AppImage.
    """
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
        return False, None

    assert proc.stdout is not None
    last_line = ""
    new_file: Path | None = None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        log.info("update_runner.out %s", line)
        last_line = line
        if on_status:
            on_status(line)
        m = _NEW_FILE_RE.search(line)
        if m:
            new_file = Path(m.group(1).strip())
    rc = proc.wait()
    log.info("update_runner.finished rc=%d last=%s", rc, last_line)
    if rc != 0 and on_status:
        on_status(f"Update failed (exit {rc})")
    return rc == 0, new_file
