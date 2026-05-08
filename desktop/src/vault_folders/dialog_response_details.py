"""Raw relay-response details dialog used by the Add-folder flow.

Shows the relay's verbatim response body in a scrollable, read-only
``Adw.Dialog`` so users can copy/paste it into a bug report when an
``add_remote_folder`` call surfaces an unexpected error.
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402


def _present_response_details_dialog(
    parent: Gtk.Widget, response_text: str,
) -> None:
    """Show the raw relay response in a scrollable, read-only window."""
    dialog = Adw.Dialog()
    dialog.set_title("Response details")
    dialog.set_content_width(640)
    dialog.set_content_height(420)

    toolbar = Adw.ToolbarView()
    dialog.set_child(toolbar)
    toolbar.add_top_bar(Adw.HeaderBar())

    body = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=8,
        margin_top=12,
        margin_bottom=12,
        margin_start=12,
        margin_end=12,
    )
    toolbar.set_content(body)

    body.append(Gtk.Label(
        label="Raw relay response",
        xalign=0,
        css_classes=["dim-label"],
    ))

    buf = Gtk.TextBuffer()
    buf.set_text(response_text or "(empty)")
    view = Gtk.TextView(
        buffer=buf,
        monospace=True,
        editable=False,
        cursor_visible=False,
    )
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    scroller.set_child(view)
    body.append(scroller)

    dialog.present(parent)
