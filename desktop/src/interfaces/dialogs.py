from __future__ import annotations

from pathlib import Path
from typing import Protocol


class DialogBackend(Protocol):
    def pick_files(self, title: str) -> list[Path]:
        ...

    def save_file(
        self,
        title: str,
        *,
        default_filename: str = "",
        file_types: tuple[tuple[str, str], ...] = (),
    ) -> Path | None:
        ...

    def confirm(self, title: str, message: str) -> bool:
        ...

    def show_info(self, title: str, message: str) -> None:
        ...
