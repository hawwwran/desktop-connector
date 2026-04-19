from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ...interfaces.shell import ShellBackend

log = logging.getLogger(__name__)


class LinuxShellBackend(ShellBackend):
    def open_url(self, url: str) -> bool:
        try:
            subprocess.Popen(["xdg-open", url])
            return True
        except Exception as e:
            log.warning("platform.open_url.failed error_kind=%s", type(e).__name__)
            return False

    def open_folder(self, folder: Path) -> bool:
        try:
            subprocess.Popen(["xdg-open", str(folder)])
            return True
        except Exception as e:
            log.warning("platform.open_folder.failed error_kind=%s", type(e).__name__)
            return False

    def launch_installer_terminal(self, command: str) -> bool:
        terminals = [
            ["konsole", "-e", "bash", "-c", command],
            ["gnome-terminal", "--", "bash", "-c", command],
            ["x-terminal-emulator", "-e", "bash", "-c", command],
        ]
        for args in terminals:
            try:
                subprocess.Popen(args)
                return True
            except FileNotFoundError:
                continue
            except Exception as e:
                log.warning("platform.terminal_launch.failed terminal=%s error_kind=%s", args[0], type(e).__name__)
                continue
        log.warning("platform.terminal_launch.failed reason=no_terminal")
        return False
