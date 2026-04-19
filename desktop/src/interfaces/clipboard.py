from __future__ import annotations

from typing import Protocol


class ClipboardBackend(Protocol):
    """Core-level clipboard capability used by desktop flows."""

    def read_clipboard(self) -> tuple[str, bytes, str] | None:
        ...

    def write_text(self, text: str) -> bool:
        ...

    def write_image(self, data: bytes, mime_type: str = "image/png") -> bool:
        ...
