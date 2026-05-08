"""URL detection + clipboard/file open flow for clicked history rows.

Pre-split these were the three module-top helpers
``_contains_single_url`` / ``_extract_single_url`` / ``on_item_click``
inside ``show_history``. They depend on the toast helper +
clipboard primitives + ``subprocess.Popen("xdg-open", …)``; the URL
detection regex is pure.
"""

from __future__ import annotations

import re as _re
import subprocess
from pathlib import Path
from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from ..clipboard import write_clipboard_image, write_clipboard_text
from .toast import show_toast


_url_re = _re.compile(r'https?://\S+')


def _contains_single_url(text: str) -> bool:
    return len(_url_re.findall(text)) == 1


def _extract_single_url(text: str) -> Optional[str]:
    m = _url_re.findall(text)
    return m[0] if len(m) == 1 else None


def on_item_click(item, win) -> None:
    filename = item.get("filename", "")
    content_path = item.get("content_path", "")
    is_clipboard = filename.startswith(".fn.clipboard")
    label = item.get("display_label", "")

    # Link detection — show open/copy dialog
    if is_clipboard and _contains_single_url(label):
        url = _extract_single_url(label)
        dialog = Adw.MessageDialog(
            transient_for=win,
            heading="Link detected",
            body=url,
        )
        dialog.add_response("copy", "Copy")
        dialog.add_response("open", "Open in Browser")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)

        def on_response(dlg, response):
            if response == "open":
                subprocess.Popen(["xdg-open", url])
            elif response == "copy":
                write_clipboard_text(label)
                show_toast(win, "Copied to clipboard")

        dialog.connect("response", on_response)
        dialog.present()
        return

    if is_clipboard:
        # Push to clipboard
        if content_path and Path(content_path).exists():
            p = Path(content_path)
            if filename.endswith(".text"):
                text = p.read_text(errors="replace")
                if write_clipboard_text(text):
                    show_toast(win, "Copied to clipboard")
                else:
                    show_toast(win, "Failed to copy to clipboard")
            elif filename.endswith(".image"):
                data = p.read_bytes()
                if write_clipboard_image(data):
                    show_toast(win, "Image copied to clipboard")
                else:
                    show_toast(win, "Failed to copy image")
        else:
            # Try using the display label as text content
            label = item.get("display_label", "")
            if label and label != "Clipboard image":
                if write_clipboard_text(label):
                    show_toast(win, "Copied to clipboard")
                else:
                    show_toast(win, "Failed to copy to clipboard")
            else:
                show_toast(win, "Clipboard content no longer available")
    elif content_path and Path(content_path).exists():
        # Open the file
        try:
            subprocess.Popen(["xdg-open", content_path])
        except Exception:
            show_toast(win, "Cannot open file")
    else:
        show_toast(win, "File no longer exists")
