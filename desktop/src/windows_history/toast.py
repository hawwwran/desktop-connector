"""Toast helper for the History window."""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402


def show_toast(win, message: str) -> None:
    toast = Adw.Toast(title=message, timeout=2)
    toast_overlay = win.get_content()
    # Find the toast overlay
    if hasattr(toast_overlay, 'add_toast'):
        toast_overlay.add_toast(toast)
