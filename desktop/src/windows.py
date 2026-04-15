#!/usr/bin/env python3
"""
GTK4/libadwaita windows — run as a separate process to avoid GTK3/4 conflict with pystray.

Usage:
    python3 -m src.windows send-files --config-dir=~/.config/desktop-connector
    python3 -m src.windows settings --config-dir=~/.config/desktop-connector
    python3 -m src.windows history --config-dir=~/.config/desktop-connector
"""

import argparse
import base64
import json
import sys
import threading
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GdkPixbuf, GLib, Pango


def format_size(b):
    if b < 1024: return f"{b} B"
    if b < 1024 * 1024: return f"{b // 1024} KB"
    return f"{b / (1024 * 1024):.1f} MB"


# ─── Send Files Window ───────────────────────────────────────────────

def show_send_files(config_dir: Path):
    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .history import TransferHistory
    from .dialogs import pick_files

    config = Config(config_dir)
    crypto = KeyManager(config_dir)
    history = TransferHistory(config_dir)

    file_list: list[Path] = []

    app = Adw.Application(application_id="com.desktopconnector.send")

    def on_activate(app):
        win = Adw.ApplicationWindow(application=app, title="Send to Phone",
                                     default_width=480, default_height=520)

        toolbar_view = Adw.ToolbarView()
        win.set_content(toolbar_view)
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        # Use a stack to swap between normal content and drop overlay
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(150)
        toolbar_view.set_content(stack)

        # --- Normal content page ---
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        stack.add_named(main_box, "content")

        # --- Drop overlay page (shown during drag) ---
        drop_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                               vexpand=True)
        drop_overlay.add_css_class("accent")
        overlay_icon = Gtk.Image.new_from_icon_name("folder-download-symbolic")
        overlay_icon.set_pixel_size(48)
        drop_overlay.append(overlay_icon)
        overlay_label = Gtk.Label(label="Drop files here")
        overlay_label.add_css_class("title-1")
        drop_overlay.append(overlay_label)
        stack.add_named(drop_overlay, "drop")

        stack.set_visible_child_name("content")

        # --- Drop zone (in normal content) ---
        drop_frame = Gtk.Frame(margin_start=16, margin_end=16, margin_top=16)
        drop_frame.add_css_class("view")
        main_box.append(drop_frame)

        drop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                           margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        drop_frame.set_child(drop_box)

        drop_icon = Gtk.Image.new_from_icon_name("folder-download-symbolic")
        drop_icon.set_pixel_size(32)
        drop_icon.add_css_class("dim-label")
        drop_box.append(drop_icon)

        drop_label = Gtk.Label(label="Drop files here or")
        drop_label.add_css_class("dim-label")
        drop_box.append(drop_label)

        browse_btn = Gtk.Button(label="Select Files...")
        browse_btn.add_css_class("pill")
        browse_btn.set_margin_top(8)
        drop_box.append(browse_btn)

        # --- Drag and drop on entire window ---
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

        def on_drop(target, value, x, y):
            files = value.get_files()
            for f in files:
                p = Path(f.get_path())
                if p.is_file() and p not in file_list:
                    file_list.append(p)
            stack.set_visible_child_name("content")
            refresh_list()
            return True

        def on_enter(target, x, y):
            stack.set_visible_child_name("drop")
            return Gdk.DragAction.COPY

        def on_leave(target):
            stack.set_visible_child_name("content")

        drop_target.connect("drop", on_drop)
        drop_target.connect("enter", on_enter)
        drop_target.connect("leave", on_leave)
        win.add_controller(drop_target)

        # --- File list ---
        list_label = Gtk.Label(label="Files to send", xalign=0,
                                margin_start=16, margin_top=12, margin_bottom=4)
        list_label.add_css_class("heading")
        main_box.append(list_label)

        scroll = Gtk.ScrolledWindow(vexpand=True, margin_start=16, margin_end=16)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scroll)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        scroll.set_child(listbox)

        sending = [False]
        cancel_requested = [False]
        file_rows: dict[Path, Adw.ActionRow] = {}

        def refresh_list():
            if sending[0]:
                return
            while True:
                row = listbox.get_row_at_index(0)
                if row is None:
                    break
                listbox.remove(row)
            file_rows.clear()

            if not file_list:
                er = Adw.ActionRow(title="No files selected")
                er.add_css_class("dim-label")
                listbox.append(er)
                send_btn.set_sensitive(False)
                send_btn.set_label("Send to Phone")
            else:
                total = sum(f.stat().st_size for f in file_list)
                for f in file_list:
                    row = Adw.ActionRow(
                        title=f.name,
                        subtitle=f"{f.parent}  \u00b7  {format_size(f.stat().st_size)}",
                    )
                    row.set_title_lines(1)
                    row.set_subtitle_lines(1)

                    remove_btn = Gtk.Button.new_from_icon_name("list-remove-symbolic")
                    remove_btn.set_valign(Gtk.Align.CENTER)
                    remove_btn.add_css_class("flat")
                    remove_btn.add_css_class("circular")
                    fp = f
                    remove_btn.connect("clicked", lambda b, p=fp: (file_list.remove(p), refresh_list()))
                    row.add_suffix(remove_btn)

                    file_rows[f] = row
                    listbox.append(row)

                send_btn.set_sensitive(True)
                send_btn.set_label(f"Send {len(file_list)} file(s) ({format_size(total)})")

        # --- Bottom bar ---
        action_bar = Gtk.ActionBar()
        main_box.append(action_bar)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", lambda b: (file_list.clear(), refresh_list()))
        action_bar.pack_start(clear_btn)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("destructive-action")
        cancel_btn.set_visible(False)
        action_bar.pack_start(cancel_btn)

        send_btn = Gtk.Button(label="Send to Phone")
        send_btn.add_css_class("suggested-action")
        send_btn.set_sensitive(False)

        refresh_list()

        status_file = config_dir / "upload_active.json"

        def write_status(uploading, current="", progress_num=0, total=0):
            try:
                status_file.write_text(json.dumps({
                    "uploading": uploading, "current": current,
                    "progress": f"{progress_num}/{total}",
                }))
            except Exception:
                pass

        def clear_status():
            try:
                status_file.unlink(missing_ok=True)
            except Exception:
                pass

        def mark_row_uploading(filepath):
            row = file_rows.get(filepath)
            if row:
                # Remove the remove button, add a spinner
                suffix = row.get_last_child()
                # Clear existing suffixes by setting subtitle to show status
                row.set_subtitle(f"Uploading...  \u00b7  {format_size(filepath.stat().st_size)}")

        def remove_row(filepath):
            row = file_rows.pop(filepath, None)
            if row:
                listbox.remove(row)
            if filepath in file_list:
                file_list.remove(filepath)

        def on_send(b):
            if not file_list:
                return
            paths = list(file_list)
            sending[0] = True
            cancel_requested[0] = False

            send_btn.set_visible(False)
            clear_btn.set_visible(False)
            cancel_btn.set_visible(True)
            cancel_btn.set_sensitive(True)
            cancel_btn.set_label("Cancel")
            browse_btn.set_sensitive(False)
            list_label.set_text("Sending...")

            # Disable remove buttons
            for row in file_rows.values():
                child = row.get_last_child()

            def do_send():
                conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
                api = ApiClient(conn, crypto)
                paired = config.get_first_paired_device()
                if not paired:
                    GLib.idle_add(finish_sending, 0, len(paths))
                    return

                target_id, target_info = paired
                symmetric_key = base64.b64decode(target_info["symmetric_key_b64"])
                sent = 0
                total = len(paths)

                write_status(True, total=total)

                for i, filepath in enumerate(paths):
                    if cancel_requested[0]:
                        break

                    GLib.idle_add(mark_row_uploading, filepath)
                    write_status(True, filepath.name, i + 1, total)

                    tid = api.send_file(filepath, target_id, symmetric_key)
                    if tid:
                        sent += 1
                        history.add(filename=filepath.name, display_label=filepath.name,
                                     direction="sent", size=filepath.stat().st_size,
                                     content_path=str(filepath), transfer_id=tid)
                        GLib.idle_add(remove_row, filepath)

                clear_status()
                GLib.idle_add(finish_sending, sent, total)

            threading.Thread(target=do_send, daemon=True).start()

        def finish_sending(sent, total):
            sending[0] = False
            cancel_btn.set_visible(False)
            send_btn.set_visible(True)
            clear_btn.set_visible(True)
            browse_btn.set_sensitive(True)

            if file_list:
                # Some files remain (cancelled or failed)
                list_label.set_text(f"Sent {sent}/{total} — {len(file_list)} remaining")
                send_btn.set_sensitive(True)
                send_btn.set_label(f"Retry {len(file_list)} file(s)")
            else:
                list_label.set_text(f"Done — {sent}/{total} sent")
                send_btn.set_sensitive(False)
                send_btn.set_label("Send to Phone")
                refresh_list()

        def on_cancel(b):
            cancel_requested[0] = True
            cancel_btn.set_sensitive(False)
            cancel_btn.set_label("Cancelling...")

        cancel_btn.connect("clicked", on_cancel)
        send_btn.connect("clicked", on_send)
        action_bar.pack_end(send_btn)

        def on_browse(b):
            paths = pick_files("Select files to send")
            for p in paths:
                if p not in file_list:
                    file_list.append(p)
            refresh_list()

        browse_btn.connect("clicked", on_browse)

        win.connect("close-request", lambda w: (clear_status(), False)[-1])

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── Settings Window ─────────────────────────────────────────────────

