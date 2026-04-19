from __future__ import annotations

from ...clipboard import read_clipboard, write_clipboard_image, write_clipboard_text
from ...interfaces.clipboard import ClipboardBackend


class LinuxClipboardBackend(ClipboardBackend):
    """Linux clipboard backend using existing wl-*/xclip/xsel helpers."""

    def read_clipboard(self) -> tuple[str, bytes, str] | None:
        return read_clipboard()

    def write_text(self, text: str) -> bool:
        return write_clipboard_text(text)

    def write_image(self, data: bytes, mime_type: str = "image/png") -> bool:
        return write_clipboard_image(data, mime_type)
