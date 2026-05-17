"""Shared folder-detail widget (stats tiles + Local binding section).

Used by both the Vault Settings → Folders tab and the Vault browser's
right pane. Both surfaces want the same Current/Stored/History tiles
and the same Local-binding affordance (Connect when unbound, action
row per binding once bound).

The function appends widgets directly to a caller-supplied ``container``
so the surrounding chrome (page title, scroller, status footer) stays
the responsibility of the caller. It still reads from a
``FoldersContext`` so the binding-row callbacks (sync now, pause,
resume, disconnect, browse) share the same plumbing as the rest of
the Folders tab — the browser supplies a context shim that fulfills
the same read/write contract.
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Pango  # noqa: E402

from .context import FoldersContext
from .data import list_bindings_for_folder
from .dialog_connect_local import open_connect_local_dialog


def append_folder_details(
    container: Gtk.Box,
    ctx: FoldersContext,
    *,
    remote_folder_id: str,
    folder_row: dict,
) -> None:
    """Append the size tiles + Local binding section for one folder."""
    # Stats tiles (F-LT11).
    stats_flow = Gtk.FlowBox(
        selection_mode=Gtk.SelectionMode.NONE,
        homogeneous=True,
        min_children_per_line=1,
        max_children_per_line=3,
        column_spacing=24,
        row_spacing=12,
    )
    for caption, value in (
        ("Current size", folder_row["current"]),
        ("Remote stored", folder_row["stored"]),
        ("History", folder_row["history"]),
    ):
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        caption_label = Gtk.Label(
            label=caption, xalign=0,
            css_classes=["dim-label", "caption"],
        )
        value_label = Gtk.Label(
            label=value, xalign=0,
            css_classes=["title-4"],
        )
        value_label.set_ellipsize(Pango.EllipsizeMode.END)
        tile.append(caption_label)
        tile.append(value_label)
        stats_flow.append(tile)
    container.append(stats_flow)

    # Local binding section (singular — one binding per remote folder).
    bindings_heading = Gtk.Label(
        label="Local binding", xalign=0,
        css_classes=["title-3"], margin_top=10,
    )
    container.append(bindings_heading)

    bindings = list_bindings_for_folder(ctx, remote_folder_id)
    if not bindings:
        connect_btn = Gtk.Button(
            label="Connect with local folder",
            css_classes=["pill", "suggested-action"],
            halign=Gtk.Align.START,
            margin_top=4,
        )
        connect_btn.set_tooltip_text(
            "Bind this remote folder to a local path. Default sync "
            "mode is Backup only (uploads local changes; remote "
            "changes never come down).",
        )
        connect_btn.connect(
            "clicked",
            lambda _b: open_connect_local_dialog(
                ctx, remote_folder_id=remote_folder_id,
            ),
        )
        container.append(connect_btn)
        return

    # Late import — _build_binding_row lives in rows.py which imports
    # from this module path's neighbours; the runtime cycle is broken
    # by deferring the lookup until call time.
    from .rows import _build_binding_row

    bindings_listbox = Gtk.ListBox(
        selection_mode=Gtk.SelectionMode.NONE,
        css_classes=["boxed-list"],
    )
    bindings_listbox.set_size_request(150, -1)
    for row in bindings:
        bindings_listbox.append(_build_binding_row(ctx, row))
    container.append(bindings_listbox)


__all__ = ["append_folder_details"]
