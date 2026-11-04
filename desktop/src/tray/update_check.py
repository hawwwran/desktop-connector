"""In-app updater (P.6b) — AppImage-only.

One check on boot + once every 24 h via ``_update_check_loop``;
``version_check`` itself caches at the same TTL so the network is only
hit when truly due. Manual "Check for updates" forces a fresh probe
and surfaces the result as a notification. "Install update" runs
``update_runner.run_update`` and relaunches from ``$APPIMAGE`` on
success (the live process is mmap'd to the OLD bytes after the swap).

Outside an AppImage every action is a no-op + the menu items are
hidden.
"""

import logging
import os
import subprocess
import threading

from ..updater import update_runner, version_check

log = logging.getLogger(__name__)


class UpdateCheckMixin:
    def _has_pending_update(self) -> bool:
        """Surface "Update available" iff inside an AppImage, network said
        a newer version exists, AND user hasn't dismissed that exact version."""
        if not self._running_appimage:
            return False
        info = self._update_info
        if info is None or not info.is_newer:
            return False
        if version_check.is_version_dismissed(info.latest_version):
            return False
        return True

    def _update_check_loop(self) -> None:
        """Boot + 24-hour periodic update check. Runs in a daemon thread."""
        while not self._should_quit.is_set():
            self._refresh_update_info(force=False)
            # Wait 24 h, but break out promptly on shutdown.
            self._should_quit.wait(timeout=24 * 3600)

    def _refresh_update_info(self, *, force: bool) -> None:
        try:
            info = version_check.check_for_update(force=force)
        except Exception:
            log.exception("update_check.unexpected_error")
            return
        # Only update + repaint if the surfaced state actually changed,
        # to avoid menu flicker.
        prev = self._update_info
        self._update_info = info
        if (prev is None) != (info is None) or (
            info is not None and prev is not None
            and (info.latest_version != prev.latest_version
                 or info.is_newer != prev.is_newer)
        ):
            try:
                self._icon.update_menu()
            except Exception:
                pass
        if info and info.is_newer and not version_check.is_version_dismissed(info.latest_version):
            log.info("update_check.surfaced latest=%s current=%s",
                     info.latest_version, info.current_version)

    def _manual_update_check(self, *_) -> None:
        """User clicked "Check for updates"."""
        try:
            self.platform.notifications.notify(
                "Checking for updates…", "Talking to GitHub.",
            )
        except Exception:
            pass
        # Fire-and-forget; check_for_update has its own timeouts.
        threading.Thread(target=lambda: self._do_manual_check(), daemon=True).start()

    def _do_manual_check(self) -> None:
        self._refresh_update_info(force=True)
        info = self._update_info
        if info is None:
            msg = "Couldn't reach the update server."
        elif not info.is_newer:
            msg = f"You're on the latest version ({info.current_version})."
        else:
            msg = f"Update {info.latest_version} is available — see tray menu."
        try:
            self.platform.notifications.notify("Update check", msg)
        except Exception:
            pass

    def _install_update(self, *_) -> None:
        """User picked "Install update" from the submenu."""
        info = self._update_info
        if info is None:
            return
        target_version = info.latest_version
        try:
            self.platform.notifications.notify(
                "Updating Desktop Connector",
                f"Downloading {target_version}…",
            )
        except Exception:
            pass
        threading.Thread(
            target=self._do_install_update,
            args=(target_version,),
            daemon=True,
        ).start()

    def _do_install_update(self, target_version: str) -> None:
        last_status = ["Starting…"]
        def on_status(line: str) -> None:
            last_status[0] = line
            log.info("update_runner.status %s", line)

        outcome = update_runner.run_update(on_status=on_status)

        if outcome is update_runner.UpdateOutcome.FAILED:
            try:
                self.platform.notifications.notify(
                    "Update failed",
                    f"{last_status[0]} — try again later.",
                )
            except Exception:
                pass
            return

        if outcome is update_runner.UpdateOutcome.NO_CHANGE:
            # Tool ran but the AppImage on disk is byte-identical — the user
            # was already on the latest version (likely manually clicked
            # "Check for updates" / "Install update"). Notify and stay put.
            try:
                self.platform.notifications.notify(
                    "Already up to date",
                    f"Desktop Connector is on the latest version ({target_version}).",
                )
            except Exception:
                pass
            return

        # UPDATED: new bytes on disk at the original path. The running
        # process is still on the OLD content (mmap'd before the swap), so
        # we relaunch from $APPIMAGE and quit. Pairings/history live in
        # ~/.config/desktop-connector/ and survive untouched.
        new_path = os.environ.get("APPIMAGE")
        try:
            self.platform.notifications.notify(
                "Update applied",
                f"Restarting on {target_version}…",
            )
        except Exception:
            pass
        if new_path:
            try:
                subprocess.Popen([new_path], start_new_session=True)
            except Exception:
                log.exception("update_runner.relaunch_failed")
        self._quit()

    def _dismiss_update(self, *_) -> None:
        info = self._update_info
        if info is None:
            return
        version_check.dismiss_version(info.latest_version)
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _open_release_notes(self, *_) -> None:
        info = self._update_info
        if info is None or not info.release_url:
            return
        # Use the same shell open path as "Open Save Folder".
        try:
            subprocess.Popen(["xdg-open", info.release_url], start_new_session=True)
        except Exception:
            log.exception("update_runner.open_url_failed url=%s", info.release_url)
