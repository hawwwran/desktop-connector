"""GTK4 modal + audible alert for incoming locate requests (M.8).

When a paired device asks this desktop "where are you?" with a
non-silent volume, :class:`GtkSubprocessAlert.start` spawns a
``locate-alert`` GTK4 window subprocess and starts a background sound
loop. ``stop()`` terminates both.

The user clicking "Stop" inside the modal causes the subprocess to
exit cleanly; a watcher thread notices the exit and invokes
``on_user_stop`` so the parent's :class:`FindDeviceResponder` can
tear down the rest of the session and send the ``stopped`` heartbeat
back to the requesting device.

This module is best-effort: if no sound player is found, the modal
still appears; if GTK4 fails to launch (no DISPLAY in a headless
session, dependency mismatch), the responder still fires its state
heartbeats so the requester knows we received the command.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("desktop-connector.find-device")

# Candidate sound files. First-existing wins. None of these are
# AppImage-bundled; we rely on system audio assets and silently skip
# the sound loop if none are present.
_SOUND_CANDIDATES = (
    "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
    "/usr/share/sounds/freedesktop/stereo/bell.oga",
    "/usr/share/sounds/sound-icons/percussion-12.wav",
    "/usr/share/sounds/alsa/Front_Center.wav",
)

# Players in order of preference. paplay = PulseAudio, aplay = ALSA,
# play = SoX, mpv = fallback.
_PLAYERS = ("paplay", "aplay", "play", "mpv")


def _find_sound_file() -> str | None:
    for path in _SOUND_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _find_player() -> str | None:
    for name in _PLAYERS:
        path = shutil.which(name)
        if path:
            return path
    return None


class GtkSubprocessAlert:
    """Best-effort always-on-top GTK4 modal plus repeating sound loop.

    Construction is cheap; ``start()`` is the heavy step (launches a
    subprocess + a sound thread). ``stop()`` is idempotent. The
    ``on_user_stop`` callback fires exactly once per session, after
    the modal subprocess exits — whether that's because the user
    clicked Stop, closed the window, or the subprocess crashed.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        on_user_stop: Callable[[], None],
        appimage_path: str | None = None,
        sound_path: str | None = None,
        player_path: str | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._on_user_stop = on_user_stop
        self._appimage_path = appimage_path or os.environ.get("APPIMAGE")
        self._sound_path = sound_path
        self._player_path = player_path

        self._lock = threading.Lock()
        self._modal_proc: subprocess.Popen | None = None
        self._sound_thread: threading.Thread | None = None
        self._sound_stop = threading.Event()
        self._user_stop_fired = False

    def start(self, sender_name: str) -> None:
        with self._lock:
            self._user_stop_fired = False
            self._spawn_modal(sender_name)
            self._spawn_sound_loop()

    def stop(self) -> None:
        with self._lock:
            self._sound_stop.set()
            proc = self._modal_proc
            self._modal_proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        # Don't join the sound thread under lock — the loop's spawn
        # may briefly block, and stop() is sometimes called from the
        # GTK main thread where blocking would hang the UI.

    # --- internals ---------------------------------------------------

    def _spawn_modal(self, sender_name: str) -> None:
        cmd = self._build_locate_alert_command(sender_name)
        try:
            proc = subprocess.Popen(cmd)
        except Exception:
            log.exception("findphone.alert.subprocess_failed sender=%s",
                          sender_name)
            return
        self._modal_proc = proc
        watcher = threading.Thread(
            target=self._watch_modal_exit,
            args=(proc,),
            daemon=True,
            name="find-device-alert-watcher",
        )
        watcher.start()

    def _build_locate_alert_command(self, sender_name: str) -> list[str]:
        if self._appimage_path:
            return [
                self._appimage_path,
                "--gtk-window=locate-alert",
                f"--config-dir={self._config_dir}",
                f"--sender-name={sender_name}",
            ]
        from . import find_device_alert as _self_module
        desktop_root = (
            Path(_self_module.__file__).resolve().parent.parent
        )
        return [
            sys.executable, "-m", "src.windows", "locate-alert",
            f"--config-dir={self._config_dir}",
            f"--sender-name={sender_name}",
        ]

    def _watch_modal_exit(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait()
        except Exception:
            log.debug("findphone.alert.wait_failed", exc_info=True)
        # The modal closed for any reason — user clicked Stop, killed
        # the window, or stop() terminated it. Tell the responder
        # exactly once. If stop() was the cause, on_user_stop's
        # responder.stop() is a no-op (session already cleared).
        with self._lock:
            if self._user_stop_fired:
                return
            self._user_stop_fired = True
        try:
            self._on_user_stop()
        except Exception:
            log.exception("findphone.alert.on_user_stop_failed")

    def _spawn_sound_loop(self) -> None:
        sound = self._sound_path or _find_sound_file()
        player = self._player_path or _find_player()
        if not sound or not player:
            log.info(
                "findphone.alert.sound_skipped reason=%s",
                "no_sound_file" if not sound else "no_player",
            )
            return

        self._sound_stop.clear()

        def loop() -> None:
            log.info(
                "findphone.alert.sound_started player=%s",
                Path(player).name,
            )
            while not self._sound_stop.is_set():
                try:
                    subprocess.run(
                        [player, sound],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                except Exception:
                    log.debug("findphone.alert.sound_failed", exc_info=True)
                    break
            log.info("findphone.alert.sound_stopped")

        thread = threading.Thread(
            target=loop, daemon=True, name="find-device-alert-sound",
        )
        self._sound_thread = thread
        thread.start()
