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

    Drives an Adw progress modal (or terminal print, depending on
    ``sys.stdout`` being a TTY) over the kill / copy / spawn steps so
    GUI launches aren't silent. A desktop notification fires regardless
    so the user gets feedback even without watching the modal.

    Side effects: writes the canonical AppImage file, spawns the
    canonical AppImage as a detached child.
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

    # Kick off a desktop notification immediately so GUI launches see
    # *something* even before the modal renders. notify-send is
    # widely-installed and lingers ~5 s on most desktops.
    _notify(
        "Installing Desktop Connector",
        f"Setting up at {canonical}",
    )

    def steps(set_status):
        return _do_install_steps(running_path, canonical, set_status)

    if sys.stdout.isatty():
        # Terminal launch: print plain status text, no modal.
        success = steps(lambda text: print(f"  [·] {text}", flush=True))
    else:
        # GUI launch: progress modal. Falls back to silent if GTK4 fails.
        success = _show_install_modal(canonical, steps)

    if sys.stdout.isatty():
        if success:
            print(
                f"Installed Desktop Connector at {canonical}.\n"
                "Tray launching in background — look for the icon in your panel.",
                flush=True,
            )
        else:
            print("Installation failed — see logs above.", flush=True)
    return success


def _do_install_steps(running_path: Path, canonical: Path, set_status) -> bool:
    """Kill any existing canonical → copy → spawn. Returns True on success.

    Calls ``set_status(text)`` between steps so a wrapping UI can paint
    progress. Idempotent w.r.t. the kill step (safe even if
    enforce_single_instance has already run).
    """
    set_status("Stopping running Desktop Connector…")
    _stop_other_instances()

    set_status(f"Installing to {canonical.parent}…")
    canonical.parent.mkdir(parents=True, exist_ok=True)

    # Atomic replacement: copy the new bytes into a sibling tmp file,
    # chmod, then `rename` over the canonical. Direct overwrite would
    # trip ETXTBSY ("Text file busy") when the canonical is still
    # mapped by the just-SIGTERM'd process — the kernel forbids opening
    # a running executable for WRITE, but allows unlink/rename. Doing
    # it through a tmp file means there's never a moment where
    # canonical doesn't exist on disk either.
    tmp = canonical.with_name(canonical.name + ".new")
    try:
        shutil.copy2(running_path, tmp)
    except OSError as e:
        log.warning("appimage.relocate.copy_failed error=%s", e)
        set_status(f"Failed: {e}")
        return False
    try:
        tmp.chmod(0o755)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        log.warning("appimage.relocate.chmod_failed error=%s", e)
        set_status(f"Failed: {e}")
        return False
    try:
        tmp.replace(canonical)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        log.warning("appimage.relocate.rename_failed error=%s", e)
        set_status(f"Failed: {e}")
        return False
    try:
        size = canonical.stat().st_size
    except OSError:
        size = -1
    log.info("appimage.relocate.copied bytes=%d", size)

    set_status("Starting Desktop Connector…")
    args = [str(canonical)] + sys.argv[1:]
    spawn_env = _clean_env_for_spawn()
    try:
        # Detach: new session + null FDs so the spawned canonical doesn't
        # keep writing to the user's terminal after we exit.
        subprocess.Popen(
            args,
            env=spawn_env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log.warning("appimage.relocate.spawn_failed error=%s", e)
        set_status(f"Failed to start: {e}")
        return False
    log.info("appimage.relocate.spawned target=%s", canonical)
    set_status("Done.")
    return True


def _notify(title: str, body: str = "") -> None:
    """Best-effort desktop notification via ``notify-send``. Silent if
    notify-send isn't installed or the daemon isn't running. Broad
    except — notifications are decorative and must never crash the
    install path."""
    try:
        cmd = [
            "notify-send",
            "--app-name=Desktop Connector",
            "--icon=desktop-connector",
            title,
        ]
        if body:
            cmd.append(body)
        subprocess.run(cmd, check=False, timeout=2)
    except Exception:  # noqa: BLE001 — defensive on purpose
        pass


def _show_install_modal(canonical: Path, work) -> bool:
    """Pulse-progress Adw modal that drives ``work`` on a worker thread.

    ``work(set_status)`` runs on the worker thread and returns a bool —
    True on success, False on failure. The modal pulses during the
    work, switches to a terminal success/failure state when ``work``
    returns, and waits for the user to dismiss via the Close button.

    The window is non-deletable while ``work`` is running so the user
    can't accidentally interrupt the install. Becomes deletable +
    Close-button-active when ``work`` returns.

    Returns the bool ``work`` returned (or False if the modal couldn't
    load GTK4 — caller decides whether to fall back).
    """
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw, Gio, GLib
    except (ValueError, ImportError):
        log.warning("appimage.relocate.modal_unavailable falling back to silent")
        try:
            return bool(work(lambda _t: None))
        except Exception:
            log.exception("appimage.relocate.fallback_work_failed")
            return False

    import threading

    state = {"done": False, "success": False, "last_status": ""}
    app = Adw.Application(
        application_id="com.desktopconnector.installer",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )

    def on_activate(_app):
        win = Adw.ApplicationWindow(
            application=app,
            title="Install Desktop Connector",
            default_width=460,
            default_height=240,
        )
        win.set_resizable(False)
        # Block window-close while work is running. Re-enabled in finalize().
        win.set_deletable(False)

        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        header = Adw.HeaderBar()
        # Hide the X (and min/max) during work — re-enable in finalize().
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        toolbar.add_top_bar(header)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        toolbar.set_content(outer)

        heading = Gtk.Label(label="Installing Desktop Connector", xalign=0)
        heading.add_css_class("title-2")
        outer.append(heading)

        sub = Gtk.Label(label=str(canonical), xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        outer.append(sub)

        progress = Gtk.ProgressBar()
        outer.append(progress)

        status_label = Gtk.Label(label="Preparing…", xalign=0, wrap=True)
        status_label.add_css_class("caption")
        outer.append(status_label)

        def pulse():
            if state["done"]:
                return False
            progress.pulse()
            return True
        GLib.timeout_add(120, pulse)

        def update_status(text):
            state["last_status"] = text
            GLib.idle_add(status_label.set_text, text)

        def finalize():
            success = state["success"]
            if success:
                heading.set_text("Installation complete")
                progress.set_fraction(1.0)
                status_label.set_text(
                    "Desktop Connector is running in the tray."
                )
            else:
                heading.set_text("Installation failed")
                progress.set_fraction(0.0)
                # Show the last status text (which _do_install_steps sets to
                # the failure reason) so the user knows WHY it failed.
                reason = state["last_status"] or "Unknown error — see logs."
                status_label.set_text(reason)
                status_label.add_css_class("error")
            # Reveal the window's X button + re-enable Esc-to-close.
            header.set_show_end_title_buttons(True)
            win.set_deletable(True)
            return False  # one-shot idle callback

        def worker():
            try:
                state["success"] = bool(work(update_status))
            except Exception:
                log.exception("appimage.relocate.modal_worker_failed")
                state["success"] = False
            finally:
                state["done"] = True
                GLib.idle_add(finalize)

        threading.Thread(target=worker, daemon=True).start()
        win.present()

    app.connect("activate", on_activate)
    app.run([])
    return state["success"]


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
