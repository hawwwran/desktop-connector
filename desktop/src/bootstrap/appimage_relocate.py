"""Self-install + single-instance enforcement on AppImage launch.

Two related guarantees, applied at every persistent-mode launch:

  1. **Single instance** — :func:`enforce_single_instance` SIGTERMs every
     other Desktop Connector process running anywhere on the machine
     (AppImage at any path, install-from-source layout, dev-tree run).
     The current process is the survivor.

  2. **Self-install** — :func:`relocate_to_canonical_if_needed` copies
     the running AppImage to
     ``~/.local/share/desktop-connector/desktop-connector.AppImage``
     when launched from anywhere else, then spawns the canonical copy
     with the same argv tail and signals the caller to exit. Running
     the AppImage from anywhere ends up with a single canonical
     install + tray.

Both functions skip in transient modes (``--send`` / ``--pair`` /
``--version``) — those are short-lived ops that shouldn't kill the
tray that spawned them.

Trust note: signature verification belongs to ``install.sh``. By the
time the AppImage is *executing*, the user has already trusted it. The
self-relocation is a UX nicety, not a security gate.

Opt out with ``DC_NO_RELOCATE=1`` to skip the copy-to-canonical step;
single-instance enforcement still fires.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("desktop-connector")

CANONICAL_INSTALL_DIR = Path.home() / ".local/share/desktop-connector"
CANONICAL_APPIMAGE_PATH = CANONICAL_INSTALL_DIR / "desktop-connector.AppImage"

# Env vars AppRun + AppImage runtime set/extend with paths into the running
# AppImage's FUSE mount (/tmp/.mount_*/). When we spawn the canonical
# AppImage from inside a non-canonical run, the running mount is about to
# unmount — anything referencing those paths in the spawned child's env will
# error at import time. Sanitize before spawn.
_PATH_LIKE_VARS = (
    "PATH",
    "PYTHONPATH",
    "LD_LIBRARY_PATH",
    "GI_TYPELIB_PATH",
    "GSETTINGS_SCHEMA_DIR",
    "XDG_DATA_DIRS",
)
_SINGLE_PATH_VARS = (
    "APPDIR",
    "APPIMAGE",
    "ARGV0",
    "OWD",
    "PYTHONHOME",
    "GDK_PIXBUF_MODULE_FILE",
    "WEBKIT_EXEC_PATH",
)


def is_persistent_mode() -> bool:
    """True iff this invocation is meant to be the running app.

    --send and --pair are transient (file-manager 'Send to Phone' calls,
    pairing window spawned by the tray) and shouldn't kill the running
    tray or relocate the binary. --version short-circuits before us.
    """
    args = sys.argv[1:]
    for a in args:
        if a == "--send" or a.startswith("--send="):
            return False
        if a == "--pair":
            return False
    return True


def enforce_single_instance() -> None:
    """SIGTERM every other Desktop Connector process on the machine.

    Matches python ``-m src.main`` processes whose ``$APPIMAGE`` env or
    cwd contains the string ``desktop-connector`` — covers AppImage
    runs at any path (canonical / temp build / ~/Downloads), the
    install-from-source.sh layout (cwd inside ``~/.local/share/desktop-connector``),
    and dev-tree runs from a checkout under ``…/desktop-connector/…``.

    No-op in transient modes (--send / --pair). Skips self + parent so
    the AppImage runtime wrapper holding our FUSE mount survives.
    """
    if not is_persistent_mode():
        return
    _stop_other_instances()


def relocate_to_canonical_if_needed() -> bool:
    """Return True iff the caller should ``return 0`` (relocation happened).

    Side effects: writes the canonical AppImage file, spawns the
    canonical AppImage as a detached child. Caller is expected to have
    already invoked :func:`enforce_single_instance`.
    """
    if not is_persistent_mode():
        return False
    if os.environ.get("DC_NO_RELOCATE"):
        return False
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        return False
    try:
        running_path = Path(appimage).resolve()
    except OSError:
        return False
    canonical = _resolve_canonical()
    if running_path == canonical:
        return False

    log.info(
        "appimage.relocate.detected from=%s to=%s", running_path, canonical
    )

    canonical.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(running_path, canonical)
    except OSError as e:
        log.warning("appimage.relocate.copy_failed error=%s", e)
        return False
    try:
        canonical.chmod(0o755)
    except OSError as e:
        log.warning("appimage.relocate.chmod_failed error=%s", e)
        return False
    try:
        size = canonical.stat().st_size
    except OSError:
        size = -1
    log.info("appimage.relocate.copied bytes=%d", size)

    args = [str(canonical)] + sys.argv[1:]
    spawn_env = _clean_env_for_spawn()
    try:
        subprocess.Popen(args, env=spawn_env, start_new_session=True)
    except OSError as e:
        log.warning("appimage.relocate.spawn_failed error=%s", e)
        return False
    log.info("appimage.relocate.spawned target=%s", canonical)
    return True


def _clean_env_for_spawn() -> dict:
    """Strip ``/tmp/.mount_*/`` entries from path-like vars and clear
    single-value vars that point into this AppImage's FUSE mount.

    The about-to-be-spawned canonical AppImage's runtime + AppRun
    re-populate everything correctly from its own mount. We just need
    to make sure stale references don't leak through and crash Python's
    ``init_fs_encoding`` step (which scans PYTHONPATH at startup).
    """
    env = dict(os.environ)

    for var in _PATH_LIKE_VARS:
        val = env.get(var)
        if not val:
            continue
        kept = [p for p in val.split(":") if p and not p.startswith("/tmp/.mount_")]
        if kept:
            env[var] = ":".join(kept)
        else:
            env.pop(var, None)

    for var in _SINGLE_PATH_VARS:
        val = env.get(var, "")
        # APPDIR/APPIMAGE/ARGV0/OWD are always set by the AppImage runtime
        # to mount paths; the others may or may not be — clear if so.
        if var in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):
            env.pop(var, None)
        elif val.startswith("/tmp/.mount_"):
            env.pop(var, None)

    return env


def _resolve_canonical() -> Path:
    # `.resolve()` swallows OSError when the path doesn't exist on older
    # Pythons; we want a stable string regardless.
    try:
        return CANONICAL_APPIMAGE_PATH.resolve(strict=False)
    except OSError:
        return CANONICAL_APPIMAGE_PATH


_PROJECT_NEEDLE = "desktop-connector"


def _stop_other_instances() -> None:
    """SIGTERM every running ``python -m src.main`` whose env or cwd marks
    it as a Desktop Connector process. Skips self and parent.

    Match criteria (any one suffices):
      - ``APPIMAGE=`` env value contains "desktop-connector"
      - ``/proc/<pid>/cwd`` link contains "desktop-connector"

    The "python -m src.main" cmdline filter avoids broadly matching
    every python process; the env/cwd substring then narrows to ours.
    """
    self_pid = str(os.getpid())
    parent_pid = str(os.getppid())
    stopped = 0
    proc = Path("/proc")
    if not proc.is_dir():
        return

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        if entry.name in (self_pid, parent_pid):
            continue
        # Cmdline filter: only target python processes running -m src.main.
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if b"src.main" not in cmdline:
            continue

        if not _process_is_ours(entry):
            continue

        try:
            os.kill(int(entry.name), signal.SIGTERM)
            stopped += 1
        except OSError:
            pass

    if stopped:
        log.info("appimage.relocate.stopped_other_instances count=%d", stopped)
        # Give the FUSE mount + child python a beat to wind down before we
        # potentially overwrite the canonical file. Otherwise the in-flight
        # process might still hold an exclusive open on it.
        time.sleep(1)


def _process_is_ours(entry: Path) -> bool:
    """True if /proc/<pid> looks like a Desktop Connector process —
    by APPIMAGE env or by cwd containing the project marker."""
    # APPIMAGE env match (covers AppImage at any path)
    try:
        environ = (entry / "environ").read_bytes()
    except (OSError, PermissionError):
        environ = b""
    for line in environ.split(b"\x00"):
        if line.startswith(b"APPIMAGE=") and _PROJECT_NEEDLE.encode() in line:
            return True

    # cwd substring match (covers source-tree + dev-tree runs)
    try:
        cwd = os.readlink(entry / "cwd")
    except (OSError, PermissionError):
        cwd = ""
    if _PROJECT_NEEDLE in cwd:
        return True
    return False
