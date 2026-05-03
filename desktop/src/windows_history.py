"""Transfer History window (`python -m src.windows history`)."""

import threading
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GdkPixbuf, GLib

from .brand import (
    DC_BLUE_400,
    DC_BLUE_500,
    DC_ORANGE_700,
    DC_YELLOW_500,
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .windows_common import (
    _connected_device_label,
    _create_device_picker,
    _make_app,
    format_size,
)


def show_history(config_dir: Path):
    import subprocess
    from .config import Config
    from .history import TransferHistory, TransferStatus
    from .clipboard import write_clipboard_text, write_clipboard_image
    from .api_client import ApiClient
    from .connection import ConnectionManager
    from .crypto import KeyManager

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    history = TransferHistory(config_dir)

    from .api_client import STORAGE_FULL_MAX_WINDOW_S

    def _scrub_zombie_waiting() -> None:
        """Flip any orphaned waiting row to 'failed' with
        failure_reason='quota_timeout'. A row is orphaned if it's
        been in waiting state longer than STORAGE_FULL_MAX_WINDOW_S
        (30 min) — beyond the retry budget of any still-live send
        subprocess. Called at window open AND on every build_list
        tick so rows age from Waiting → Failed without the user
        needing to close + reopen.

        Covers both waiting flavours:
          * ``waiting`` — classic 507 at init (row never uploaded
            anything; the legacy chunks_downloaded=-1 sentinel also
            qualifies, for back-compat with tray clipboard + --send
            CLI rows written by older builds).
          * ``waiting_stream`` — streaming mid-upload 507 (quota
            back-pressure between sender's write head and recipient's
            drain).
        Both use the same 30-min ceiling — they're the same logical
        budget, just measured from different points in the transfer
        lifecycle.

        Age check prefers waiting_started_at (stamped when the row
        entered waiting), falling back to timestamp. The 30-min window
        is a ceiling, not an instant-kill — a live subprocess must be
        given the full budget before we declare its row dead, otherwise
        the UI flashes Failed while the sender is still retrying and
        eventually succeeds.
        """
        cutoff = int(time.time()) - int(STORAGE_FULL_MAX_WINDOW_S)
        waiting_statuses = {TransferStatus.WAITING, TransferStatus.WAITING_STREAM}
        for it in history.items:
            chunks_dl = it.get("chunks_downloaded", 0) or 0
            is_waiting = (it.get("status") in waiting_statuses
                          or chunks_dl < 0)
            if not is_waiting:
                continue
            age_ref = int(it.get("waiting_started_at") or it.get("timestamp") or 0)
            if age_ref and age_ref < cutoff:
                tid = it.get("transfer_id")
                if tid:
                    history.update(tid, status=TransferStatus.FAILED,
                                   chunks_downloaded=0, chunks_total=0,
                                   failure_reason="quota_timeout")

    _scrub_zombie_waiting()

    app = _make_app()

    import re as _re
    _url_re = _re.compile(r'https?://\S+')

    def _contains_single_url(text):
        return len(_url_re.findall(text)) == 1

    def _extract_single_url(text):
        m = _url_re.findall(text)
        return m[0] if len(m) == 1 else None

    def on_item_click(item, win):
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

    def show_toast(win, message):
        toast = Adw.Toast(title=message, timeout=2)
        toast_overlay = win.get_content()
        # Find the toast overlay
        if hasattr(toast_overlay, 'add_toast'):
            toast_overlay.add_toast(toast)

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Transfer History",
                                     default_width=500, default_height=480)
        win.set_size_request(400, 300)

        # Card-per-item styling + flush progress bar.
        css = Gtk.CssProvider()
        css.load_from_string("""
            .transfer-card {
                padding-top: 5px;
                padding-bottom: 5px;
                transition: background-color 120ms ease,
                            opacity 300ms ease-out,
                            min-height 300ms ease-out,
                            padding 300ms ease-out,
                            margin 300ms ease-out;
            }
            .transfer-card.has-progress {
                padding-bottom: 0;
            }
            /* Shrink + fade when deleting. Matching Python timeout
               removes the widget from the tree after the transition. */
            .transfer-card.removing {
                opacity: 0;
                min-height: 0;
                padding-top: 0;
                padding-bottom: 0;
                margin-top: 0;
                margin-bottom: 0;
            }
            .transfer-card:hover {
                background-color: mix(@card_bg_color, @window_fg_color, 0.06);
            }
            .transfer-card:active {
                background-color: mix(@card_bg_color, @window_fg_color, 0.12);
            }
            .transfer-card-list,
            .transfer-card-list > row,
            .transfer-card-list > row.activatable {
                background: transparent;
                border: 0;
                padding-left: 3px;
                margin-left: 0px;
            }
            .transfer-card-list > row > box {
                margin-left: 0px;
                padding-left: 0px;
            }
            .transfer-card-list > row frame {
                min-width: 50px;
                min-height: 50px;
                background: alpha(@card_shade_color, 0.3);
                border-radius: 6px;
            }
            .transfer-card-list > row.activatable:hover,
            .transfer-card-list > row.activatable:active {
                background: transparent;
            }
            .upload-bar, .download-bar, .delivery-bar {
                min-height: 5px;
            }
            .upload-bar trough, .download-bar trough, .delivery-bar trough {
                min-height: 5px;
                background: alpha(@card_shade_color, 0.35);
                border-radius: 0;
            }
            .upload-bar progress, .download-bar progress, .delivery-bar progress {
                min-height: 5px;
                border-radius: 0;
            }
            .upload-bar progress {
                background-color: #FDD00C;
            }
            .download-bar progress {
                background-color: #5898FB;
            }
            .delivery-bar progress {
                background-color: #3986FC;
            }
            @keyframes pulse {
                0% { opacity: 0.5; }
                50% { opacity: 1.0; }
                100% { opacity: 0.5; }
            }
            .pulse-bar progress {
                animation: pulse 2s ease-in-out infinite;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        toolbar_view = Adw.ToolbarView()
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(toolbar_view)
        win.set_content(toast_overlay)

        header = Adw.HeaderBar()
        header.set_decoration_layout(":close")
        folder_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        folder_btn.set_tooltip_text("Open save folder")
        folder_btn.add_css_class("brand-action-accent")
        folder_btn.connect("clicked", lambda b: subprocess.Popen([
            "xdg-open", str(config.save_directory)
        ]))
        header.pack_start(folder_btn)

        clear_all_btn = Gtk.Button.new_from_icon_name("edit-clear-all-symbolic")
        clear_all_btn.set_tooltip_text("Clear visible history")
        clear_all_btn.add_css_class("brand-action-destructive")
        def on_clear_all(b):
            device = selected_device[0]
            if device is None:
                show_toast(win, "No connected device selected")
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
                    history.clear_for_peer(
                        device.device_id,
                        fallback_device_id=device.device_id,
                    )
                    _reset_history_view()
                    build_list()
            dialog.connect("response", on_response)
            dialog.present()
        clear_all_btn.connect("clicked", on_clear_all)
        header.pack_start(clear_all_btn)

        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroll)

        clamp = Adw.Clamp(maximum_size=9999, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        scroll.set_child(clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        clamp.set_child(content_box)

        device_picker, selected_device, paired_devices = _create_device_picker(
            config,
            title="History for",
            subtitle="Connected device",
        )
        device_group = Adw.PreferencesGroup()
        device_group.add(device_picker)
        content_box.append(device_group)
        clear_all_btn.set_sensitive(selected_device[0] is not None)

        list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content_box.append(list_container)

        has_active = [False]
        # row_widgets[transfer_id] = (box_widget, row, progress_bar_or_None)
        row_widgets = {}
        all_widgets = []  # ordered list of group children
        structural_sig = [None]  # (transfer_id, ...) — triggers structural diff
        empty_label = [None]  # holds the "No transfers yet" label when shown
        progress_sig = [None]    # mutable fields — triggers in-place update

        def _selected_device_id() -> str:
            device = selected_device[0]
            return device.device_id if device is not None else ""

        def _selected_device_name() -> str:
            device = selected_device[0]
            if device is None:
                return "connected devices"
            return _connected_device_label(device)

        def _empty_history_text() -> str:
            if selected_device[0] is None:
                return "No connected devices"
            return f"No transfers with {_selected_device_name()}"

        def _reset_history_view() -> None:
            structural_sig[0] = None
            progress_sig[0] = None

        def _compute_status(item):
            item_status = item.get("status", "complete")
            chunks_dl = item.get("chunks_downloaded", 0)
            chunks_total = item.get("chunks_total", 0)
            chunks_up = item.get("chunks_uploaded", 0)
            recv_dl = item.get("recipient_chunks_downloaded", 0)
            recv_total = item.get("recipient_chunks_total", 0)
            delivered = item.get("delivered", False)
            # Streaming "Sending X→Y" and "Waiting X→Y" both need the
            # same numerator/denominator pair: X = sender's upload count,
            # Y = recipient's ack count, N = transfer chunk count. On a
            # row that hasn't had delivery observations yet, fall back
            # to the classic chunks_total so the label isn't "X→0/0".
            stream_total = recv_total or chunks_total
            stream_xy = f"{chunks_up}→{recv_dl}"  # "X→Y"

            # chunks_dl < 0 is the legacy "waiting" sentinel written by
            # earlier builds before status="waiting" existed. Normalise
            # on read so old history rows render as Waiting too, not
            # as "Uploading -1/N".
            if item_status == TransferStatus.WAITING or chunks_dl < 0:
                # Yellow — queued, server storage is full for recipient.
                # Matches the tray/banner "storage full" colour family.
                text = f'<span foreground="{DC_YELLOW_500}">Waiting</span>'
            elif item_status == TransferStatus.WAITING_STREAM:
                # Mid-stream 507: sender paused until quota drains.
                # Same yellow as classic WAITING; different denominator
                # shape (X→Y, not N/total) because we're past init.
                text = (
                    f'<span foreground="{DC_YELLOW_500}">Waiting {stream_xy}'
                    f'/{stream_total}</span>'
                    if stream_total > 0
                    else f'<span foreground="{DC_YELLOW_500}">Waiting</span>'
                )
            elif item_status == TransferStatus.ABORTED:
                # Either-party abort. Orange matches the brand error
                # slot — terminal, no retry. abort_reason (optional)
                # is a short tag ("sender_abort", "recipient_abort",
                # "sender_failed") from the DELETE call that surfaced
                # the abort on this side.
                reason = item.get("abort_reason")
                reason_label = {
                    "sender_abort": "sender cancelled",
                    "recipient_abort": "recipient cancelled",
                    "sender_failed": "sender gave up",
                }.get(reason)
                if reason_label:
                    text = f'<span foreground="{DC_ORANGE_700}">Aborted ({reason_label})</span>'
                else:
                    text = f'<span foreground="{DC_ORANGE_700}">Aborted</span>'
            elif item_status == TransferStatus.UPLOADING:
                text = f"Uploading {chunks_dl}/{chunks_total}" if chunks_total > 0 else "Uploading"
            elif item_status == TransferStatus.SENDING:
                # The SENDING status is a "streaming in-flight" marker
                # set at init time by the sender loop. The LABEL we show
                # depends on where we actually are in the stream:
                #
                #   1. No recipient progress yet   → "Uploading X/N"
                #      (same as classic UPLOADING; sender is the only
                #      active party. Avoids the "Sending 5→0/5" footer
                #      that was confusing users into thinking they'd
                #      already finished.)
                #   2. Real overlap, upload in
                #      progress + recipient acking → "Sending X→Y/N"
                #      (blue; both sides active.)
                #   3. Upload done, recipient
                #      still draining              → "Delivering Y/N"
                #      (blue; matches classic delivery label — only
                #      the recipient is active.)
                #
                # Terminal "Delivered" handled by the `delivered` flag
                # branch further down.
                upload_done = stream_total > 0 and chunks_up >= stream_total
                if stream_total == 0:
                    text = f'<span foreground="{DC_BLUE_500}">Sending</span>'
                elif recv_dl == 0:
                    text = f"Uploading {chunks_up}/{stream_total}"
                elif not upload_done:
                    text = (
                        f'<span foreground="{DC_BLUE_500}">Sending {stream_xy}'
                        f'/{stream_total}</span>'
                    )
                else:
                    text = (
                        f'<span foreground="{DC_BLUE_500}">Delivering {recv_dl}'
                        f'/{stream_total}</span>'
                    )
            elif item_status == TransferStatus.DOWNLOADING:
                text = f"Downloading {chunks_dl}/{chunks_total}" if chunks_total > 0 else "Downloading"
            elif item_status == TransferStatus.FAILED:
                # Brand error slot — matches android/server/tray.
                # failure_reason (optional) is a short tag set by the
                # callers that know WHY a send failed; renders as a
                # parenthetical note. No tag => plain "Failed".
                reason = item.get("failure_reason")
                reason_label = {
                    "quota": "quota exceeded",
                    "quota_timeout": "quota exceeded",
                    "too_large": "exceeds server quota",
                }.get(reason)
                if reason_label:
                    text = f'<span foreground="{DC_ORANGE_700}">Failed ({reason_label})</span>'
                else:
                    text = f'<span foreground="{DC_ORANGE_700}">Failed</span>'
            elif item["direction"] == "received":
                # Sky blue — completed incoming transfer.
                text = f'<span foreground="{DC_BLUE_400}">Received</span>'
            elif delivered:
                # Brand success — green is retired.
                text = f'<span foreground="{DC_BLUE_500}">Delivered</span>'
            elif recv_dl > 0 and recv_total > 0:
                text = f"Delivering {recv_dl}/{recv_total}"
            else:
                text = "Sent"

            # Progress bar state: (show, css_class, fraction)
            if item_status == TransferStatus.WAITING or chunks_dl < 0:
                # No progress bar for waiting — the yellow text is the
                # whole signal. (Pulse + fraction=0 would imply motion
                # where there is none.) Same legacy-row fallback as
                # above so stale chunks_downloaded=-1 rows don't paint
                # a negative fraction.
                bar = (False, None, 0.0)
            elif item_status == TransferStatus.WAITING_STREAM:
                # Same reasoning as classic WAITING: yellow text is the
                # signal, no bar. The X→Y counters already convey the
                # in-flight position; a pulse would imply upload motion
                # that is precisely what's stalled.
                bar = (False, None, 0.0)
            elif item_status == TransferStatus.ABORTED:
                # Terminal — nothing to show motion for.
                bar = (False, None, 0.0)
            elif item_status == TransferStatus.UPLOADING and chunks_total > 0:
                bar = (True, "upload-bar", chunks_dl / chunks_total)
            elif item_status == TransferStatus.SENDING and stream_total > 0:
                # Streaming phases (matches the label branches above):
                #   recv_dl == 0               → upload fraction, yellow
                #                                (visually the same as
                #                                classic UPLOADING).
                #   recv_dl > 0, upload in
                #                 progress     → delivery fraction, blue
                #                                (blue denotes real
                #                                overlap).
                #   recv_dl > 0, upload done   → delivery fraction, blue
                #                                (same blue; label flips
                #                                to "Delivering").
                if recv_dl == 0:
                    bar = (True, "upload-bar", chunks_up / stream_total)
                else:
                    bar = (True, "delivery-bar", recv_dl / stream_total)
            elif item_status == TransferStatus.DOWNLOADING and chunks_total > 0:
                bar = (True, "download-bar", chunks_dl / chunks_total)
            elif (item["direction"] == "sent" and not delivered
                    and item_status == TransferStatus.COMPLETE):
                if recv_dl > 0 and recv_total > 0:
                    bar = (True, "delivery-bar", recv_dl / recv_total)
                else:
                    bar = (True, "delivery-bar pulse-bar", 1.0)
            else:
                bar = (False, None, 0.0)

            return text, bar

        def _create_row(item):
            """Create a new card widget (Box with rounded card styling containing a single-row ListBox + optional flush ProgressBar)."""
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
            import mimetypes as _mt
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

            def _do_local_remove(it, c, b):
                """The shared shrink+fade + history.remove path. Does NOT
                call the server; callers decide whether to cancel first."""
                history.remove(it)
                row_widgets.pop(_row_key(it), None)
                if c in all_widgets:
                    all_widgets.remove(c)
                structural_sig[0] = None
                b.set_sensitive(False)
                c.add_css_class("removing")

                def _finalize():
                    try:
                        list_container.remove(c)
                    except Exception:
                        pass
                    return False
                GLib.timeout_add(300, _finalize)

            def on_delete(b, it=captured_item, c=card):
                # Terminal rows (delivered sent, completed received,
                # aborted, failed) have no server state left — the
                # click just prunes the local history entry.
                # Non-terminal rows still own bytes on the server:
                #   * Sent + not-yet-delivered
                #       -> abort as sender. Recipient's next chunk call
                #         returns 410 and their row flips to Aborted.
                #   * Received + still downloading (streaming only —
                #                 classic receivers finalise before
                #                 returning to the history list)
                #       -> abort as recipient. Sender's next chunk
                #         upload returns 410 and their row flips to
                #         Aborted. Poller's streaming download loop
                #         also picks up the 410 on its next GET.
                status = it.get("status", "complete")
                direction = it.get("direction", "sent")
                delivered = it.get("delivered", False)
                is_live_receiver = (
                    direction == "received"
                    and status == TransferStatus.DOWNLOADING
                )
                is_live_sender = (
                    direction == "sent"
                    and not delivered
                    and status not in (TransferStatus.FAILED,
                                       TransferStatus.ABORTED)
                )
                if not (is_live_sender or is_live_receiver):
                    _do_local_remove(it, c, b)
                    return

                tid = it.get("transfer_id")
                label = history.get_label(it)
                if is_live_receiver:
                    heading = "Stop receiving?"
                    body = (f"The download of “{label}” will be "
                            f"cancelled and the sender will see Aborted.")
                    action_label = "Stop download"
                    abort_reason = "recipient_abort"
                else:
                    heading = "Cancel delivery?"
                    body = (f"The recipient will no longer receive "
                            f"“{label}”.")
                    action_label = "Cancel delivery"
                    abort_reason = "sender_abort"

                dialog = Adw.MessageDialog(
                    transient_for=win,
                    heading=heading,
                    body=body,
                )
                dialog.add_response("keep", "Keep")
                dialog.add_response("cancel", action_label)
                dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response("keep")
                dialog.set_close_response("keep")

                def on_response(d, response, _it=it, _c=c, _b=b, _tid=tid,
                                _reason=abort_reason):
                    if response != "cancel":
                        return
                    # Fire the server abort in a worker thread so the
                    # UI stays responsive if the server is slow; the
                    # local shrink/fade starts immediately either way
                    # (abort success or network failure — the row goes
                    # either way, the server will gc on its own expiry
                    # if we can't reach it now).
                    if _tid:
                        def _abort_worker(tid_local=_tid, reason=_reason):
                            try:
                                conn = ConnectionManager(
                                    config.server_url,
                                    config.device_id,
                                    config.auth_token,
                                )
                                ApiClient(conn, crypto).abort_transfer(
                                    tid_local, reason)
                            except Exception:
                                pass  # best effort; row still removed locally
                        threading.Thread(target=_abort_worker, daemon=True).start()
                    _do_local_remove(_it, _c, _b)

                dialog.connect("response", on_response)
                dialog.present()
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
            row.connect("activated", lambda r: on_item_click(_current_item(), win))

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

        def _update_row(item, row, old_progress_bar, parent_box):
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

        def _row_key(item):
            """Unique row identity for history-window diffing.
            Transfer-pipeline items have a real `transfer_id`; fn-payload
            items (clipboard text/image, .fn.unpair) go through fasttrack
            and persist with an EMPTY-STRING transfer_id, which collides on
            `dict.get(...)` lookups (empty-string is a present value, not
            a default-trigger). Fall back to a (timestamp, filename, label
            prefix) composite that's unique-enough across realistic clipboard
            cadences."""
            tid = item.get("transfer_id")
            if tid:
                return tid
            return (
                item.get("timestamp", 0),
                item.get("filename", ""),
                (item.get("display_label", "") or "")[:40],
            )

        def build_list():
            _scrub_zombie_waiting()
            history._items = history._load()
            selected_id = _selected_device_id()
            items = (
                history.items_for_peer(
                    selected_id,
                    fallback_device_id=selected_id,
                )
                if selected_id else []
            )

            # Structural sig: item identity and base state
            s_sig = (
                selected_id,
                tuple((_row_key(i), i.get("direction")) for i in items),
            )
            # Progress sig: all mutable fields
            p_sig = (
                selected_id,
                tuple(
                    (_row_key(i), i.get("status"), i.get("delivered"),
                     i.get("chunks_downloaded", 0),
                     i.get("recipient_chunks_downloaded", 0))
                    for i in items
                ),
            )

            if p_sig == progress_sig[0]:
                return True  # Nothing changed at all

            # Check if we need a full rebuild or just in-place updates
            needs_rebuild = (s_sig != structural_sig[0])
            structural_sig[0] = s_sig
            progress_sig[0] = p_sig

            # Update has_active flag
            downloading = any(i.get("status") in ("downloading", "uploading") for i in items)
            active_sent = any(
                i.get("direction") == "sent" and not i.get("delivered")
                for i in items
            )
            has_active[0] = downloading or active_sent

            if needs_rebuild:
                # Diff instead of full rebuild so adding one item or
                # deleting one row doesn't tear down and recreate every
                # other card (O(N) widget churn on every add/remove used
                # to cause visible jank on a full 50-item history).
                #
                # Also critical for the delete animation: a row in the
                # .removing transition window must not be ripped out of
                # the tree by an interleaving refresh — the 300 ms
                # GLib.timeout_add in on_delete handles final removal.
                new_tids_ordered = [_row_key(i) for i in items]
                by_tid_item = dict(zip(new_tids_ordered, items))

                current_tids = list(row_widgets.keys())
                new_tids_set = set(new_tids_ordered)
                removed_tids = [t for t in current_tids if t not in new_tids_set]

                # Drop the empty-state label if we now have items.
                if items and empty_label[0] is not None:
                    list_container.remove(empty_label[0])
                    if empty_label[0] in all_widgets:
                        all_widgets.remove(empty_label[0])
                    empty_label[0] = None

                # Remove rows whose tid is gone. Skip widgets already
                # animating out via the delete button — the GLib timer
                # there will finalize them.
                for tid in removed_tids:
                    entry = row_widgets.get(tid)
                    if not entry:
                        continue
                    card, _row, _pbar = entry
                    if "removing" in card.get_css_classes():
                        continue
                    try:
                        list_container.remove(card)
                    except Exception:
                        pass
                    if card in all_widgets:
                        all_widgets.remove(card)
                    row_widgets.pop(tid, None)

                # Insert newcomers at their correct positions. Assumes
                # `items` is ordered consistently across ticks (history
                # preserves insertion order, newest first).
                for idx, tid in enumerate(new_tids_ordered):
                    if tid in row_widgets:
                        continue
                    item = by_tid_item[tid]
                    card, row, pbar = _create_row(item)
                    if idx == 0:
                        list_container.prepend(card)
                    else:
                        prev_tid = new_tids_ordered[idx - 1]
                        prev_entry = row_widgets.get(prev_tid)
                        if prev_entry is not None:
                            list_container.insert_child_after(card, prev_entry[0])
                        else:
                            # Previous row isn't in the tree yet (unlikely
                            # with newest-first order + prepend loop) —
                            # append as a safe fallback.
                            list_container.append(card)
                    row_widgets[tid] = (card, row, pbar)
                    all_widgets.insert(min(idx, len(all_widgets)), card)

                # Empty-state label if nothing remains.
                if not items:
                    if empty_label[0] is None:
                        empty = Gtk.Label(label=_empty_history_text())
                        empty.add_css_class("dim-label")
                        empty.set_margin_top(48)
                        empty.set_margin_bottom(48)
                        list_container.append(empty)
                        all_widgets.append(empty)
                        empty_label[0] = empty
                    else:
                        empty_label[0].set_text(_empty_history_text())

            # In-place update on every tick — refresh subtitles and
            # progress bars for rows that existed before AND for rows we
            # just added (cheap, idempotent, makes the code branch-free).
            for item in items:
                tid = _row_key(item)
                entry = row_widgets.get(tid)
                if entry:
                    box, row, old_pbar = entry
                    new_pbar = _update_row(item, row, old_pbar, box)
                    row_widgets[tid] = (box, row, new_pbar)

            return True

        def on_history_device_changed(combo, _pspec):
            clear_all_btn.set_sensitive(selected_device[0] is not None)
            _reset_history_view()
            build_list()

        device_picker.connect("notify::selected", on_history_device_changed)

        build_list()

        # Adaptive refresh: 1s during active transfers, 3s otherwise
        def refresh_tick():
            build_list()
            interval = 1000 if has_active[0] else 3000
            GLib.timeout_add(interval, refresh_tick)
            return False  # don't repeat this one, the new timeout takes over
        GLib.timeout_add(1000, refresh_tick)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