def _format_bytes(b):
    if b < 1024: return f"{b} B"
    if b < 1024 * 1024: return f"{b // 1024} KB"
    if b < 1024 * 1024 * 1024: return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def show_settings(config_dir: Path):
    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager, ConnectionState
    from .api_client import ApiClient

    config = Config(config_dir)
    crypto = KeyManager(config_dir)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")

    # Fetch stats from server
    stats = None
    try:
        api = ApiClient(conn, crypto)
        stats = api.get_stats()
    except Exception:
        pass

    app = Adw.Application(application_id="com.desktopconnector.settings")

    def on_activate(app):
        win = Adw.ApplicationWindow(application=app, title="Settings", default_width=420, default_height=480)
        win.set_resizable(False)

        toolbar_view = Adw.ToolbarView()
        win.set_content(toolbar_view)
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroll)

        clamp = Adw.Clamp(maximum_size=500, margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        scroll.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(content)

        # Connection
        conn_group = Adw.PreferencesGroup(title="Connection")
        content.append(conn_group)

        # Quick health check
        try:
            reachable = conn.check_connection()
        except Exception:
            reachable = False
        status_text = "Connected" if reachable else "Disconnected"

        status_row = Adw.ActionRow(title="Status", subtitle=status_text)
        conn_group.add(status_row)

        server_row = Adw.EntryRow(title="Relay Server URL", text=config.server_url)
        conn_group.add(server_row)

        # Long poll status (auto-refreshes every 3s)
        poll_status_file = config_dir / "poll_status.json"
        lp_labels = {"active": "Active", "unavailable": "Not available", "testing": "Testing (may take up to 30s)...", "offline": "Offline", "unknown": "Unknown"}
        lp_row = Adw.ActionRow(title="Long polling", subtitle="...")
        retry_btn = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
        retry_btn.add_css_class("suggested-action")
        def on_retry_lp(btn):
            poll_status_file.write_text(json.dumps({"long_poll": "testing"}))
            lp_row.set_subtitle("Testing (may take up to 30s)...")
            btn.set_visible(False)
        retry_btn.connect("clicked", on_retry_lp)
        lp_row.add_suffix(retry_btn)
        conn_group.add(lp_row)

        def refresh_lp_status():
            try:
                s = json.loads(poll_status_file.read_text()).get("long_poll", "unknown") if poll_status_file.exists() else "unknown"
            except Exception:
                s = "unknown"
            lp_row.set_subtitle(lp_labels.get(s, s))
            retry_btn.set_visible(s != "active")
            return True  # Keep timer
        refresh_lp_status()
        GLib.timeout_add(3000, refresh_lp_status)

        # Auto-open links toggle
        link_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        link_switch.set_active(config.auto_open_links)
        link_switch.connect("notify::active", lambda sw, _: setattr(config, 'auto_open_links', sw.get_active()))
        link_row = Adw.ActionRow(title="Auto-open links", subtitle="Open received URLs in browser automatically")
        link_row.add_suffix(link_switch)
        link_row.set_activatable_widget(link_switch)
        conn_group.add(link_row)

        save_btn = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
        save_btn.add_css_class("suggested-action")
        server_row.add_suffix(save_btn)

        def on_save(btn):
            new_url = server_row.get_text().strip()
            if new_url:
                config.server_url = new_url
                btn.set_label("\u2713 Saved")
                btn.set_sensitive(False)
                GLib.timeout_add(2000, lambda: (btn.set_label("Save"), btn.set_sensitive(True), False)[-1])

        save_btn.connect("clicked", on_save)

        # This device
        device_group = Adw.PreferencesGroup(title="This Device")
        content.append(device_group)
        device_group.add(Adw.ActionRow(title="Name", subtitle=config.device_name))
        device_group.add(Adw.ActionRow(title="Device ID", subtitle=crypto.get_device_id()[:24] + "..."))

        # Paired device
        pair_group = Adw.PreferencesGroup(title="Paired Device")
        content.append(pair_group)

        paired = config.get_first_paired_device()
        if paired:
            device_id, info = paired
            name = info.get("name", "Unknown")

            pair_group.add(Adw.ActionRow(title="Name", subtitle=name))
            pair_group.add(Adw.ActionRow(title="Device ID", subtitle=device_id[:24] + "..."))

            unpair_btn = Gtk.Button(label="Unpair", valign=Gtk.Align.CENTER,
                                    margin_top=8, halign=Gtk.Align.START)
            unpair_btn.add_css_class("destructive-action")

            def on_unpair(btn):
                dialog = Adw.MessageDialog(
                    transient_for=win,
                    heading="Unpair?",
                    body=f"Disconnect from \"{name}\"?\nYou will need to pair again.",
                )
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("unpair", "Unpair")
                dialog.set_response_appearance("unpair", Adw.ResponseAppearance.DESTRUCTIVE)

                def on_response(dlg, response):
                    if response == "unpair":
                        # Notify the other side
                        try:
                            import tempfile
                            sym_key = base64.b64decode(info["symmetric_key_b64"])
                            tmp = Path(tempfile.mktemp(suffix="_.fn.unpair"))
                            tmp.write_bytes(b"unpair")
                            conn_tmp = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")
                            api_tmp = ApiClient(conn_tmp, crypto)
                            api_tmp.send_file(tmp, device_id, sym_key, filename_override=".fn.unpair")
                            tmp.unlink(missing_ok=True)
                        except Exception:
                            pass
                        # Remove local pairing
                        devices = config.paired_devices
                        if device_id in devices:
                            del devices[device_id]
                            config._data["paired_devices"] = devices
                            config.save()
                        win.close()

                dialog.connect("response", on_response)
                dialog.present()

            unpair_btn.connect("clicked", on_unpair)
            pair_group.add(unpair_btn)
        else:
            pair_group.add(Adw.ActionRow(title="Not paired", subtitle="Use Pair... from the tray menu"))

        # Connection statistics (only when paired)
        if stats and paired:
            stats_group = Adw.PreferencesGroup(title="Connection Statistics")
            content.append(stats_group)

            paired_devs = stats.get("paired_devices", [])
            if paired_devs:
                pd = paired_devs[0]
                online = pd.get("online", False)
                stats_group.add(Adw.ActionRow(
                    title="Paired device status",
                    subtitle="Online" if online else "Offline",
                ))
                stats_group.add(Adw.ActionRow(
                    title="Total transfers",
                    subtitle=str(pd.get("transfers", 0)),
                ))
                stats_group.add(Adw.ActionRow(
                    title="Data transferred",
                    subtitle=_format_bytes(pd.get("bytes_transferred", 0)),
                ))
                paired_since = pd.get("paired_since", 0)
                if paired_since:
                    stats_group.add(Adw.ActionRow(
                        title="Paired since",
                        subtitle=time.strftime("%b %d, %Y", time.localtime(paired_since)),
                    ))

            stats_group.add(Adw.ActionRow(
                title="Pending incoming",
                subtitle=str(stats.get("pending_incoming", 0)),
            ))
            stats_group.add(Adw.ActionRow(
                title="Pending outgoing",
                subtitle=str(stats.get("pending_outgoing", 0)),
            ))

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── History Window ──────────────────────────────────────────────────

def show_history(config_dir: Path):
    import subprocess
    from .config import Config
    from .history import TransferHistory
    from .clipboard import write_clipboard_text, write_clipboard_image

    config = Config(config_dir)
    history = TransferHistory(config_dir)

    app = Adw.Application(application_id="com.desktopconnector.history")

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
        win = Adw.ApplicationWindow(application=app, title="Transfer History",
                                     default_width=500, default_height=480)
        win.set_size_request(400, 300)

        # Reduce ActionRow left padding so thumbnails have equal spacing
        css = Gtk.CssProvider()
        css.load_from_string("""
            row.activatable { padding-left: 3px; margin-left: 0px; }
            row.activatable > box { margin-left: 0px; padding-left: 0px; }
            row.activatable frame {
                min-width: 50px;
                min-height: 50px;
                background: alpha(@card_shade_color, 0.3);
                border-radius: 6px;
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
        folder_btn.connect("clicked", lambda b: subprocess.Popen([
            "xdg-open", str(config.save_directory)
        ]))
        header.pack_start(folder_btn)
        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroll)

        clamp = Adw.Clamp(maximum_size=9999, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        scroll.set_child(clamp)

        group = Adw.PreferencesGroup()
        clamp.set_child(group)

        last_snapshot = [None]
        current_rows = []

        def build_list():
            # Reload from disk to pick up changes from the main process
            history._items = history._load()
            items = history.items

            # Check if anything changed
            snapshot = json.dumps(items)
            if snapshot == last_snapshot[0]:
                return True  # No change, keep timer going
            last_snapshot[0] = snapshot

            # Remove previously added rows
            for row in current_rows:
                group.remove(row)
            current_rows.clear()

            if not items:
                row = Adw.ActionRow(title="No transfers yet")
                row.add_css_class("dim-label")
                group.add(row)
                current_rows.append(row)
            else:
                for item in items:
                    direction_prefix = "\u2193" if item["direction"] == "received" else "\u2191"
                    label = history.get_label(item)
                    size = format_size(item.get("size", 0))
                    ts = time.strftime("%b %d, %H:%M", time.localtime(item.get("timestamp", 0)))
                    is_clipboard = item.get("filename", "").startswith(".fn.clipboard")
                    delivered = item.get("delivered", False)
                    if item["direction"] == "received":
                        status = "Received"
                    elif delivered:
                        status = "Delivered"
                    else:
                        status = "Sent"

                    row = Adw.ActionRow(
                        title=f"{direction_prefix}  {label}",
                        subtitle=f"{size}  \u00b7  {ts}  \u00b7  {status}",
                        activatable=True,
                    )
                    row.set_title_lines(1)

                    # Thumbnail or icon as prefix
                    content_path = item.get("content_path", "")
                    filename = item.get("filename", "")
                    import mimetypes as _mt
                    mime, _ = _mt.guess_type(filename or content_path)
                    thumb_widget = None

                    if content_path and Path(content_path).exists() and mime and (mime.startswith("image/") or mime.startswith("video/")):
                        try:
                            # Load and crop to square (cover style)
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
                    captured_item = item

                    def on_delete(b, it=captured_item):
                        history.remove(it)
                        last_snapshot[0] = None  # Force rebuild
                        build_list()

                    del_btn.connect("clicked", on_delete)
                    row.add_suffix(del_btn)

                    captured = item
                    row.connect("activated", lambda r, it=captured: on_item_click(it, win))

                    group.add(row)
                    current_rows.append(row)

            return True  # Keep timer going

        build_list()

        # Auto-refresh every 3 seconds
        GLib.timeout_add(3000, build_list)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── Pairing Window ──────────────────────────────────────────────────

def show_pairing(config_dir: Path):
    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .pairing import generate_qr_data, generate_qr_image

    import io

    config = Config(config_dir)
    crypto = KeyManager(config_dir)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")
    api = ApiClient(conn, crypto)

    qr_data = generate_qr_data(config, crypto)
    qr_pil = generate_qr_image(qr_data)
    server_url = json.loads(qr_data)["server"]
    device_id = crypto.get_device_id()

    app = Adw.Application(application_id="com.desktopconnector.pairing")

    def on_activate(app):
        win = Adw.ApplicationWindow(application=app, title="Pair with Phone",
                                     default_width=400, default_height=560)

        toolbar_view = Adw.ToolbarView()
        win.set_content(toolbar_view)
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=24, margin_start=24, margin_end=24,
                      halign=Gtk.Align.CENTER)
        toolbar_view.set_content(box)

        # Title
        title = Gtk.Label(label="Scan this QR code with your phone")
        title.add_css_class("title-3")
        box.append(title)

        # Server info
        server_label = Gtk.Label(label=server_url)
        server_label.add_css_class("dim-label")
        server_label.add_css_class("caption")
        box.append(server_label)

        id_label = Gtk.Label(label=f"Device ID: {device_id[:16]}...")
        id_label.add_css_class("dim-label")
        id_label.add_css_class("caption")
        box.append(id_label)

        # QR code image
        buf = io.BytesIO()
        qr_pil.save(buf, format="PNG")
        buf.seek(0)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(buf.read())
        loader.close()
        pixbuf = loader.get_pixbuf()
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        qr_image = Gtk.Picture.new_for_paintable(texture)
        qr_image.set_size_request(280, 280)
        qr_image.set_content_fit(Gtk.ContentFit.CONTAIN)
        box.append(qr_image)

        # Status
        status_label = Gtk.Label(label="Waiting for phone to scan...")
        status_label.add_css_class("body")
        box.append(status_label)

        # Verification code
        code_label = Gtk.Label(label="")
        code_label.add_css_class("title-1")
        box.append(code_label)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          halign=Gtk.Align.CENTER)
        box.append(btn_box)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda b: win.close())
        btn_box.append(cancel_btn)

        confirm_btn = Gtk.Button(label="Confirm Pairing")
        confirm_btn.add_css_class("suggested-action")
        confirm_btn.set_sensitive(False)
        btn_box.append(confirm_btn)

        phone_info = [None]

        def on_confirm(b):
            info = phone_info[0]
            if info:
                import base64
                sym_key = crypto.derive_shared_key(info["phone_pubkey"])
                config.add_paired_device(
                    device_id=info["phone_id"],
                    pubkey=info["phone_pubkey"],
                    symmetric_key_b64=base64.b64encode(sym_key).decode(),
                    name=f"Phone-{info['phone_id'][:8]}",
                )
                api.confirm_pairing(info["phone_id"])
                status_label.set_text("Paired!")
                GLib.timeout_add(1000, win.close)

        confirm_btn.connect("clicked", on_confirm)

        def poll_pairing():
            if not win.is_visible():
                return False
            requests_list = api.poll_pairing()
            if requests_list:
                req = requests_list[0]
                phone_info[0] = req
                sym_key = crypto.derive_shared_key(req["phone_pubkey"])
                code = KeyManager.get_verification_code(sym_key)
                status_label.set_text(f"Phone connected: {req['phone_id'][:12]}...  Verify code:")
                code_label.set_text(code)
                confirm_btn.set_sensitive(True)
                return False  # Stop polling
            return True  # Keep polling

        GLib.timeout_add(2000, poll_pairing)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── CLI entry point ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("window", choices=["send-files", "settings", "history", "pairing"])
    parser.add_argument("--config-dir", required=True)
    args = parser.parse_args()

    config_dir = Path(args.config_dir)

    if args.window == "send-files":
        show_send_files(config_dir)
    elif args.window == "settings":
        show_settings(config_dir)
    elif args.window == "history":
        show_history(config_dir)
    elif args.window == "pairing":
        show_pairing(config_dir)


if __name__ == "__main__":
    main()
