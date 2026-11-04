"""Clear-all-history button + confirmation dialog.

Pre-split this was the ``on_clear_all`` closure inline in
``on_activate``. It reads ``ctx.selected_device``, calls the toast
helper for the no-device case, and delegates to
``ctx.history.clear_for_peer`` / ``ctx.reset_history_view`` /
``ctx.build_list`` on confirm.
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from ..windows_common import _connected_device_label
from .context import HistoryContext


def on_clear_all(ctx: HistoryContext, b) -> None:
    win = ctx.win
    device = ctx.selected_device[0]
    if device is None:
        ctx.show_toast(win, "No connected device selected")
        return
    device_name = _connected_device_label(device)
    dialog = Adw.MessageDialog(
        transient_for=win,
        heading=f"Clear history for {device_name}?",
        body=(
            f"This will remove visible transfer history entries "
            f"for {device_name}."
        ),
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("clear", "Clear")
    dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)

    def on_response(dlg, response):
        if response == "clear":
            ctx.history.clear_for_peer(
                device.device_id,
                fallback_device_id=device.device_id,
            )
            ctx.reset_history_view()
            ctx.build_list()
    dialog.connect("response", on_response)
    dialog.present()
