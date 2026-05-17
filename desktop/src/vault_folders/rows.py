"""Row builders + detail-pane / sidebar renderers for the Folders tab.

These helpers fully rebuild the relevant subtree on every refresh so
no stale references survive a sync/pause/disconnect cycle. Each
helper takes the shared :class:`FoldersContext` and reads/writes the
state dicts it carries.
"""

from __future__ import annotations

from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Pango  # noqa: E402

from .actions_disconnect import run_disconnect
from .actions_sync import run_pause, run_resume, run_sync_now
from .buttons import _make_overflow_button
from .context import FoldersContext
from .data import list_bindings_for_folder, list_folders
from .dialog_configure_folder import open_configure_folder_dialog


def _build_binding_row(ctx: FoldersContext, row: dict) -> Gtk.Widget:
    """Render one binding as an ``Adw.ActionRow`` with a primary
    action button + an overflow popover for the rest. Lighter than
    the old card-of-pills shape and follows the libadwaita idiom for
    per-row controls.
    """
    bid = row["binding_id"]
    state = row["state"]
    sync_mode = row["sync_mode"]
    local_path = row["local_path"]

    path_basename = Path(local_path).name or local_path
    action_row = Adw.ActionRow(
        title=path_basename,
        subtitle=local_path,
        tooltip_text=local_path,
    )
    action_row.set_subtitle_lines(1)
    action_row.set_title_lines(1)
    action_row.set_use_markup(False)
    # Adw.ActionRow's default natural width forces the whole
    # NavigationSplitView's content pane to stay wide — even though
    # the long path is already ellipsized. Override the minimum so
    # the binding row can shrink with the window.
    action_row.set_size_request(150, -1)

    # Meta strip: don't echo "bound" — every row that survives the
    # ``state != "unbound"`` filter and isn't paused is bound, so
    # the word is dead weight. Paused / needs-preflight surface
    # implicitly through the primary action (Resume / no action).
    meta_parts: list[str] = []
    if sync_mode and sync_mode != "paused":
        meta_parts.append(sync_mode)
    meta_parts.append(f"rev {row['last_synced_revision']}")
    meta_label = Gtk.Label(
        label="  ·  ".join(meta_parts),
        xalign=1,
        css_classes=["dim-label", "caption"],
        valign=Gtk.Align.CENTER,
    )
    action_row.add_suffix(meta_label)

    # Primary action — flat icon, not a pill. Keeps the row visually
    # quiet next to the section header. Tooltips carry the verb.
    if state == "bound" and sync_mode != "paused":
        primary = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        primary.add_css_class("flat")
        primary.set_valign(Gtk.Align.CENTER)
        primary.set_tooltip_text(
            "Sync now — drain pending local changes and push them to "
            "the vault.",
        )
        primary.connect(
            "clicked",
            lambda _b, btn=primary: run_sync_now(ctx, bid, btn),
        )
        action_row.add_suffix(primary)

        secondary_actions: list[tuple[str, str, callable, list[str]]] = []
        if local_path:
            secondary_actions.append((
                "Open in file manager",
                "folder-open-symbolic",
                lambda p=local_path: ctx.open_browse_local(p),
                [],
            ))
        secondary_actions.append((
            "Pause sync",
            "media-playback-pause-symbolic",
            lambda: run_pause(ctx, bid),
            [],
        ))
        secondary_actions.append((
            "Disconnect",
            "user-trash-symbolic",
            lambda: run_disconnect(ctx, bid),
            ["destructive-action"],
        ))
        action_row.add_suffix(_make_overflow_button(secondary_actions))

    elif state == "paused" or sync_mode == "paused":
        primary = Gtk.Button.new_from_icon_name(
            "media-playback-start-symbolic",
        )
        primary.add_css_class("flat")
        primary.set_valign(Gtk.Align.CENTER)
        primary.set_tooltip_text(
            "Resume syncing and drain anything the watcher queued.",
        )
        primary.connect(
            "clicked", lambda _b: run_resume(ctx, bid),
        )
        action_row.add_suffix(primary)

        secondary_actions = []
        if local_path:
            secondary_actions.append((
                "Open in file manager",
                "folder-open-symbolic",
                lambda p=local_path: ctx.open_browse_local(p),
                [],
            ))
        secondary_actions.append((
            "Disconnect",
            "user-trash-symbolic",
            lambda: run_disconnect(ctx, bid),
            ["destructive-action"],
        ))
        action_row.add_suffix(_make_overflow_button(secondary_actions))

    else:
        # ``needs-preflight`` and other transitional states — only
        # the browse affordance makes sense.
        if local_path:
            browse_btn = Gtk.Button.new_from_icon_name(
                "folder-open-symbolic",
            )
            browse_btn.add_css_class("flat")
            browse_btn.set_tooltip_text("Open in file manager")
            browse_btn.set_valign(Gtk.Align.CENTER)
            browse_btn.connect(
                "clicked",
                lambda _b, p=local_path: ctx.open_browse_local(p),
            )
            action_row.add_suffix(browse_btn)
    return action_row


