from __future__ import annotations

from pathlib import Path
from typing import Protocol


class DialogBackend(Protocol):
    def pick_files(self, title: str = "Select files to send") -> list[Path]:
        ...

    def confirm(self, title: str, message: str) -> bool:
        ...

    def show_info(self, title: str, message: str) -> None:
        ...
