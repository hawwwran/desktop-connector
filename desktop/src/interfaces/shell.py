from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ShellBackend(Protocol):
    def open_url(self, url: str) -> bool:
        ...

    def open_folder(self, folder: Path) -> bool:
        ...

    def open_path(self, path: Path) -> bool:
        ...

    def launch_installer_terminal(self, command: str) -> bool:
        ...