def _build_sidebar_row(ctx: FoldersContext, row: dict) -> Gtk.Widget:
    rfid = row["remote_folder_id"]
    bindings = list_bindings_for_folder(ctx, rfid)

    # Outer horizontal layout: text cluster on the left, overflow
    # menu icon on the right (so the user can configure the folder
    # without opening the detail pane).
    outer = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        margin_top=8, margin_bottom=8,
        margin_start=12, margin_end=8,
    )

    text_cluster = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=2,
        hexpand=True, valign=Gtk.Align.CENTER,
    )
    outer.append(text_cluster)

    title = Gtk.Label(
        label=row["name"], xalign=0,
        ellipsize=Pango.EllipsizeMode.END,
        css_classes=["heading"],
    )
    text_cluster.append(title)

    sub_parts: list[str] = []
    if not bindings:
        sub_parts.append("Not bound")
    elif len(bindings) == 1:
        b = bindings[0]
        base = Path(b["local_path"]).name or b["local_path"]
        sub_parts.append(base)
        if b["state"] == "paused" or b["sync_mode"] == "paused":
            sub_parts.append("paused")
        elif b["state"] == "needs-preflight":
            sub_parts.append("setting up")
    else:
        sub_parts.append(f"{len(bindings)} bindings")

    subtitle = Gtk.Label(
        label="  ·  ".join(sub_parts),
        xalign=0,
        ellipsize=Pango.EllipsizeMode.END,
        css_classes=["dim-label", "caption"],
    )
    text_cluster.append(subtitle)

    # F-LT12: per-row overflow menu so Configure / Delete are
    # reachable without first selecting the folder. The menu
    # button intercepts the click so it doesn't trigger row
    # selection.
    overflow_btn = _make_overflow_button([
        (
            "Configure folder",
            "document-edit-symbolic",
            lambda r=rfid: open_configure_folder_dialog(ctx, r),
            [],
        ),
        (
            "Delete folder",
            "user-trash-symbolic",
            lambda: None,
            ["destructive-action"],
        ),
    ])
    overflow_btn.set_valign(Gtk.Align.CENTER)
    outer.append(overflow_btn)

    listbox_row = Gtk.ListBoxRow(child=outer)
    listbox_row.set_activatable(True)
    return listbox_row


def _build_empty_state() -> Gtk.Widget:
    empty = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=10,
        valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
        hexpand=True, vexpand=True,
    )
    icon = Gtk.Image.new_from_icon_name("folder-symbolic")
    icon.set_pixel_size(64)
    icon.add_css_class("dim-label")
    empty.append(icon)
    empty.append(Gtk.Label(
        label="Select a folder",
        css_classes=["title-2"],
    ))
    empty.append(Gtk.Label(
        label="Pick a folder from the list, or click + to add a new one.",
        css_classes=["dim-label"],
        wrap=True, justify=Gtk.Justification.CENTER,
    ))
    return empty


def render_detail(ctx: FoldersContext) -> None:
    from .tab import clear_box  # local import — avoids cycle at module load
    from .details import append_folder_details

    clear_box(ctx.content_box)
    rfid = ctx.selection_state["folder_id"]
    folder_row = ctx.folder_rows_by_id.get(rfid) if rfid else None

    if folder_row is None:
        ctx.content_page.set_title("Folders")
        ctx.content_box.append(_build_empty_state())
        ctx.content_box.append(ctx.content_status)
        return

    ctx.content_page.set_title(folder_row["name"])
    append_folder_details(
        ctx.content_box, ctx,
        remote_folder_id=rfid, folder_row=folder_row,
    )
    ctx.content_box.append(ctx.content_status)


def refresh_sidebar(ctx: FoldersContext) -> None:
    from .tab import clear_listbox  # local import — avoids cycle at module load
    ctx.suspend_selection_signal["value"] = True
    try:
        clear_listbox(ctx.folder_list)
        ctx.folder_rows_by_id.clear()
        rows = list_folders(ctx)
        for row in rows:
            ctx.folder_rows_by_id[row["remote_folder_id"]] = row
            ctx.folder_list.append(_build_sidebar_row(ctx, row))

        if not ctx.vault_id:
            ctx.set_sidebar_status("No local vault is connected.")
        elif not rows:
            ctx.set_sidebar_status("No remote folders yet — use + to add one.")
        else:
            ctx.set_sidebar_status(f"{len(rows)} folder(s).")

        # Reselect previous selection if still present, else pick the
        # first row so the detail pane is never empty when there are
        # folders to choose from.
        target = ctx.selection_state["folder_id"]
        if target not in ctx.folder_rows_by_id:
            target = next(iter(ctx.folder_rows_by_id), None)
            ctx.selection_state["folder_id"] = target
        if target is not None:
            row_index = list(ctx.folder_rows_by_id).index(target)
            list_row = ctx.folder_list.get_row_at_index(row_index)
            if list_row is not None:
                ctx.folder_list.select_row(list_row)
    finally:
        ctx.suspend_selection_signal["value"] = False
