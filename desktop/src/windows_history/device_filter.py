"""Connected-device picker helpers for the History window.

Pre-split these were five small closures defined inline in
``on_activate`` (``_selected_device_id``, ``_selected_device_name``,
``_empty_history_text``, ``_reset_history_view``,
``on_history_device_changed``). They all read from / write to
``ctx.selected_device`` and the structural / progress signature
slots; lifting them keeps them adjacent to the picker.
"""

from __future__ import annotations

from ..windows_common import _connected_device_label
from .context import HistoryContext


def _selected_device_id(ctx: HistoryContext) -> str:
    device = ctx.selected_device[0]
    return device.device_id if device is not None else ""


def _selected_device_name(ctx: HistoryContext) -> str:
    device = ctx.selected_device[0]
    if device is None:
        return "connected devices"
    return _connected_device_label(device)


def _empty_history_text(ctx: HistoryContext) -> str:
    if ctx.selected_device[0] is None:
        return "No connected devices"
    return f"No transfers with {_selected_device_name(ctx)}"


def _reset_history_view(ctx: HistoryContext) -> None:
    ctx.structural_sig[0] = None
    ctx.progress_sig[0] = None


def on_history_device_changed(ctx: HistoryContext, combo, _pspec) -> None:
    ctx.clear_all_btn.set_sensitive(ctx.selected_device[0] is not None)
    _reset_history_view(ctx)
    ctx.build_list()
