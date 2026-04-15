"""
System clipboard read/write.
Supports text and images via xclip/xsel/wl-copy.
"""

import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def _is_wayland() -> bool:
    return os.environ.get("WAYLAND_DISPLAY") is not None


def read_clipboard_text() -> str | None:
    """Read text from system clipboard."""
    try:
        if _is_wayland() and _has_cmd("wl-paste"):
            result = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=5)
        elif _has_cmd("xclip"):
            result = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, timeout=5)
        elif _has_cmd("xsel"):
            result = subprocess.run(["xsel", "--clipboard", "--output"], capture_output=True, timeout=5)
        else:
            log.warning("No clipboard tool found (xclip, xsel, or wl-paste)")
            return None

        if result.returncode == 0 and result.stdout:
            return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        log.exception("Failed to read clipboard")
    return None


def read_clipboard_image() -> bytes | None:
    """Read image from system clipboard as PNG bytes."""
    try:
        if _is_wayland() and _has_cmd("wl-paste"):
            result = subprocess.run(["wl-paste", "--type", "image/png"], capture_output=True, timeout=5)
        elif _has_cmd("xclip"):
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o", "-t", "image/png"],
                capture_output=True, timeout=5,
            )
        else:
            return None

        if result.returncode == 0 and result.stdout and len(result.stdout) > 8:
            return result.stdout
    except Exception:
        log.exception("Failed to read clipboard image")
    return None


def read_clipboard() -> tuple[str, bytes, str] | None:
    """
    Read clipboard content smartly.
    Returns (filename, data, mime_type) or None.
    Uses .fn.clipboard.{text,image} naming convention for special transfer handling.
    """
    # Try image first
    img_data = read_clipboard_image()
    if img_data:
        return (".fn.clipboard.image", img_data, "image/png")

    # Fall back to text
    text = read_clipboard_text()
    if text:
        return (".fn.clipboard.text", text.encode("utf-8"), "text/plain")

    return None


def write_clipboard_text(text: str) -> bool:
    """Write text to system clipboard."""
    try:
        if _is_wayland() and _has_cmd("wl-copy"):
            proc = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
        elif _has_cmd("xclip"):
            proc = subprocess.Popen(["xclip", "-selection", "clipboard", "-i"], stdin=subprocess.PIPE)
        elif _has_cmd("xsel"):
            proc = subprocess.Popen(["xsel", "--clipboard", "--input"], stdin=subprocess.PIPE)
        else:
            log.warning("No clipboard tool found")
            return False

        proc.communicate(input=text.encode("utf-8"), timeout=5)
        return proc.returncode == 0
    except Exception:
        log.exception("Failed to write clipboard text")
        return False


def write_clipboard_image(data: bytes, mime_type: str = "image/png") -> bool:
    """Write image to system clipboard."""
    try:
        if _is_wayland() and _has_cmd("wl-copy"):
            proc = subprocess.Popen(["wl-copy", "--type", mime_type], stdin=subprocess.PIPE)
        elif _has_cmd("xclip"):
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard", "-i", "-t", mime_type],
                stdin=subprocess.PIPE,
            )
        else:
            log.warning("No clipboard tool found for image")
            return False

        proc.communicate(input=data, timeout=5)
        return proc.returncode == 0
    except Exception:
        log.exception("Failed to write clipboard image")
        return False


def write_clipboard_file(filepath: Path) -> bool:
    """
    Write file content to clipboard based on its type.
    Images go to clipboard as image, text files as text.
    """
    mime, _ = mimetypes.guess_type(filepath.name)

    if mime and mime.startswith("image/"):
        data = filepath.read_bytes()
        return write_clipboard_image(data, mime)
    elif mime and mime.startswith("text/"):
        text = filepath.read_text(errors="replace")
        return write_clipboard_text(text)
    else:
        # For other types, copy the file path as text
        return write_clipboard_text(str(filepath))
