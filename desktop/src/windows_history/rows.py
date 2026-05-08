"""Row builders for the History window.

``create_row`` returns ``(card, row, progress_bar_or_None)``. Card is
a vertically-oriented ``Gtk.Box`` styled as an Adw card with a single
``Adw.ActionRow`` inside an inner ``Gtk.ListBox`` plus an optional
flush ``Gtk.ProgressBar`` clipped to the card's rounded corners by
``Gtk.Overflow.HIDDEN``.

``update_row`` updates an existing row's subtitle + progress-bar
state in-place — used by ``build_list``'s diff path so adding /
removing one item doesn't tear down and recreate every other card.
"""

from __future__ import annotations

import mimetypes as _mt
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from ..brand import apply_pointer_cursors
from ..windows_common import format_size
from .context import HistoryContext
from .delete_row import on_delete as _on_delete
from .status import compute_status as _compute_status
from .url_helpers import _contains_single_url, on_item_click


def create_row(ctx: HistoryContext, item):
    """Create a new card widget (Box with rounded card styling containing a single-row ListBox + optional flush ProgressBar)."""
    history = ctx.history
    direction_prefix = "↓" if item["direction"] == "received" else "↑"
    label = history.get_label(item)
    size = format_size(item.get("size", 0))
    ts = time.strftime("%b %d, %H:%M", time.localtime(item.get("timestamp", 0)))
    status_text, bar_state = _compute_status(item)

    # overflow HIDDEN clips the flush progress bar to the card's rounded corners.
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    card.add_css_class("card")
    card.add_css_class("transfer-card")
    card.set_overflow(Gtk.Overflow.HIDDEN)

    inner_list = Gtk.ListBox()
    inner_list.add_css_class("transfer-card-list")
    inner_list.set_selection_mode(Gtk.SelectionMode.NONE)
    card.append(inner_list)

    # The label is user-controlled (filename / clipboard URL) and
    # set_use_markup is enabled below for the subtitle's coloured
    # status span. Pango parses `&` as an entity start, so URLs
    # like `?v=...&is=...` trip an entity parse error and render
    # an empty title. Escape before interpolation. The direction
    # prefix is a fixed arrow glyph, safe as-is.
    escaped_label = GLib.markup_escape_text(label) if label else ""
    row = Adw.ActionRow(
        title=f"{direction_prefix}  {escaped_label}",
        subtitle=f"{size}  ·  {ts}  ·  {status_text}",
    )
    row.set_title_lines(1)
    # Subtitle carries Pango markup for the status fragment
    # ('Failed' orange / 'Delivered' blue). Size and ts are safe
    # strings (no '<' or '&') so enabling markup is fine.
    row.set_subtitle_lines(1)
    try:
        row.set_use_markup(True)
    except Exception:
        pass

    # Thumbnail or icon as prefix
    content_path = item.get("content_path", "")
    filename = item.get("filename", "")
    mime, _ = _mt.guess_type(filename)
    if not mime and content_path:
        mime, _ = _mt.guess_type(content_path)
    thumb_widget = None
    is_clipboard = item.get("filename", "").startswith(".fn.clipboard")
    is_clipboard_image = filename.endswith(".fn.clipboard.image")
    has_existing_content = bool(content_path and Path(content_path).exists())
    can_thumbnail = (
        has_existing_content
        and (
            is_clipboard_image
            or bool(mime and (mime.startswith("image/") or mime.startswith("video/")))
        )
    )

    if can_thumbnail:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(content_path, 100, 100, True)
            w, h = pixbuf.get_width(), pixbuf.get_height()
            s = min(w, h)
            cropped = pixbuf.new_subpixbuf((w - s) // 2, (h - s) // 2, s, s)
            scaled = cropped.scale_simple(50, 50, GdkPixbuf.InterpType.BILINEAR)
            texture = Gdk.Texture.new_for_pixbuf(scaled)
            img = Gtk.Picture.new_for_paintable(texture)
            img.set_size_request(50, 50)
            img.set_content_fit(Gtk.ContentFit.COVER)
            frame = Gtk.Frame()
            frame.set_size_request(50, 50)
            frame.set_child(img)
            frame.set_overflow(Gtk.Overflow.HIDDEN)
            thumb_widget = frame
        except Exception:
            pass

    is_link = is_clipboard and _contains_single_url(label)

    if thumb_widget is None:
        if is_link:
            icon = Gtk.Image.new_from_icon_name("web-browser-symbolic")
        elif is_clipboard_image:
            icon = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
        elif is_clipboard:
            icon = Gtk.Image.new_from_icon_name("edit-paste-symbolic")
        elif mime and mime.startswith("image/"):
            icon = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
        elif mime and mime.startswith("video/"):
            icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
        elif mime and mime.startswith("text/"):
            icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        else:
            icon = Gtk.Image.new_from_icon_name("document-open-symbolic")
        icon.set_pixel_size(22)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        frame = Gtk.Frame()
        frame.set_size_request(50, 50)
        frame.set_child(icon)
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        thumb_widget = frame

    thumb_widget.set_margin_start(0)
    row.add_prefix(thumb_widget)

    # Delete button
    del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
    del_btn.set_valign(Gtk.Align.CENTER)
    del_btn.add_css_class("flat")
    del_btn.add_css_class("circular")
    del_btn.add_css_class("brand-icon-destructive")
    captured_item = item

    def on_delete(b, it=captured_item, c=card):
        _on_delete(ctx, b, it, c)
    del_btn.connect("clicked", on_delete)
    row.add_suffix(del_btn)

    tid = item.get("transfer_id")
    ts = item.get("timestamp")

    def _current_item(captured=item, _tid=tid, _ts=ts):
        # Re-fetch from history so content_path set after the row was
        # created (e.g. download completing) is picked up on click.
        for h in history.items:
            if _tid and h.get("transfer_id") == _tid:
                return h
            if not _tid and h.get("timestamp") == _ts:
                return h
        return captured
    row.set_activatable(True)
    row.connect("activated", lambda r: on_item_click(_current_item(), ctx.win))

    inner_list.append(row)

    # Progress bar — flush to card edges; card's overflow:hidden clips to rounded corners.
    # When present, it replaces the card's bottom padding (has-progress class drops padding-bottom to 0).
    show, bar_cls, fraction = bar_state
    progress_bar = None
    if show:
        progress_bar = Gtk.ProgressBar()
        progress_bar.set_fraction(fraction)
        for cls in bar_cls.split():
            progress_bar.add_css_class(cls)
        card.append(progress_bar)
        card.add_css_class("has-progress")

    # History rows are built after the window's on_activate has
    # already applied cursors — paint pointer on this freshly
    # constructed subtree too (mainly catches the per-row trash
    # button).
    apply_pointer_cursors(card)
    return card, row, progress_bar


def update_row(item, row, old_progress_bar, parent_box):
    """Update an existing row in-place (subtitle + progress bar)."""
    size = format_size(item.get("size", 0))
    ts = time.strftime("%b %d, %H:%M", time.localtime(item.get("timestamp", 0)))
    status_text, bar_state = _compute_status(item)
    row.set_subtitle(f"{size}  ·  {ts}  ·  {status_text}")

    show, bar_cls, fraction = bar_state

    if show:
        if old_progress_bar:
            old_progress_bar.set_fraction(fraction)
            # Update CSS classes
            for cls in ("upload-bar", "download-bar", "delivery-bar", "pulse-bar"):
                if cls in (bar_cls or ""):
                    old_progress_bar.add_css_class(cls)
                else:
                    old_progress_bar.remove_css_class(cls)
            return old_progress_bar
        else:
            progress_bar = Gtk.ProgressBar()
            progress_bar.set_fraction(fraction)
            for cls in bar_cls.split():
                progress_bar.add_css_class(cls)
            parent_box.append(progress_bar)
            parent_box.add_css_class("has-progress")
            return progress_bar
    else:
        if old_progress_bar:
            parent_box.remove(old_progress_bar)
            parent_box.remove_css_class("has-progress")
        return None
