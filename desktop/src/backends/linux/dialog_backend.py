from __future__ import annotations

from pathlib import Path

from ...dialogs import confirm, pick_files, save_file, show_info
from ...interfaces.dialogs import DialogBackend


class LinuxDialogBackend(DialogBackend):
    def pick_files(self, title: str = "Select files to send") -> list[Path]:
        return pick_files(title)

    def save_file(
        self,
        title: str,
        *,
        default_filename: str = "",
        file_types: tuple[tuple[str, str], ...] = (),
    ) -> Path | None:
        return save_file(
            title,
            default_filename=default_filename,
            file_types=file_types,
        )

    def confirm(self, title: str, message: str) -> bool:
        return confirm(title, message)

    def show_info(self, title: str, message: str) -> None:
        show_info(title, message)
