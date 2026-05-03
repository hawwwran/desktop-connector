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
from gi.repository import Gtk, Adw, Gdk, GdkPixbuf, Gio, GLib, Pango

from .brand import (
    APP_ID,
    DC_BLUE_400,
    DC_BLUE_500,
    DC_ORANGE_700,
    DC_YELLOW_500,
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
    claim_gtk_identity,
)
from .notifications import notify


def _notify_folders_skipped(count: int) -> None:
    word = "folder" if count == 1 else "folders"
    notify(
        "Folder transport is not supported",
        f"Skipped {count} {word}. Send individual files instead.",
        icon="dialog-warning",
    )


def _make_app() -> Adw.Application:
    """Shared Adw.Application factory.

    NON_UNIQUE lets multiple window subprocesses coexist under one app_id,
    which is what makes the compositor group them into a single taskbar
    entry tagged with the brand icon."""
    claim_gtk_identity()
    return Adw.Application(application_id=APP_ID,
                            flags=Gio.ApplicationFlags.NON_UNIQUE)


def format_size(b):
    if b < 1024: return f"{b} B"
    if b < 1024 * 1024: return f"{b // 1024} KB"
    return f"{b / (1024 * 1024):.1f} MB"


def _connected_device_label(device) -> str:
    name = (device.name or "").strip()
    return name if name else f"Device {device.short_id}"


def _create_device_picker(config, *, title: str, subtitle: str = ""):
    from .devices import ConnectedDeviceRegistry

    registry = ConnectedDeviceRegistry(config)
    devices = registry.list_devices()
    active_device = registry.get_active_device()
    device_labels = [_connected_device_label(device) for device in devices]
    selected_device = [None]

    if active_device is not None:
        active_id = active_device.device_id
        for index, device in enumerate(devices):
            if device.device_id == active_id:
                selected_device[0] = device
                selected_index = index
                break
        else:
            selected_index = 0
    else:
        selected_index = 0

    row = Adw.ComboRow(
        title=title,
        subtitle=subtitle,
        model=Gtk.StringList.new(device_labels or ["No paired devices"]),
    )
    row.set_sensitive(bool(devices))
    row.set_selected(selected_index)

    def on_device_changed(combo, _pspec):
        selected = combo.get_selected()
        if 0 <= selected < len(devices):
            selected_device[0] = devices[selected]
        else:
            selected_device[0] = None

    row.connect("notify::selected", on_device_changed)
    return row, selected_device, devices


# ─── Send Files Window ───────────────────────────────────────────────

def show_send_files(config_dir: Path):
    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .history import TransferHistory, TransferStatus
    # windows.py runs as a GTK4 subprocess — Linux-scoped by construction,
    # so instantiate the Linux backend directly instead of composing all four.
    from .backends.linux.dialog_backend import LinuxDialogBackend

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    history = TransferHistory(config_dir)
    dialogs = LinuxDialogBackend()

    file_list: list[Path] = []

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Send files to",
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

        device_picker, selected_device, paired_devices = _create_device_picker(
            config,
            title="Send files to",
            subtitle="Connected device",
        )
        device_group = Adw.PreferencesGroup(
            margin_start=16,
            margin_end=16,
            margin_top=16,
        )
        device_group.add(device_picker)
        main_box.append(device_group)

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
        drop_frame = Gtk.Frame(margin_start=16, margin_end=16, margin_top=12)
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
            skipped_folders = 0
            for f in files:
                p = Path(f.get_path())
                if p.is_dir():
                    skipped_folders += 1
                    continue
                if p.is_file() and p not in file_list:
                    file_list.append(p)
            if skipped_folders:
                _notify_folders_skipped(skipped_folders)
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
                send_btn.set_label("Send files")
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

                send_btn.set_sensitive(selected_device[0] is not None)
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

        send_btn = Gtk.Button(label="Send files")
        send_btn.add_css_class("suggested-action")
        send_btn.set_sensitive(False)
        device_picker.connect("notify::selected", lambda *_: refresh_list())

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
            target = selected_device[0]
            if target is None:
                notify("No connected device", "Pair a device before sending files.")
                return
            if not target.symmetric_key_b64:
                notify(
                    "Send failed",
                    "Missing pairing key for the selected device.",
                    icon="dialog-warning",
                )
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
            device_picker.set_sensitive(False)
            list_label.set_text("Sending...")

            # Disable remove buttons
            for row in file_rows.values():
                child = row.get_last_child()

            def do_send():
                conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
                api = ApiClient(conn, crypto)

                target_id = target.device_id
                symmetric_key = base64.b64decode(target.symmetric_key_b64)
                try:
                    config.active_device_id = target_id
                except Exception:
                    pass
                sent = 0
                total = len(paths)

                write_status(True, total=total)

                for i, filepath in enumerate(paths):
                    if cancel_requested[0]:
                        break

                    GLib.idle_add(mark_row_uploading, filepath)
                    write_status(True, filepath.name, i + 1, total)

                    # Track upload progress in history. Flags share
                    # state between the two callbacks and the post-
                    # send_file fallback; a dict keeps closures cheap
                    # without one list-per-flag.
                    file_size = filepath.stat().st_size
                    st = {
                        "tid": None,
                        "saw_waiting_classic": False,   # classic init 507
                        "saw_waiting_stream": False,    # streaming mid-upload 507
                        "saw_too_large": False,         # classic init 413
                        "stream_terminal": False,       # stream cb set final status
                    }

                    def upload_progress(transfer_id, uploaded, total_chunks,
                                        fp=filepath, sz=file_size, state=st):
                        # Classic-path callback. Sentinel values:
                        #   0  — initial row write (uploading 0/N)
                        #   -1 — 507 storage_full, flip to WAITING
                        #   -2 — 413 too_large, terminal failure
                        # Streaming init never 507s; uploaded==-1 only
                        # happens when the server downgraded to classic
                        # and the classic path hit a 507 at init.
                        if uploaded == -2:
                            state["saw_too_large"] = True
                            if state["tid"] is None:
                                state["tid"] = transfer_id
                                history.add(filename=fp.name, display_label=fp.name,
                                            direction="sent", size=sz,
                                            content_path=str(fp), transfer_id=transfer_id,
                                            status=TransferStatus.FAILED,
                                            chunks_downloaded=0, chunks_total=total_chunks,
                                            peer_device_id=target_id,
                                            failure_reason="too_large")
                            else:
                                history.update(transfer_id,
                                                status=TransferStatus.FAILED,
                                                failure_reason="too_large")
                            return
                        if uploaded in (0, -1):
                            if state["tid"] is None:
                                state["tid"] = transfer_id
                                history.add(filename=fp.name, display_label=fp.name,
                                            direction="sent", size=sz,
                                            content_path=str(fp), transfer_id=transfer_id,
                                            status=(TransferStatus.WAITING
                                                    if uploaded == -1
                                                    else TransferStatus.UPLOADING),
                                            chunks_downloaded=0, chunks_total=total_chunks,
                                            peer_device_id=target_id)
                            elif uploaded == -1:
                                history.update(transfer_id,
                                               status=TransferStatus.WAITING)
                            else:
                                history.update(transfer_id,
                                               status=TransferStatus.UPLOADING)
                            if uploaded == -1:
                                state["saw_waiting_classic"] = True
                                history.update(transfer_id,
                                               waiting_started_at=int(time.time()))
                        else:
                            history.update(transfer_id,
                                           status=TransferStatus.UPLOADING,
                                           chunks_downloaded=uploaded,
                                           chunks_total=total_chunks)

                    def stream_progress(transfer_id, uploaded, total_chunks,
                                        stream_state, fp=filepath, state=st):
                        """Streaming-path callback — state ∈
                        {sending, waiting_stream, aborted, failed}.

                        By the time this fires, the classic ``on_progress``
                        has already created the history row with
                        status=uploading. We flip to the streaming
                        representation here and own terminal statuses
                        (aborted / failed) so the post-send_file
                        fallback doesn't overwrite them.
                        """
                        if stream_state == "sending":
                            # Row may still be in UPLOADING from the
                            # pre-init placeholder; flip to SENDING + mark
                            # mode=streaming so _compute_status picks the
                            # "Sending X→Y/N" branch. chunks_downloaded
                            # stays in lockstep with chunks_uploaded so
                            # older readers (no mode awareness) still
                            # render sensible numbers.
                            history.update(
                                transfer_id,
                                status=TransferStatus.SENDING,
                                mode="streaming",
                                chunks_uploaded=uploaded,
                                chunks_downloaded=uploaded,
                                chunks_total=total_chunks,
                            )
                        elif stream_state == "waiting_stream":
                            state["saw_waiting_stream"] = True
                            history.update(
                                transfer_id,
                                status=TransferStatus.WAITING_STREAM,
                                mode="streaming",
                                chunks_uploaded=uploaded,
                                chunks_total=total_chunks,
                                waiting_started_at=int(time.time()),
                            )
                        elif stream_state == "aborted":
                            # Streaming sender only sees aborted when the
                            # recipient aborted — the server emits 410
                            # with abort_reason=recipient_abort on the
                            # next chunk upload.
                            state["stream_terminal"] = True
                            history.update(
                                transfer_id,
                                status=TransferStatus.ABORTED,
                                abort_reason="recipient_abort",
                                chunks_downloaded=0,
                                chunks_total=0,
                            )
                        elif stream_state == "failed":
                            state["stream_terminal"] = True
                            reason = ("quota_timeout"
                                      if state["saw_waiting_stream"]
                                      else None)
                            fields = {
                                "status": TransferStatus.FAILED,
                                "chunks_downloaded": 0,
                                "chunks_total": 0,
                            }
                            if reason:
                                fields["failure_reason"] = reason
                            history.update(transfer_id, **fields)

                    tid = api.send_file(filepath, target_id, symmetric_key,
                                        on_progress=upload_progress,
                                        on_stream_progress=stream_progress)
                    if tid:
                        sent += 1
                        # Upload logic cleans up its own progress fields;
                        # delivery tracker owns recipient_* from here.
                        # Keep mode intact so the history renderer knows
                        # whether to paint "Delivered" vs a streaming
                        # breadcrumb (both currently render "Delivered"
                        # blue, but C.7 may diverge).
                        history.update(tid, status=TransferStatus.COMPLETE,
                                       chunks_downloaded=0, chunks_total=0)
                        GLib.idle_add(remove_row, filepath)
                    elif (state["tid"] and not state["saw_too_large"]
                          and not state["stream_terminal"]):
                        # Row wasn't already tagged by the too_large or
                        # streaming-terminal callback. Map WAITING
                        # (classic init 507) → quota_timeout, everything
                        # else to plain Failed.
                        reason = ("quota_timeout"
                                  if state["saw_waiting_classic"]
                                  else None)
                        fields = {"status": TransferStatus.FAILED}
                        if reason:
                            fields["failure_reason"] = reason
                        history.update(state["tid"], **fields)

                clear_status()
                GLib.idle_add(finish_sending, sent, total)

            threading.Thread(target=do_send, daemon=True).start()

        def finish_sending(sent, total):
            sending[0] = False
            cancel_btn.set_visible(False)
            send_btn.set_visible(True)
            clear_btn.set_visible(True)
            browse_btn.set_sensitive(True)
            device_picker.set_sensitive(bool(paired_devices))

            if file_list:
                # Some files remain (cancelled or failed)
                list_label.set_text(f"Sent {sent}/{total} — {len(file_list)} remaining")
                send_btn.set_sensitive(True)
                send_btn.set_label(f"Retry {len(file_list)} file(s)")
            else:
                list_label.set_text(f"Done — {sent}/{total} sent")
                send_btn.set_sensitive(False)
                send_btn.set_label("Send files")
                refresh_list()

        def on_cancel(b):
            cancel_requested[0] = True
            cancel_btn.set_sensitive(False)
            cancel_btn.set_label("Cancelling...")

        cancel_btn.connect("clicked", on_cancel)
        send_btn.connect("clicked", on_send)
        action_bar.pack_end(send_btn)

        def on_browse(b):
            paths = dialogs.pick_files("Select files to send")
            skipped_folders = 0
            for p in paths:
                if p.is_dir():
                    skipped_folders += 1
                    continue
                if p not in file_list:
                    file_list.append(p)
            if skipped_folders:
                _notify_folders_skipped(skipped_folders)
            refresh_list()

        browse_btn.connect("clicked", on_browse)

        win.connect("close-request", lambda w: (clear_status(), False)[-1])

        apply_pointer_cursors(win)
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
    import os as _os
    from .config import (
        Config,
        DEFAULT_RECEIVE_ACTION_LIMITS,
        RECEIVE_ACTION_COPY,
        RECEIVE_ACTION_KEY_DOCUMENT_OPEN,
        RECEIVE_ACTION_KEY_IMAGE_OPEN,
        RECEIVE_ACTION_KEY_TEXT_COPY,
        RECEIVE_ACTION_KEY_URL_COPY,
        RECEIVE_ACTION_KEY_URL_OPEN,
        RECEIVE_ACTION_KEY_VIDEO_OPEN,
        RECEIVE_ACTION_LIMIT_BATCH,
        RECEIVE_ACTION_LIMIT_MAX,
        RECEIVE_ACTION_LIMIT_MINUTE,
        RECEIVE_ACTION_NONE,
        RECEIVE_ACTION_OPEN,
        RECEIVE_KIND_DOCUMENT,
        RECEIVE_KIND_IMAGE,
        RECEIVE_KIND_TEXT,
        RECEIVE_KIND_URL,
        RECEIVE_KIND_VIDEO,
    )
    from .crypto import KeyManager
    from .connection import ConnectionManager, ConnectionState
    from .api_client import ApiClient
    from .bootstrap.app_version import get_app_version

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")

    # Fetch stats from server
    from .devices import (
        ConnectedDeviceRegistry,
        DeviceRegistryError,
        DuplicateDeviceNameError,
    )
    from .file_manager_integration import sync_file_manager_targets

    stats = None
    settings_registry = ConnectedDeviceRegistry(config)
    settings_active_device = settings_registry.get_active_device()
    try:
        api = ApiClient(conn, crypto)
        stats = api.get_stats(
            paired_with=settings_active_device.device_id
            if settings_active_device else None,
        )
    except Exception:
        pass

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Settings", default_width=630, default_height=624)
        win.set_resizable(True)

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

        # Appearance
        appearance_group = Adw.PreferencesGroup(title="Appearance")
        content.append(appearance_group)

        theme_modes = (
            ("System", "system"),
            ("Light", "light"),
            ("Dark", "dark"),
        )
        theme_model = Gtk.StringList.new([label for label, _ in theme_modes])
        theme_row = Adw.ComboRow(
            title="Theme",
            subtitle="Match desktop, or force light / dark mode.",
            model=theme_model,
        )
        current_mode = config.theme_mode
        for idx, (_, value) in enumerate(theme_modes):
            if value == current_mode:
                theme_row.set_selected(idx)
                break

        def on_theme_changed(combo, _pspec, modes=theme_modes):
            i = combo.get_selected()
            if 0 <= i < len(modes):
                new_mode = modes[i][1]
                if new_mode != config.theme_mode:
                    config.theme_mode = new_mode
                    # Live-apply to this window so the change is visible
                    # without a restart. Other open subprocesses pick it
                    # up on their next reload (config.json is the source
                    # of truth).
                    from .brand import apply_theme_mode
                    apply_theme_mode(new_mode)

        theme_row.connect("notify::selected", on_theme_changed)
        appearance_group.add(theme_row)

        # ---- Vault section (T3.3) ----
        # T0 §D16: a small "Vault" group with the active toggle and an
        # "Open Vault settings…" button. Toggle is ON by default on
        # fresh install; OFF hides the tray submenu and pauses sync
        # without destroying any data.
        from .vault_ui_state import vault_settings_button_state

        vault_group = Adw.PreferencesGroup(title="Vault")
        content.append(vault_group)

        vault_active_row = Adw.SwitchRow(
            title="Vault active",
            subtitle="Show Vault in tray menu and run sync. OFF is reversible — keys, manifests, and downloaded data are preserved.",
            active=config.vault_active,
        )
        vault_group.add(vault_active_row)

        open_vault_row = Adw.ActionRow(
            title="Open Vault settings…",
            subtitle="Opens the deep-config window. Disabled when Vault is inactive.",
        )
        vault_group.add(open_vault_row)
        open_vault_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_vault_btn.add_css_class("pill")
        open_vault_row.add_suffix(open_vault_btn)

        def vault_exists_locally() -> bool:
            raw = config._data.get("vault")
            return isinstance(raw, dict) and bool(raw.get("last_known_id"))

        def refresh_vault_button():
            state = vault_settings_button_state(
                toggle_active=config.vault_active,
                vault_exists=vault_exists_locally(),
            )
            open_vault_btn.set_sensitive(state.enabled)

        def on_vault_toggled(switch, _pspec):
            new_value = switch.get_active()
            if new_value != config.vault_active:
                config.vault_active = new_value
            refresh_vault_button()

        def on_open_vault_clicked(_btn):
            state = vault_settings_button_state(
                toggle_active=config.vault_active,
                vault_exists=vault_exists_locally(),
            )
            target = None
            if state.action == "launch_wizard":
                target = "vault-onboard"
            elif state.action == "launch_settings":
                target = "vault-main"
            if target is None:
                return
            import os as _os
            import subprocess as _subprocess
            import sys as _sys
            appimage = _os.environ.get("APPIMAGE")
            cmd = (
                [appimage, f"--gtk-window={target}",
                 f"--config-dir={config.config_dir}"]
                if appimage else
                [_sys.executable, "-m", "src.windows", target,
                 f"--config-dir={config.config_dir}"]
            )
            cwd = (None if appimage
                   else str(Path(__file__).resolve().parent.parent))
            _subprocess.Popen(cmd, cwd=cwd)

        vault_active_row.connect("notify::active", on_vault_toggled)
        open_vault_btn.connect("clicked", on_open_vault_clicked)
        refresh_vault_button()

        # Receive actions
        receive_group = Adw.PreferencesGroup(title="Receive Actions")
        content.append(receive_group)

        receive_action_rows = (
            (
                RECEIVE_KIND_URL,
                "URL",
                "What to do when received text is detected as a URL.",
                (
                    ("Open in default browser", RECEIVE_ACTION_OPEN),
                    ("Copy to clipboard", RECEIVE_ACTION_COPY),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_TEXT,
                "Text",
                "What to do after receiving text that is not only a URL.",
                (
                    ("Copy to clipboard", RECEIVE_ACTION_COPY),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_IMAGE,
                "Image",
                "What to do after receiving an image file.",
                (
                    ("Open in default image viewer", RECEIVE_ACTION_OPEN),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_VIDEO,
                "Video",
                "What to do after receiving a video file.",
                (
                    ("Open in default video viewer", RECEIVE_ACTION_OPEN),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_DOCUMENT,
                "Document",
                "What to do after receiving a document file.",
                (
                    ("Open in default document viewer", RECEIVE_ACTION_OPEN),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
        )

        for kind, title, subtitle, options in receive_action_rows:
            labels = [label for label, _action in options]
            actions = [action for _label, action in options]
            row = Adw.ComboRow(
                title=title,
                subtitle=subtitle,
                model=Gtk.StringList.new(labels),
            )
            current_action = config.get_receive_action(kind)
            try:
                row.set_selected(actions.index(current_action))
            except ValueError:
                row.set_selected(0)

            def on_receive_action_changed(combo, _pspec, k=kind, acts=actions):
                selected = combo.get_selected()
                if 0 <= selected < len(acts):
                    config.set_receive_action(k, acts[selected])

            row.connect("notify::selected", on_receive_action_changed)
            receive_group.add(row)

        # Receive action flood protection
        flood_group = Adw.PreferencesGroup(title="Receive Action Flood Protection")
        content.append(flood_group)

        reset_row = Adw.ActionRow(
            title="Flood limits",
            subtitle="0 means unlimited",
        )
        reset_btn = Gtk.Button(label="Reset to defaults", valign=Gtk.Align.CENTER)
        reset_row.add_suffix(reset_btn)
        flood_group.add(reset_row)

        limit_spinbuttons: dict[tuple[str, str], Gtk.SpinButton] = {}

        def make_limit_spin(action_key: str, limit_name: str) -> Gtk.SpinButton:
            value = config.get_receive_action_limits(action_key)[limit_name]
            adjustment = Gtk.Adjustment(
                value=float(value),
                lower=0.0,
                upper=float(RECEIVE_ACTION_LIMIT_MAX),
                step_increment=1.0,
                page_increment=10.0,
            )
            spin = Gtk.SpinButton(
                adjustment=adjustment,
                climb_rate=1.0,
                digits=0,
                numeric=True,
                valign=Gtk.Align.CENTER,
            )
            spin.set_width_chars(3)

            def on_limit_changed(widget, _action_key=action_key, _limit_name=limit_name):
                config.set_receive_action_limit(
                    _action_key,
                    _limit_name,
                    int(widget.get_value()),
                )

            spin.connect("value-changed", on_limit_changed)
            limit_spinbuttons[(action_key, limit_name)] = spin
            return spin

        receive_action_limit_rows = (
            (RECEIVE_ACTION_KEY_URL_OPEN, "Open URL"),
            (RECEIVE_ACTION_KEY_URL_COPY, "Copy URL to clipboard"),
            (RECEIVE_ACTION_KEY_TEXT_COPY, "Copy text to clipboard"),
            (RECEIVE_ACTION_KEY_IMAGE_OPEN, "Open image"),
            (RECEIVE_ACTION_KEY_VIDEO_OPEN, "Open video"),
            (RECEIVE_ACTION_KEY_DOCUMENT_OPEN, "Open document"),
        )

        limits_table = Gtk.Grid(
            column_spacing=16,
            row_spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        flood_group.add(limits_table)

        table_headers = ("Action type", "Max per batch", "Max per minute")
        for column, header in enumerate(table_headers):
            label = Gtk.Label(label=header, xalign=0.0)
            label.add_css_class("dim-label")
            label.add_css_class("caption-heading")
            limits_table.attach(label, column, 0, 1, 1)

        for row_index, (action_key, title) in enumerate(receive_action_limit_rows, start=1):
            action_label = Gtk.Label(
                label=title,
                xalign=0.0,
                valign=Gtk.Align.CENTER,
                hexpand=True,
            )
            limits_table.attach(action_label, 0, row_index, 1, 1)
            limits_table.attach(
                make_limit_spin(action_key, RECEIVE_ACTION_LIMIT_BATCH),
                1,
                row_index,
                1,
                1,
            )
            limits_table.attach(
                make_limit_spin(action_key, RECEIVE_ACTION_LIMIT_MINUTE),
                2,
                row_index,
                1,
                1,
            )

        def on_reset_limits(btn):
            config.reset_receive_action_limits()
            for action_key, limits in DEFAULT_RECEIVE_ACTION_LIMITS.items():
                for limit_name, value in limits.items():
                    spin = limit_spinbuttons.get((action_key, limit_name))
                    if spin is not None:
                        spin.set_value(float(value))
            btn.set_label("\u2713 Reset")
            btn.set_sensitive(False)

            def restore_reset_label() -> bool:
                btn.set_label("Reset to defaults")
                btn.set_sensitive(True)
                return False

            GLib.timeout_add(2000, restore_reset_label)

        reset_btn.connect("clicked", on_reset_limits)

        def add_logs_group():
            logs_group = Adw.PreferencesGroup(title="Logs")
            content.append(logs_group)

            log_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            log_switch.set_active(config.allow_logging)
            log_switch.connect("notify::active", lambda sw, _: setattr(config, 'allow_logging', sw.get_active()))
            log_toggle_row = Adw.ActionRow(title="Allow logging", subtitle="Write logs to file (requires restart)")
            log_toggle_row.add_suffix(log_switch)
            log_toggle_row.set_activatable_widget(log_switch)
            logs_group.add(log_toggle_row)

            log_dir = config_dir / "logs"
            log_files = sorted(log_dir.glob("desktop-connector.log*")) if log_dir.exists() else []
            total_size = sum(f.stat().st_size for f in log_files) if log_files else 0
            log_size_text = _format_bytes(total_size) if total_size > 0 else "No logs"

            log_row = Adw.ActionRow(title="Log files", subtitle=log_size_text)
            download_btn = Gtk.Button(label="Download Logs", valign=Gtk.Align.CENTER)

            def on_download_logs(btn):
                import shutil, subprocess as _sp
                downloads = Path.home() / "Downloads"
                downloads.mkdir(exist_ok=True)
                dest = downloads / f"desktop-connector-logs-{time.strftime('%Y%m%d-%H%M%S')}"
                dest.mkdir(exist_ok=True)
                copied = 0
                for f in (log_dir.glob("desktop-connector.log*") if log_dir.exists() else []):
                    shutil.copy2(f, dest / f.name)
                    copied += 1
                if copied > 0:
                    _sp.Popen(["xdg-open", str(dest)])
                    btn.set_label(f"\u2713 Saved to Downloads")
                    btn.set_sensitive(False)
                    GLib.timeout_add(3000, lambda: (btn.set_label("Download Logs"), btn.set_sensitive(True), False)[-1])
                else:
                    btn.set_label("No logs found")
                    GLib.timeout_add(2000, lambda: (btn.set_label("Download Logs"), btn.set_sensitive(True), False)[-1])

            download_btn.connect("clicked", on_download_logs)
            log_row.add_suffix(download_btn)

            clear_btn = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
            clear_btn.add_css_class("destructive-action")

            def on_clear_logs(btn):
                cleared = 0
                # Truncate (don't unlink) so any running logger keeps its file handle.
                for f in (log_dir.glob("desktop-connector.log*") if log_dir.exists() else []):
                    try:
                        f.open("w").close()
                        cleared += 1
                    except OSError:
                        pass
                log_row.set_subtitle("No logs")
                if cleared > 0:
                    btn.set_label("\u2713 Cleared")
                else:
                    btn.set_label("No logs found")
                btn.set_sensitive(False)
                GLib.timeout_add(2000, lambda: (btn.set_label("Clear"), btn.set_sensitive(True), False)[-1])

            clear_btn.connect("clicked", on_clear_logs)
            log_row.add_suffix(clear_btn)
            logs_group.add(log_row)

        # This device
        device_group = Adw.PreferencesGroup(title="This Device")
        content.append(device_group)
        device_group.add(Adw.ActionRow(title="Name", subtitle=config.device_name))
        device_group.add(Adw.ActionRow(title="Device ID", subtitle=crypto.get_device_id()[:24] + "..."))

        # Connected devices list
        pair_group = Adw.PreferencesGroup(title="Connected Devices")
        content.append(pair_group)

        paired_devices = settings_registry.list_devices()
        active_device_id = (
            settings_active_device.device_id if settings_active_device else None
        )

        def open_rename_dialog(target_id: str, current_name: str):
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="Rename connected device",
                body="Choose a unique name for this connected device.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("save", "Save")
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

            entry_group = Adw.PreferencesGroup()
            name_entry = Adw.EntryRow(title="Name")
            name_entry.set_text(current_name)
            entry_group.add(name_entry)

            error_label = Gtk.Label(label="")
            error_label.add_css_class("error")
            error_label.set_wrap(True)
            error_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            error_label.set_xalign(0)
            error_label.set_visible(False)
            error_label.set_margin_top(8)

            extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            extra.append(entry_group)
            extra.append(error_label)
            dialog.set_extra_child(extra)

            def on_response(dlg, response):
                if response != "save":
                    dlg.close()
                    return
                try:
                    settings_registry.rename(target_id, name_entry.get_text())
                except DuplicateDeviceNameError:
                    error_label.set_label(
                        "This name is already used by another device."
                    )
                    error_label.set_visible(True)
                    return
                except DeviceRegistryError:
                    error_label.set_label("Name cannot be empty.")
                    error_label.set_visible(True)
                    return
                try:
                    sync_file_manager_targets(config)
                except Exception:
                    pass
                dlg.close()
                win.close()

            dialog.connect("response", on_response)
            # Block default close-on-response so validation can keep the
            # dialog open when the user enters a duplicate or empty name.
            dialog.set_close_response("cancel")
            dialog.present()

        def open_unpair_dialog(target_id: str, target_name: str, target_info: dict):
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="Unpair?",
                body=f"Disconnect from \"{target_name}\"?\nYou will need to pair again.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("unpair", "Unpair")
            dialog.set_response_appearance("unpair", Adw.ResponseAppearance.DESTRUCTIVE)

            def on_response(dlg, response):
                if response != "unpair":
                    return
                # Notify only this specific device. The remote side
                # mirrors the unpair via .fn.unpair so both sides stay
                # in sync; if the notify hops fail (offline, auth lost,
                # quota), log loudly — the local pairing is still
                # removed below, leaving the remote believing it's
                # paired until they hit a 403 of their own.
                import logging
                import os as _os
                import tempfile
                log = logging.getLogger("desktop-connector.settings")
                tmp_path: Path | None = None
                try:
                    sym_key = base64.b64decode(target_info["symmetric_key_b64"])
                    fd, tmp_name = tempfile.mkstemp(suffix="_.fn.unpair")
                    tmp_path = Path(tmp_name)
                    with _os.fdopen(fd, "wb") as fh:
                        fh.write(b"unpair")
                    conn_tmp = ConnectionManager(
                        config.server_url,
                        config.device_id or "",
                        config.auth_token or "",
                    )
                    api_tmp = ApiClient(conn_tmp, crypto)
                    api_tmp.send_file(
                        tmp_path, target_id, sym_key, filename_override=".fn.unpair"
                    )
                except Exception:
                    log.warning(
                        "pairing.unpair.notify_failed peer=%s",
                        target_id[:12],
                        exc_info=True,
                    )
                finally:
                    if tmp_path is not None:
                        try:
                            tmp_path.unlink(missing_ok=True)
                        except OSError:
                            log.debug(
                                "pairing.unpair.tmp_cleanup_failed",
                                exc_info=True,
                            )
                # Remove only this pairing through Config so the keyring
                # entry is cleaned up alongside JSON metadata. The
                # registry helper also clears active_device_id when it
                # was pointing at the unpaired device.
                settings_registry.unpair(target_id)
                try:
                    sync_file_manager_targets(config)
                except Exception:
                    pass
                win.close()

            dialog.connect("response", on_response)
            dialog.present()

        if paired_devices:
            for device in paired_devices:
                target_id = device.device_id
                target_name = device.name or "Unknown"
                if target_id == active_device_id:
                    subtitle = f"{target_id[:24]}…  ·  Active"
                else:
                    subtitle = f"{target_id[:24]}…"
                row = Adw.ActionRow(title=target_name, subtitle=subtitle)

                rename_btn = Gtk.Button(
                    label="Rename", valign=Gtk.Align.CENTER,
                )
                rename_btn.add_css_class("flat")
                rename_btn.connect(
                    "clicked",
                    lambda _b, tid=target_id, nm=target_name: open_rename_dialog(tid, nm),
                )
                row.add_suffix(rename_btn)

                target_info = config.paired_devices.get(target_id, {})
                unpair_btn = Gtk.Button(
                    label="Unpair", valign=Gtk.Align.CENTER,
                )
                unpair_btn.add_css_class("destructive-action")
                unpair_btn.connect(
                    "clicked",
                    lambda _b, tid=target_id, nm=target_name, info=target_info:
                        open_unpair_dialog(tid, nm, info),
                )
                row.add_suffix(unpair_btn)

                pair_group.add(row)
        else:
            pair_group.add(Adw.ActionRow(
                title="No connected devices",
                subtitle="Use Pair… from the tray menu",
            ))

        # Add-pairing entry-point. Mirrors Android's "Pair with another
        # desktop" row in PairingsCard. Spawning the pairing window
        # from here works whether or not the user already has a pair.
        add_pair_row = Adw.ActionRow(
            title="Pair another device",
            subtitle="Open the QR code window to add a new connected device",
            activatable=True,
        )
        add_pair_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        add_pair_row.add_prefix(add_pair_icon)
        add_pair_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )

        def on_add_pair(_row):
            import os as _os
            import subprocess as _subprocess
            import sys as _sys
            appimage = _os.environ.get("APPIMAGE")
            cmd = (
                [appimage, "--gtk-window=pairing",
                 f"--config-dir={config.config_dir}"]
                if appimage else
                [_sys.executable, "-m", "src.windows", "pairing",
                 f"--config-dir={config.config_dir}"]
            )
            cwd = (None if appimage
                   else str(Path(__file__).resolve().parent.parent))
            _subprocess.Popen(cmd, cwd=cwd)
            win.close()

        add_pair_row.connect("activated", on_add_pair)
        pair_group.add(add_pair_row)

        # Connection statistics (only when at least one pair + stats fetched)
        if stats and paired_devices:
            stats_group = Adw.PreferencesGroup(title="Connection Statistics")
            content.append(stats_group)

            paired_devs_stats = stats.get("paired_devices", [])
            stats_by_id = {
                d.get("device_id"): d for d in paired_devs_stats if d.get("device_id")
            }
            for device in paired_devices:
                pd = stats_by_id.get(device.device_id)
                if not pd:
                    continue
                online = pd.get("online", False)
                last_seen = pd.get("last_seen", 0)
                if online:
                    status_str = "Online"
                elif last_seen:
                    ago = int(time.time()) - last_seen
                    if ago < 60:
                        status_str = "Last seen just now"
                    elif ago < 3600:
                        status_str = f"Last seen {ago // 60} min ago"
                    elif ago < 86400:
                        status_str = f"Last seen {ago // 3600}h ago"
                    else:
                        status_str = f"Last seen {time.strftime('%b %d, %H:%M', time.localtime(last_seen))}"
                else:
                    status_str = "Offline"
                pair_label = device.name or device.device_id[:12]
                stats_group.add(Adw.ActionRow(
                    title=f"{pair_label} — status",
                    subtitle=status_str,
                ))
                stats_group.add(Adw.ActionRow(
                    title=f"{pair_label} — transfers",
                    subtitle=str(pd.get("transfers", 0)),
                ))
                stats_group.add(Adw.ActionRow(
                    title=f"{pair_label} — data",
                    subtitle=_format_bytes(pd.get("bytes_transferred", 0)),
                ))
                paired_since = pd.get("paired_since", 0)
                if paired_since:
                    stats_group.add(Adw.ActionRow(
                        title=f"{pair_label} — paired since",
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

        # Security: verify secret storage (H.6). Surfaces the active
        # backend's status and gives the user a way to re-scrub
        # plaintext after manual config.json edits or a partial-failure
        # boot. Settings open already triggers Config init's automatic
        # migration, so on first display the row almost always reads
        # "Already clean".
        sec_group = Adw.PreferencesGroup(title="Security")
        content.append(sec_group)

        _secure_now = config.is_secret_storage_secure()
        verify_row = Adw.ActionRow(
            title="Secret storage",
            subtitle=(
                "Identity, auth token + pairing keys in OS keyring "
                "(libsecret / KWallet)"
                if _secure_now else
                "Identity, auth token + pairing keys in plaintext "
                "~/.config/desktop-connector/ (no keyring backend "
                "reachable)"
            ),
        )
        sec_group.add(verify_row)

        # Info icon: tooltip on hover, click opens an explainer dialog.
        info_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        info_btn.set_child(Gtk.Image.new_from_icon_name(
            "dialog-information-symbolic",
        ))
        info_btn.add_css_class("flat")
        info_btn.add_css_class("circular")
        info_btn.set_tooltip_text(
            "Verify scans config.json for plaintext secrets and "
            "migrates them into the OS keyring. Click for details."
        )

        def _on_secret_info(_btn):
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="About verify secret storage",
                body=(
                    "Verify scans for plaintext secret material and "
                    "moves anything it finds into the OS keyring. "
                    "Three locations are checked:\n\n"
                    "  • config.json → auth_token field\n"
                    "  • config.json → paired_devices[*].symmetric_key_b64\n"
                    "  • keys/private_key.pem → long-term device "
                    "identity key\n\n"
                    "Useful when:\n"
                    "  • You've manually edited config.json or "
                    "restored a private_key.pem from backup\n"
                    "  • A previous launch's automatic migration "
                    "failed (e.g. the keyring was locked at startup)\n"
                    "  • You want to confirm secrets are stored "
                    "correctly\n\n"
                    "Verify does NOT change cryptographic identity, "
                    "doesn't re-pair, and doesn't rotate keys. It "
                    "only ensures plaintext doesn't accumulate "
                    "outside the keyring."
                ),
            )
            dialog.add_response("close", "Close")
            dialog.set_default_response("close")
            dialog.present()

        info_btn.connect("clicked", _on_secret_info)
        verify_row.add_suffix(info_btn)

        verify_btn = Gtk.Button(label="Verify", valign=Gtk.Align.CENTER)
        verify_btn.add_css_class("pill")

        def _on_verify_secret_storage(_btn):
            result = config.scrub_secrets()
            # H.7 also covers the private-key PEM. crypto already
            # ran a migration check in __init__ (window-spawn time);
            # re-running here picks up anything that arrived since.
            pem_scrubbed = (
                crypto.scrub_private_key() or crypto.was_pem_migrated
            )

            if not result.secure:
                verify_row.set_subtitle(
                    "Plaintext fallback active — no scrub possible. "
                    "Install gnome-keyring or kwallet and re-launch."
                )
                return
            if result.failed > 0:
                verify_row.set_subtitle(
                    f"Scrubbed {result.scrubbed}, {result.failed} "
                    "field(s) remain (keyring transient — re-try)"
                )
                return

            items: list[str] = []
            if result.scrubbed > 0:
                items.append(f"{result.scrubbed} field(s)")
            if pem_scrubbed:
                items.append("device private key")
            if items:
                verify_row.set_subtitle(
                    "\u2713 Scrubbed " + " + ".join(items) +
                    " into the keyring"
                )
            else:
                verify_row.set_subtitle(
                    "\u2713 Already clean — identity, auth token + "
                    "pairing keys all in keyring"
                )

        verify_btn.connect("clicked", _on_verify_secret_storage)
        verify_row.add_suffix(verify_btn)

        add_logs_group()

        # --- Footer: version + install shape ---------------------------------
        # $APPIMAGE is set by AppRun when running inside the AppImage; absent
        # for install-from-source.sh layouts and for dev-tree runs of
        # `python3 -m src.main`. Both non-AppImage paths get the same shape
        # label since they share the lifecycle (manual install / no in-app
        # updater).
        version_str = get_app_version()
        appimage_env = _os.environ.get("APPIMAGE")
        if appimage_env:
            shape_str = "AppImage release"
        else:
            shape_str = "Installed from source"

        version_label = Gtk.Label(label=f"Desktop Connector {version_str}",
                                   xalign=0.5)
        version_label.add_css_class("dim-label")
        version_label.add_css_class("caption-heading")
        version_label.set_margin_top(8)
        content.append(version_label)

        shape_label = Gtk.Label(label=shape_str, xalign=0.5)
        shape_label.add_css_class("dim-label")
        shape_label.add_css_class("caption")
        if appimage_env:
            shape_label.set_tooltip_text(appimage_env)
        content.append(shape_label)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── History Window ──────────────────────────────────────────────────

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
            direction_prefix = "\u2193" if item["direction"] == "received" else "\u2191"
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
                subtitle=f"{size}  \u00b7  {ts}  \u00b7  {status_text}",
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
                    body = (f"The download of \u201c{label}\u201d will be "
                            f"cancelled and the sender will see Aborted.")
                    action_label = "Stop download"
                    abort_reason = "recipient_abort"
                else:
                    heading = "Cancel delivery?"
                    body = (f"The recipient will no longer receive "
                            f"\u201c{label}\u201d.")
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
            row.set_subtitle(f"{size}  \u00b7  {ts}  \u00b7  {status_text}")

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


# ─── Pairing Window ──────────────────────────────────────────────────

def show_pairing(config_dir: Path):
    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .devices import (
        ConnectedDeviceRegistry,
        DeviceRegistryError,
        DuplicateDeviceNameError,
    )
    from .file_manager_integration import sync_file_manager_targets
    from .pairing import generate_qr_data, generate_qr_image
    from .pairing_key import (
        AlreadyPairedError,
        JoinRequestError,
        PairingHandshake,
        PairingKeyError,
        PairingKeyParseError,
        PairingKeySchemaError,
        RelayMismatchError,
        SelfPairError,
        begin_join,
        build_local_key,
        complete_join,
        decode as decode_pairing_key,
        default_filename,
        encode as encode_pairing_key,
        validate_for_join,
    )
    # windows.py runs as a GTK4 subprocess — Linux-scoped by construction,
    # so instantiate the Linux backend directly.
    from .backends.linux.dialog_backend import LinuxDialogBackend

    import io
    import logging
    import os as _os

    log = logging.getLogger("desktop-connector.pairing-key")

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")
    api = ApiClient(conn, crypto)
    dialogs = LinuxDialogBackend()

    qr_data = generate_qr_data(config, crypto)
    qr_pil = generate_qr_image(qr_data)
    server_url = json.loads(qr_data)["server"]
    device_id = crypto.get_device_id()

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Pair with Device",
                                     default_width=460, default_height=640)

        toolbar_view = Adw.ToolbarView()
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(toolbar_view)
        win.set_content(toast_overlay)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(200)
        toolbar_view.set_content(stack)

        # ── Shared state across pages ───────────────────────────
        # role tracks which side of the pair we're on at the
        # naming step. "inviter" means a phone scanned our QR (or a
        # joiner sent us their pairing key); "joiner" means we
        # entered/imported someone else's pairing key.
        role = ["inviter"]
        device_info = [None]      # inviter side: dict from poll_pairing
        derived_key = [None]      # inviter side: bytes
        joiner_handshake: list = [None]  # joiner side: PairingHandshake

        # ── QR + verification page (phone pairing, default) ─────
        qr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=16, margin_bottom=24, margin_start=24, margin_end=24,
                         halign=Gtk.Align.CENTER)
        stack.add_named(qr_box, "qr")

        title = Gtk.Label(label="Scan this QR code with your device")
        title.add_css_class("title-3")
        qr_box.append(title)

        server_label = Gtk.Label(label=server_url)
        server_label.add_css_class("dim-label")
        server_label.add_css_class("caption")
        qr_box.append(server_label)

        id_label = Gtk.Label(label=f"Device ID: {device_id[:16]}...")
        id_label.add_css_class("dim-label")
        id_label.add_css_class("caption")
        qr_box.append(id_label)

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
        qr_image.set_size_request(260, 260)
        qr_image.set_content_fit(Gtk.ContentFit.CONTAIN)
        qr_box.append(qr_image)

        status_label = Gtk.Label(label="Waiting for device to scan...")
        status_label.add_css_class("body")
        qr_box.append(status_label)

        code_label = Gtk.Label(label="")
        code_label.add_css_class("title-1")
        qr_box.append(code_label)

        qr_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                             halign=Gtk.Align.CENTER)
        qr_box.append(qr_btn_box)

        qr_cancel_btn = Gtk.Button(label="Cancel")
        qr_cancel_btn.connect("clicked", lambda b: win.close())
        qr_btn_box.append(qr_cancel_btn)

        confirm_btn = Gtk.Button(label="Confirm Pairing")
        confirm_btn.add_css_class("suggested-action")
        confirm_btn.set_sensitive(False)
        qr_btn_box.append(confirm_btn)

        # Mode-switch link — opens the desktop-mode page.
        pair_desktop_btn = Gtk.Button(label="Pair desktop instead")
        pair_desktop_btn.add_css_class("flat")
        pair_desktop_btn.set_halign(Gtk.Align.CENTER)
        pair_desktop_btn.connect(
            "clicked", lambda _b: stack.set_visible_child_name("desktop"),
        )
        qr_box.append(pair_desktop_btn)

        # ── Desktop-mode hub ───────────────────────────────────
        desktop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                              margin_top=16, margin_bottom=16, margin_start=24, margin_end=24)
        stack.add_named(desktop_box, "desktop")

        desktop_top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=8, halign=Gtk.Align.START)
        desktop_box.append(desktop_top_row)

        pair_phone_btn = Gtk.Button(label="Pair phone instead")
        pair_phone_btn.add_css_class("flat")
        pair_phone_btn.connect(
            "clicked", lambda _b: stack.set_visible_child_name("qr"),
        )
        desktop_top_row.append(pair_phone_btn)

        desktop_title = Gtk.Label(label="Pair with another desktop", xalign=0)
        desktop_title.add_css_class("title-3")
        desktop_box.append(desktop_title)

        desktop_subtitle = Gtk.Label(
            label=(
                "Exchange a pairing key with the other desktop through any "
                "channel you trust — copy/paste through chat, or save to a "
                "file and transfer it. The verification code on both screens "
                "must match before either side confirms."
            ),
            xalign=0, wrap=True,
        )
        desktop_subtitle.add_css_class("dim-label")
        desktop_subtitle.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        desktop_box.append(desktop_subtitle)

        # Group 1 — Share your pairing key (inviter side)
        share_group = Adw.PreferencesGroup(title="Share your pairing key")
        desktop_box.append(share_group)

        show_key_row = Adw.ActionRow(
            title="Show pairing key",
            subtitle="Display the key as text to copy and send",
            activatable=True,
        )
        show_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        share_group.add(show_key_row)

        export_key_row = Adw.ActionRow(
            title="Export pairing key",
            subtitle=f"Save as a {default_filename(build_local_key(config, crypto))[-7:]} file",
            activatable=True,
        )
        export_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        share_group.add(export_key_row)

        # Group 2 — Use someone else's pairing key (joiner side)
        join_group = Adw.PreferencesGroup(
            title="Use someone else's pairing key",
        )
        desktop_box.append(join_group)

        enter_key_row = Adw.ActionRow(
            title="Enter pairing key",
            subtitle="Paste the key text from the other desktop",
            activatable=True,
        )
        enter_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        join_group.add(enter_key_row)

        import_key_row = Adw.ActionRow(
            title="Import pairing key",
            subtitle="Open a .dcpair file from the other desktop",
            activatable=True,
        )
        import_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        join_group.add(import_key_row)

        # Live status row at the bottom of the desktop hub. Updated
        # by the same poll loop that drives the QR page.
        desktop_status = Gtk.Label(
            label="Waiting for an incoming pair request…",
            xalign=0,
        )
        desktop_status.add_css_class("dim-label")
        desktop_box.append(desktop_status)

        # ── Joiner verification page ───────────────────────────
        join_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                           margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
                           halign=Gtk.Align.CENTER)
        stack.add_named(join_box, "join")

        join_title = Gtk.Label(label="Verify pairing")
        join_title.add_css_class("title-3")
        join_box.append(join_title)

        join_subtitle = Gtk.Label(
            label=(
                "Confirm this code matches the verification code shown on "
                "the other desktop's pairing window before clicking Confirm."
            ),
            xalign=0, wrap=True,
        )
        join_subtitle.add_css_class("dim-label")
        join_subtitle.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        join_box.append(join_subtitle)

        join_status = Gtk.Label(label="", xalign=0)
        join_status.add_css_class("body")
        join_box.append(join_status)

        join_code = Gtk.Label(label="")
        join_code.add_css_class("title-1")
        join_box.append(join_code)

        join_help = Gtk.Label(
            label=(
                "If the other desktop never confirms, the pair won't take "
                "effect — try opening its pairing window and trying again."
            ),
            xalign=0, wrap=True,
        )
        join_help.add_css_class("dim-label")
        join_help.add_css_class("caption")
        join_help.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        join_box.append(join_help)

        join_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                               halign=Gtk.Align.CENTER, margin_top=8)
        join_box.append(join_btn_box)

        join_cancel_btn = Gtk.Button(label="Cancel")
        join_cancel_btn.connect(
            "clicked",
            lambda _b: stack.set_visible_child_name("desktop"),
        )
        join_btn_box.append(join_cancel_btn)

        join_confirm_btn = Gtk.Button(label="Confirm Pairing")
        join_confirm_btn.add_css_class("suggested-action")
        join_btn_box.append(join_confirm_btn)

        # ── Naming page (shared) ────────────────────────────────
        naming_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        stack.add_named(naming_box, "naming")

        naming_title = Gtk.Label(label="Name this device")
        naming_title.add_css_class("title-3")
        naming_title.set_xalign(0)
        naming_box.append(naming_title)

        naming_subtitle = Gtk.Label(
            label="Choose how this connected device appears in lists, history, and file-manager send targets."
        )
        naming_subtitle.add_css_class("dim-label")
        naming_subtitle.add_css_class("body")
        naming_subtitle.set_wrap(True)
        naming_subtitle.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        naming_subtitle.set_xalign(0)
        naming_box.append(naming_subtitle)

        name_group = Adw.PreferencesGroup()
        naming_box.append(name_group)

        name_row = Adw.EntryRow(title="Name")
        name_group.add(name_row)

        naming_error = Gtk.Label(label="")
        naming_error.add_css_class("error")
        naming_error.set_wrap(True)
        naming_error.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        naming_error.set_xalign(0)
        naming_error.set_visible(False)
        naming_box.append(naming_error)

        naming_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                                 halign=Gtk.Align.END, margin_top=8)
        naming_box.append(naming_btn_box)

        naming_cancel_btn = Gtk.Button(label="Cancel")
        naming_cancel_btn.connect("clicked", lambda b: win.close())
        naming_btn_box.append(naming_cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        naming_btn_box.append(save_btn)

        stack.set_visible_child_name("qr")

        # ── Inviter side: Confirm Pairing on the QR page ───────
        def on_confirm_inviter(_b):
            if not device_info[0]:
                return
            role[0] = "inviter"
            registry = ConnectedDeviceRegistry(config)
            name_row.set_text(registry.next_default_name())
            naming_error.set_visible(False)
            stack.set_visible_child_name("naming")
            name_row.grab_focus()

        confirm_btn.connect("clicked", on_confirm_inviter)

        # ── Joiner side: Confirm Pairing on the join page ──────
        def on_confirm_joiner(_b):
            if not joiner_handshake[0]:
                return
            role[0] = "joiner"
            registry = ConnectedDeviceRegistry(config)
            name_row.set_text(
                joiner_handshake[0].key.name
                or registry.next_default_name(),
            )
            naming_error.set_visible(False)
            stack.set_visible_child_name("naming")
            name_row.grab_focus()

        join_confirm_btn.connect("clicked", on_confirm_joiner)

        # ── Naming page save handler (branches on role) ─────────
        def show_naming_error(message: str):
            naming_error.set_label(message)
            naming_error.set_visible(True)

        def on_save(_b):
            registry = ConnectedDeviceRegistry(config)
            try:
                normalized = registry.validate_unique_name(name_row.get_text())
            except DuplicateDeviceNameError:
                show_naming_error("This name is already used by another device.")
                return
            except DeviceRegistryError:
                show_naming_error("Name cannot be empty.")
                return

            if role[0] == "joiner":
                handshake = joiner_handshake[0]
                if handshake is None:
                    show_naming_error("Pairing handshake is not ready.")
                    return
                try:
                    complete_join(
                        handshake,
                        config=config,
                        name=normalized,
                        on_synced=lambda: sync_file_manager_targets(config),
                    )
                except Exception as exc:
                    log.exception("pairing.key.complete_join_failed")
                    show_naming_error(f"Could not save pairing: {exc}")
                    return
            else:
                info = device_info[0]
                if info is None:
                    show_naming_error("Pairing request is no longer valid.")
                    return
                sym_key = derived_key[0]
                if sym_key is None:
                    sym_key = crypto.derive_shared_key(info["phone_pubkey"])
                config.add_paired_device(
                    device_id=info["phone_id"],
                    pubkey=info["phone_pubkey"],
                    symmetric_key_b64=base64.b64encode(sym_key).decode(),
                    name=normalized,
                )
                api.confirm_pairing(info["phone_id"])
                try:
                    registry.mark_active(info["phone_id"], reason="paired")
                except DeviceRegistryError:
                    pass
                try:
                    sync_file_manager_targets(config)
                except Exception:
                    pass

            naming_title.set_label("Paired!")
            save_btn.set_sensitive(False)
            naming_cancel_btn.set_sensitive(False)
            GLib.timeout_add(800, win.close)

        save_btn.connect("clicked", on_save)

        # ── Inviter poll loop (also serves desktop hub) ─────────
        def poll_pairing():
            if not win.is_visible():
                return False
            requests_list = api.poll_pairing()
            paired_ids = set(config.paired_devices.keys()) if requests_list else set()
            for req in requests_list:
                if req["phone_id"] in paired_ids:
                    log.info(
                        "pairing.request.ignored_already_paired peer=%s",
                        req["phone_id"][:12],
                    )
                    continue
                device_info[0] = req
                sym_key = crypto.derive_shared_key(req["phone_pubkey"])
                derived_key[0] = sym_key
                code = KeyManager.get_verification_code(sym_key)
                msg = f"Device connected: {req['phone_id'][:12]}...  Verify code:"
                status_label.set_text(msg)
                code_label.set_text(code)
                confirm_btn.set_sensitive(True)
                desktop_status.set_text(
                    "Incoming pair request — review the verification code on the QR tab."
                )
                # Auto-switch to QR page so the user sees the verification.
                if stack.get_visible_child_name() == "desktop":
                    stack.set_visible_child_name("qr")
                return False
            return True

        GLib.timeout_add(2000, poll_pairing)

        # ── Show pairing key dialog (string surface) ───────────
        def on_show_pairing_key(_row):
            key = build_local_key(config, crypto)
            text = encode_pairing_key(key)
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="Your pairing key",
                body=(
                    "Send this key to the other desktop through a channel "
                    "you trust. Anyone with the key can request to pair "
                    "with this desktop while this window is open. The "
                    "verification code on both screens must match before "
                    "either side confirms."
                ),
            )
            extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            text_view = Gtk.TextView()
            text_view.set_editable(False)
            text_view.set_monospace(True)
            text_view.set_wrap_mode(Gtk.WrapMode.CHAR)
            text_view.get_buffer().set_text(text)
            scroller = Gtk.ScrolledWindow()
            scroller.set_min_content_height(120)
            scroller.set_hexpand(True)
            scroller.set_child(text_view)
            extra.append(scroller)
            dialog.set_extra_child(extra)
            dialog.add_response("close", "Done")
            dialog.add_response("copy", "Copy")
            dialog.set_response_appearance(
                "copy", Adw.ResponseAppearance.SUGGESTED,
            )
            dialog.set_default_response("copy")

            def on_response(dlg, response):
                if response == "copy":
                    clipboard = win.get_clipboard()
                    clipboard.set(text)
                    toast_overlay.add_toast(
                        Adw.Toast(title="Pairing key copied", timeout=2),
                    )
                dlg.close()

            dialog.connect("response", on_response)
            log.info("pairing.key.shown")
            dialog.present()

        show_key_row.connect("activated", on_show_pairing_key)

        # ── Export pairing key dialog (file surface) ───────────
        def on_export_pairing_key(_row):
            key = build_local_key(config, crypto)
            text = encode_pairing_key(key)
            chosen = dialogs.save_file(
                "Export pairing key",
                default_filename=default_filename(key),
                file_types=(("Pairing key", "*.dcpair"),),
            )
            if chosen is None:
                return
            try:
                # Write atomically with restrictive perms — same bucket
                # as identity material.
                tmp = chosen.with_suffix(chosen.suffix + ".tmp")
                tmp.write_text(text)
                try:
                    _os.chmod(tmp, 0o600)
                except OSError:
                    pass
                tmp.replace(chosen)
            except OSError as exc:
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=f"Could not write file: {exc}", timeout=4,
                    ),
                )
                log.warning(
                    "pairing.key.export_failed err=%s",
                    type(exc).__name__,
                )
                return
            log.info("pairing.key.exported path=%s", chosen)
            toast_overlay.add_toast(
                Adw.Toast(
                    title=f"Pairing key exported to {chosen.name}", timeout=3,
                ),
            )

        export_key_row.connect("activated", on_export_pairing_key)

        # ── Joiner: present the verification page after parse+validate ──
        def begin_joiner_session(text: str, *, surface: str) -> None:
            try:
                key = decode_pairing_key(text)
            except (PairingKeyParseError, PairingKeySchemaError) as exc:
                log.warning(
                    "pairing.key.import_parse_failed surface=%s err=%s",
                    surface, type(exc).__name__,
                )
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=f"Pairing key is malformed: {exc}", timeout=4,
                    ),
                )
                return
            try:
                validate_for_join(key, config=config, crypto=crypto)
            except SelfPairError:
                log.warning("pairing.key.import_self_pair_refused")
                toast_overlay.add_toast(
                    Adw.Toast(
                        title="This pairing key is from this same desktop.",
                        timeout=4,
                    ),
                )
                return
            except RelayMismatchError as exc:
                # Hostnames only — full URL may carry tokens we don't
                # want in logs.
                from urllib.parse import urlsplit
                local_host = urlsplit(exc.local).netloc
                remote_host = urlsplit(exc.remote).netloc
                log.warning(
                    "pairing.key.import_relay_mismatched local=%s remote=%s",
                    local_host, remote_host,
                )
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=(
                            f"Different relay servers ({remote_host}). Both "
                            f"desktops must be configured for the same relay."
                        ),
                        timeout=6,
                    ),
                )
                return
            except AlreadyPairedError as exc:
                log.warning(
                    "pairing.key.import_already_paired_refused peer=%s",
                    exc.device_id[:12],
                )
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=(
                            f"Already paired with \"{exc.name}\". Unpair "
                            f"first if you want to re-pair."
                        ),
                        timeout=6,
                    ),
                )
                return
            except PairingKeyError as exc:
                toast_overlay.add_toast(
                    Adw.Toast(title=str(exc), timeout=4),
                )
                return

            try:
                handshake = begin_join(
                    key, crypto=crypto,
                    send_pairing_request=api.send_pairing_request,
                )
            except JoinRequestError as exc:
                log.warning(
                    "pairing.key.import_request_failed peer=%s",
                    key.device_id[:12],
                )
                toast_overlay.add_toast(
                    Adw.Toast(title=str(exc), timeout=6),
                )
                return

            joiner_handshake[0] = handshake
            log.info(
                "pairing.request.sent_as_joiner target=%s",
                key.device_id[:12],
            )
            join_status.set_text(
                f"Pairing with {handshake.key.name} ({handshake.key.device_id[:12]}…)",
            )
            join_code.set_text(handshake.verification_code)
            stack.set_visible_child_name("join")

        # ── Enter pairing key dialog (string surface) ──────────
        def on_enter_pairing_key(_row):
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="Enter pairing key",
                body=(
                    "Paste the pairing key text the other desktop shared. "
                    "It typically starts with `dc-pair:`."
                ),
            )
            extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            text_view = Gtk.TextView()
            text_view.set_monospace(True)
            text_view.set_wrap_mode(Gtk.WrapMode.CHAR)
            text_view.set_accepts_tab(False)
            scroller = Gtk.ScrolledWindow()
            scroller.set_min_content_height(120)
            scroller.set_hexpand(True)
            scroller.set_child(text_view)
            extra.append(scroller)
            dialog.set_extra_child(extra)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("continue", "Continue")
            dialog.set_response_appearance(
                "continue", Adw.ResponseAppearance.SUGGESTED,
            )
            dialog.set_default_response("continue")

            def on_response(dlg, response):
                if response == "continue":
                    buf_obj = text_view.get_buffer()
                    start = buf_obj.get_start_iter()
                    end = buf_obj.get_end_iter()
                    text = buf_obj.get_text(start, end, False)
                    dlg.close()
                    begin_joiner_session(text, surface="text")
                else:
                    dlg.close()

            dialog.connect("response", on_response)
            dialog.present()
            text_view.grab_focus()

        enter_key_row.connect("activated", on_enter_pairing_key)

        # ── Import pairing key dialog (file surface) ───────────
        def on_import_pairing_key(_row):
            paths = dialogs.pick_files("Import pairing key")
            if not paths:
                return
            path = paths[0]
            try:
                text = path.read_text()
            except OSError as exc:
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=f"Could not read file: {exc}", timeout=4,
                    ),
                )
                return
            begin_joiner_session(text, surface="file")

        import_key_row.connect("activated", on_import_pairing_key)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── Find My Device Window ──────────────────────────────────────────

def show_find_phone(config_dir: Path):
    import logging
    log = logging.getLogger("desktop-connector.find-phone")

    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .devices import ConnectedDeviceRegistry, DeviceRegistryError
    from .messaging import FasttrackAdapter, MessageType

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)

    def decode_target_find_device_update(raw: dict, target_id: str, symmetric_key: bytes):
        if (raw.get("sender_id") or "") != target_id:
            return None
        mid = raw.get("id")
        enc_data = raw.get("encrypted_data", "")
        try:
            enc_bytes = base64.b64decode(enc_data)
            plain = crypto.decrypt_blob(enc_bytes, symmetric_key)
            resp = json.loads(plain)
        except Exception as exc:
            log.error("Decrypt failed: %s", exc)
            return None
        if not isinstance(resp, dict):
            return None
        msg = FasttrackAdapter.to_device_message(resp)
        if not msg or msg.type != MessageType.FIND_PHONE_LOCATION_UPDATE:
            return None
        return mid, resp

    # Check WebKit availability
    has_webkit = False
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit
        has_webkit = True
    except (ValueError, ImportError):
        pass

    MAP_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9/dist/leaflet.js"></script>
<style>body{margin:0;background:#1e1e1e}#map{width:100%;height:100vh}</style>
</head><body>
<div id="map"></div>
<script>
var map = L.map('map').setView([0,0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'OSM', maxZoom:19}).addTo(map);
var marker = null;
var circle = null;
function updatePos(lat,lng,acc) {
  if (!marker) {
    marker = L.marker([lat,lng]).addTo(map);
    map.setView([lat,lng], 16);
  } else {
    marker.setLatLng([lat,lng]);
    map.panTo([lat,lng]);
  }
  if (circle) map.removeLayer(circle);
  if (acc && acc > 0) {
    circle = L.circle([lat,lng], {radius:acc, color:'#3986FC',
      fillColor:'#3986FC', fillOpacity:0.15, weight:1}).addTo(map);
  }
}
</script>
</body></html>"""

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Find my Device",
                                     default_width=480, default_height=640)

        toolbar_view = Adw.ToolbarView()
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(toolbar_view)
        win.set_content(toast_overlay)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        toolbar_view.set_content(content)

        # Status
        status_label = Gtk.Label(label="Ready")
        status_label.add_css_class("title-3")
        content.append(status_label)

        # Connected-device picker
        device_picker, selected_device, paired_devices = _create_device_picker(
            config,
            title="Find my Device",
            subtitle="Connected device",
        )
        device_group = Adw.PreferencesGroup()
        device_group.add(device_picker)
        content.append(device_group)

        # Settings group
        settings_group = Adw.PreferencesGroup(title="Settings")
        content.append(settings_group)

        # Silent search toggle
        silent_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        silent_switch.set_active(False)
        silent_row = Adw.ActionRow(title="Silent search", subtitle="Track location without alarm (stolen device)")
        silent_row.add_suffix(silent_switch)
        silent_row.set_activatable_widget(silent_switch)
        settings_group.add(silent_row)

        # Volume slider
        volume_row = Adw.ActionRow(title="Volume")
        volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 100, 10)
        volume_scale.set_value(80)
        volume_scale.set_hexpand(True)
        volume_scale.set_valign(Gtk.Align.CENTER)
        volume_scale.set_draw_value(True)
        volume_scale.set_value_pos(Gtk.PositionType.RIGHT)
        volume_row.add_suffix(volume_scale)
        settings_group.add(volume_row)

        def on_silent_changed(sw, _):
            volume_scale.set_sensitive(not sw.get_active())
        silent_switch.connect("notify::active", on_silent_changed)

        # Action buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          halign=Gtk.Align.CENTER, margin_top=4)
        content.append(btn_box)

        start_btn = Gtk.Button(label="Start")
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.set_sensitive(selected_device[0] is not None)
        btn_box.append(start_btn)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("pill")
        stop_btn.set_visible(False)
        btn_box.append(stop_btn)

        def on_picker_changed(_combo, _pspec):
            # Idle states gate Start on a selection; while locating, the
            # picker is locked anyway so this stays a no-op.
            start_btn.set_sensitive(selected_device[0] is not None)
        device_picker.connect("notify::selected", on_picker_changed)

        # Map or fallback
        webview = [None]

        if has_webkit:
            from gi.repository import WebKit
            wv = WebKit.WebView()
            wv.set_vexpand(True)
            wv.set_hexpand(True)
            wv.set_size_request(-1, 250)
            wv.load_html(MAP_HTML, "about:blank")
            map_frame = Gtk.Frame()
            map_frame.set_child(wv)
            map_frame.set_overflow(Gtk.Overflow.HIDDEN)
            content.append(map_frame)
            webview[0] = wv
        else:
            map_placeholder = Gtk.Label(label="Map unavailable (install gir1.2-webkit-6.0)")
            map_placeholder.add_css_class("dim-label")
            map_placeholder.set_vexpand(True)
            content.append(map_placeholder)

        # Location info + open in browser
        loc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.append(loc_box)

        loc_label = Gtk.Label(label="", xalign=0, hexpand=True)
        loc_label.add_css_class("caption")
        loc_label.add_css_class("dim-label")
        loc_box.append(loc_label)

        open_map_btn = Gtk.Button(label="Open in Browser")
        open_map_btn.add_css_class("flat")
        open_map_btn.set_visible(False)
        loc_box.append(open_map_btn)

        # ── State ─────────────────────────────────────────────────
        # poll_generation: incremented on each Start. Old poll threads see mismatch and exit.
        # No shared mutable flags — eliminates all thread races.
        poll_generation = [0]
        last_lat = [None]
        last_lng = [None]
        is_silent = [False]
        shared_api = [None]
        shared_target = [None]
        shared_key = [None]

        LOST_COMMS_TIMEOUT = 20  # seconds with no heartbeat

        def set_ui(status_text, sliders_enabled, show_start, show_stop):
            status_label.set_text(status_text)
            volume_scale.set_sensitive(sliders_enabled and not silent_switch.get_active())
            silent_row.set_sensitive(sliders_enabled)
            # Picker locks while a session is in progress so the user
            # can't switch targets mid-locate. Re-enabling needs paired
            # devices to exist; otherwise the empty-list picker stays
            # insensitive.
            device_picker.set_sensitive(sliders_enabled and bool(paired_devices))
            start_btn.set_visible(show_start)
            start_btn.set_sensitive(
                show_start and not show_stop and selected_device[0] is not None
            )
            stop_btn.set_visible(show_stop)

        def update_location(lat, lng, accuracy):
            last_lat[0] = lat
            last_lng[0] = lng
            if lat is not None and lng is not None:
                acc_text = f"  |  ~{int(accuracy)}m" if accuracy else ""
                loc_label.set_text(f"{lat:.6f}, {lng:.6f}{acc_text}  |  {time.strftime('%H:%M:%S')}")
                open_map_btn.set_visible(True)
                if webview[0]:
                    acc_val = accuracy if accuracy else 0
                    webview[0].evaluate_javascript(
                        f"updatePos({lat},{lng},{acc_val})", -1, None, None, None, None, None)

        def on_open_map(btn):
            if last_lat[0] is not None:
                import subprocess
                url = f"https://www.openstreetmap.org/?mlat={last_lat[0]}&mlon={last_lng[0]}#map=16/{last_lat[0]}/{last_lng[0]}"
                subprocess.Popen(["xdg-open", url])
        open_map_btn.connect("clicked", on_open_map)

        def _send_stop(api, target_id, symmetric_key):
            payload = json.dumps({"fn": "find-phone", "action": "stop"}).encode()
            encrypted = crypto.encrypt_blob(payload, symmetric_key)
            encrypted_b64 = base64.b64encode(encrypted).decode()
            log.info("fasttrack.command.sent fn=find-phone action=stop recipient=%s", target_id[:12])
            api.fasttrack_send(target_id, encrypted_b64)

        def on_start(btn):
            target = selected_device[0]
            if target is None:
                toast_overlay.add_toast(Adw.Toast(title="No connected device selected", timeout=3))
                return
            if not target.symmetric_key_b64:
                toast_overlay.add_toast(Adw.Toast(
                    title="Cannot locate — pairing key missing for this device",
                    timeout=3,
                ))
                return

            target_id = target.device_id
            symmetric_key = base64.b64decode(target.symmetric_key_b64)
            volume = 0 if silent_switch.get_active() else int(volume_scale.get_value())
            is_silent[0] = silent_switch.get_active()

            # Advance generation — any old poll thread will see mismatch and exit
            poll_generation[0] += 1
            my_gen = poll_generation[0]

            set_ui("Sending command...", False, False, True)

            payload = json.dumps({
                "fn": "find-phone",
                "action": "start",
                "volume": volume,
                "timeout": 300,  # hardcoded 5 min, enforced on phone
            }).encode()
            encrypted = crypto.encrypt_blob(payload, symmetric_key)
            encrypted_b64 = base64.b64encode(encrypted).decode()

            def do_poll():
                conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")
                api = ApiClient(conn, crypto)
                shared_api[0] = api
                shared_target[0] = target_id
                shared_key[0] = symmetric_key

                # Flush only stale sender-side updates from this target.
                # Other pending fasttrack messages belong to the tray receiver.
                stale = api.fasttrack_pending()
                flushed_count = 0
                for m in stale:
                    decoded = decode_target_find_device_update(m, target_id, symmetric_key)
                    if decoded is None:
                        continue
                    mid, _resp = decoded
                    if mid:
                        api.fasttrack_ack(mid)
                        flushed_count += 1
                if flushed_count:
                    log.info("fasttrack.message.flushed_stale count=%d", flushed_count)

                log.info("fasttrack.command.sent fn=find-phone action=start volume=%d silent=%s recipient=%s",
                         volume, is_silent[0], target_id[:12])
                msg_id = api.fasttrack_send(target_id, encrypted_b64)
                if msg_id is None:
                    log.error("fasttrack.command.send_failed fn=find-phone")
                    GLib.idle_add(set_ui, "Failed to reach device", True, True, False)
                    return

                # D2: marking active happens only after a directed
                # device action is successfully queued.
                try:
                    ConnectedDeviceRegistry(config).mark_active(
                        target_id, reason="find_device_start",
                    )
                except DeviceRegistryError:
                    pass

                log.debug("fasttrack.command.polling message_id=%s", msg_id)
                last_heartbeat = time.time()
                comms_lost_shown = False

                while poll_generation[0] == my_gen:
                    time.sleep(3)
                    if poll_generation[0] != my_gen:
                        break

                    # Lost communication detection (fire UI update only once)
                    silence = time.time() - last_heartbeat
                    if silence > LOST_COMMS_TIMEOUT and not comms_lost_shown:
                        log.warning("No heartbeat for %.0fs", silence)
                        GLib.idle_add(set_ui, "Lost communication", False, True, True)
                        comms_lost_shown = True

                    try:
                        messages = api.fasttrack_pending()
                        for m in messages:
                            decoded = decode_target_find_device_update(m, target_id, symmetric_key)
                            if decoded is None:
                                continue
                            mid, resp = decoded
                            # Never log resp directly — it contains GPS coordinates for find-phone.
                            log.info("Response: fn=%s state=%s", resp.get("fn"), resp.get("state"))

                            resp_state = resp.get("state", "")
                            lat = resp.get("lat")
                            lng = resp.get("lng")
                            accuracy = resp.get("accuracy")

                            if resp_state == "ringing":
                                last_heartbeat = time.time()
                                comms_lost_shown = False
                                label = "Search in progress" if is_silent[0] else "Device is ringing!"
                                GLib.idle_add(set_ui, label, False, False, True)
                                if lat is not None:
                                    # Never log raw lat/lng — accuracy only.
                                    log.info("GPS fix received acc=%.1f", accuracy or 0)
                                    GLib.idle_add(update_location, lat, lng, accuracy)
                            elif resp_state == "stopped":
                                log.info("Device confirmed stopped")
                                GLib.idle_add(set_ui, "Alarm stopped", True, True, False)
                                if mid:
                                    api.fasttrack_ack(mid)
                                return  # clean exit
                            if mid:
                                api.fasttrack_ack(mid)
                    except Exception as e:
                        log.error("Poll failed: %s", e)

            threading.Thread(target=do_poll, daemon=True).start()

        def on_stop(btn):
            set_ui("Stopping...", False, False, False)
            poll_generation[0] += 1  # kill poll thread
            def do_stop():
                api, tid, key = shared_api[0], shared_target[0], shared_key[0]
                if api and tid and key:
                    _send_stop(api, tid, key)
                GLib.idle_add(set_ui, "Alarm stopped", True, True, False)
            threading.Thread(target=do_stop, daemon=True).start()

        start_btn.connect("clicked", on_start)
        stop_btn.connect("clicked", on_stop)

        def on_close(w):
            poll_generation[0] += 1  # kill poll thread
            api, tid, key = shared_api[0], shared_target[0], shared_key[0]
            if api and tid and key:
                threading.Thread(target=_send_stop, args=(api, tid, key), daemon=True).start()
            return False

        win.connect("close-request", on_close)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── CLI entry point ─────────────────────────────────────────────────

def _setup_subprocess_logging(config_dir: Path) -> None:
    """Set up file logging for subprocess windows (mirrors main.py setup_logging)."""
    from .config import Config
    config = Config(config_dir)
    if not config.allow_logging:
        return
    import logging
    from logging.handlers import RotatingFileHandler
    log_dir = config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handler = RotatingFileHandler(
        log_dir / "desktop-connector.log",
        maxBytes=1_000_000, backupCount=1,
    )
    handler.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def show_onboarding(config_dir: Path):
    """First-launch onboarding dialog (P.4a).

    Asks for the relay server URL (with /api/health probe button) and an
    autostart toggle. Persists answers via the same Config object the
    parent uses; the parent detects Save vs Cancel by re-reading
    config.server_url.

    Runs as a subprocess (spawned by appimage_onboarding) for the same
    reason all other GTK4 windows do — pystray's appindicator backend
    loads GTK3 in the parent process at dep-check time, locking GTK to
    3.0 there.
    """
    from .config import Config
    from .bootstrap.appimage_onboarding import (
        commit_onboarding_settings,
        probe_server,
    )

    config = Config(config_dir)
    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Welcome to Desktop Connector",
            default_width=480,
            default_height=420,
        )

        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        title = Gtk.Label(label="Welcome to Desktop Connector", xalign=0)
        title.add_css_class("title-2")
        outer.append(title)

        subtitle = Gtk.Label(
            label="Connect to your relay server to pair with your devices.",
            xalign=0, wrap=True,
        )
        subtitle.add_css_class("dim-label")
        outer.append(subtitle)

        url_label = Gtk.Label(label="Relay server URL", xalign=0)
        url_label.add_css_class("heading")
        outer.append(url_label)

        url_entry = Gtk.Entry(
            placeholder_text="https://example.com/SERVICES/desktop-connector",
        )
        outer.append(url_entry)

        probe_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(probe_row)
        probe_btn = Gtk.Button(label="Test connection")
        probe_row.append(probe_btn)
        probe_status = Gtk.Label(xalign=0, hexpand=True)
        probe_status.add_css_class("dim-label")
        probe_row.append(probe_status)

        def run_probe(url):
            if probe_server(url):
                probe_status.set_text("✓ Server reachable")
                probe_status.add_css_class("success")
                return True
            probe_status.remove_css_class("success")
            probe_status.set_text("✗ Could not reach server")
            return False

        def on_probe(_btn):
            url = url_entry.get_text().strip().rstrip("/")
            if not url:
                probe_status.set_text("Enter a URL first.")
                return
            probe_status.set_text("Checking…")
            GLib.idle_add(lambda: (run_probe(url), False)[1])

        probe_btn.connect("clicked", on_probe)

        autostart_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(autostart_row)
        autostart_label = Gtk.Label(
            label="Start automatically on login",
            xalign=0, hexpand=True,
        )
        autostart_row.append(autostart_label)
        autostart_switch = Gtk.Switch()
        autostart_switch.set_active(True)
        autostart_switch.set_valign(Gtk.Align.CENTER)
        autostart_row.append(autostart_switch)

        outer.append(Gtk.Box(vexpand=True))
        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.END)
        outer.append(button_row)

        cancel_btn = Gtk.Button(label="Cancel")
        button_row.append(cancel_btn)
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        button_row.append(save_btn)

        def commit(url):
            # Delegate to the free function in appimage_onboarding so
            # the persistence logic is unit-testable without GTK4.
            commit_onboarding_settings(
                config_dir,
                server_url=url,
                autostart_enabled=autostart_switch.get_active(),
            )

        cancel_btn.connect("clicked", lambda _b: win.close())

        def on_save(_btn):
            url = url_entry.get_text().strip().rstrip("/")
            if not url:
                probe_status.set_text("Enter a URL first.")
                return
            if probe_server(url):
                commit(url)
                win.close()
                return
            # Server unreachable — confirm "Save anyway?" (mirrors install.sh).
            dlg = Adw.AlertDialog(
                heading="Server did not respond",
                body=(
                    f"{url}/api/health did not return a healthy response. "
                    "Save anyway? You can update the URL in Settings later."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("save", "Save anyway")
            dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def on_resp(_d, response):
                if response == "save":
                    commit(url)
                    win.close()

            dlg.connect("response", on_resp)
            dlg.present(win)

        save_btn.connect("clicked", on_save)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_secret_storage_warning(config_dir: Path):
    """Explainer for H.5's plaintext-fallback warning.

    Opened when the user clicks the tray's "⚠ Secrets in plaintext"
    row. Three short sections — what's happening, why, how to fix —
    plus a Close button. No buttons that act on state; this window
    is informational. Fixing means installing a Secret Service
    backend (gnome-keyring on Zorin/Ubuntu/Mint, kwallet on KDE)
    and re-launching Desktop Connector.
    """
    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Secret storage warning",
            default_width=560,
            default_height=520,
        )

        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        title = Gtk.Label(label="Secrets are stored in plaintext", xalign=0)
        title.add_css_class("title-2")
        outer.append(title)

        subtitle = Gtk.Label(
            label=(
                "Desktop Connector couldn't reach a Secret Service "
                "backend (GNOME Keyring, KWallet, etc.). It's still "
                "working, but your long-term identity key, "
                "authentication token, and per-pairing encryption keys "
                "are sitting in plain text in your config directory."
            ),
            xalign=0, wrap=True, hexpand=True,
        )
        subtitle.add_css_class("dim-label")
        outer.append(subtitle)

        def _section(title_text: str, body_text: str) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            heading = Gtk.Label(label=title_text, xalign=0)
            heading.add_css_class("heading")
            box.append(heading)
            body = Gtk.Label(label=body_text, xalign=0, wrap=True, hexpand=True)
            body.set_selectable(True)
            box.append(body)
            return box

        outer.append(_section(
            "What's happening",
            f"Secrets are written to:\n  {config_dir / 'config.json'}\n"
            f"  {config_dir / 'keys' / 'private_key.pem'}\n"
            "with restrictive permissions (0o600), but anyone who can "
            "read your home directory (other accounts on this machine, "
            "anyone with a backup of ~/.config) sees the values in "
            "plain text. The private key is the most sensitive of the "
            "three — losing it leaks your long-term device identity "
            "and every pairing's encryption key.",
        ))

        outer.append(_section(
            "Why this is happening",
            "Either no Secret Service backend is installed / running, "
            "or it's locked and Desktop Connector couldn't unlock it. "
            "Desktop sessions normally start gnome-keyring (or kwallet "
            "on KDE) automatically, but headless / minimal installs "
            "may not.",
        ))

        outer.append(_section(
            "How to fix it",
            "On GNOME / Zorin / Ubuntu / Mint:\n"
            "  sudo apt install gnome-keyring libsecret-tools\n\n"
            "On KDE Plasma:\n"
            "  sudo apt install kwalletmanager kwallet-pam\n\n"
            "Then log out, log back in (so the keyring daemon "
            "registers with your session), and re-launch Desktop "
            "Connector. The next start will migrate your secrets out "
            "of config.json into the keyring automatically.",
        ))

        button_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
        )
        outer.append(button_row)

        close_btn = Gtk.Button(label="Close")
        close_btn.add_css_class("pill")
        close_btn.connect("clicked", lambda _: win.close())
        button_row.append(close_btn)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_locate_alert(config_dir: Path, *, sender_name: str):
    """Always-on-top modal shown when this desktop is being located (M.8).

    Spawned as a subprocess by ``GtkSubprocessAlert`` in the parent
    Poller process. The window has one job: display sender info + a
    Stop button. Clicking Stop (or closing the window) exits the
    process; the parent's watcher thread sees the exit and tears the
    rest of the locate session down.
    """
    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Being located",
            default_width=400,
            default_height=220,
        )
        win.set_modal(True)
        try:
            win.set_keep_above(True)
        except Exception:
            pass

        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        title = Gtk.Label(
            label="This device is being located",
            xalign=0,
        )
        title.add_css_class("title-2")
        outer.append(title)

        body = Gtk.Label(
            label=f"Locate request from {sender_name}.\n"
                  "Click Stop to silence this device.",
            xalign=0,
            wrap=True,
        )
        body.add_css_class("body")
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        outer.append(body)

        outer.append(Gtk.Box(vexpand=True))

        button_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        outer.append(button_row)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("pill")
        stop_btn.connect("clicked", lambda _b: win.close())
        button_row.append(stop_btn)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_vault_main(config_dir: Path):
    """Vault settings GTK window skeleton (T3.4).

    Top: Vault ID with copy button + (placeholder) QR icon.
    Body: tabbed pane with placeholders per the plan
    (Recovery / Folders / Devices / Activity / Maintenance / Security /
    Sync safety / Storage / Danger zone). Recovery tab implements the
    §gaps §2 emergency-access block.

    M1 manual-smoke surface; later phases populate the empty tabs.
    """
    from .config import Config

    config = Config(config_dir)
    app = _make_app()

    vault_id_undashed = ""
    recovery_status_text = "Untested"
    recovery_last_tested = "—"
    paired = config.paired_devices
    # Reading the vault id from local grant storage is T3.2's surface;
    # for the M1 walk-through we surface whatever's currently stashed
    # under config["vault"]["last_known_id"] (set by the wizard on
    # successful create) and fall back to a placeholder.
    vault_meta = config._data.get("vault") if isinstance(config._data.get("vault"), dict) else {}
    vault_id_undashed = (vault_meta or {}).get("last_known_id") or ""

    def vault_id_dashed() -> str:
        v = vault_id_undashed
        if len(v) == 12:
            return f"{v[0:4]}-{v[4:8]}-{v[8:12]}"
        return "(no vault opened)"

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Vault settings",
            default_width=720,
            default_height=540,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
        )
        toolbar.set_content(outer)

        # ---- header: Vault ID + copy button ----
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(header)

        id_label = Gtk.Label(label="Vault ID:", xalign=0)
        id_label.add_css_class("dim-label")
        header.append(id_label)

        id_value = Gtk.Label(label=vault_id_dashed(), xalign=0, hexpand=True)
        id_value.add_css_class("monospace")
        id_value.add_css_class("title-4")
        header.append(id_value)

        def on_copy(_btn):
            display = win.get_display()
            if display is not None:
                display.get_clipboard().set(vault_id_dashed())
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.add_css_class("pill")
        copy_btn.connect("clicked", on_copy)
        header.append(copy_btn)

        qr_btn = Gtk.Button(label="QR")
        qr_btn.add_css_class("pill")
        qr_btn.set_tooltip_text("Show Vault ID as a QR code (post-v1)")
        qr_btn.set_sensitive(False)
        header.append(qr_btn)

        # ---- tabbed pane ----
        view_stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        outer.append(switcher)
        outer.append(view_stack)

        def add_tab(name: str, title: str, body: Gtk.Widget) -> None:
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_child(body)
            view_stack.add_titled(scroller, name, title)

        # Recovery tab — §gaps §2 emergency-access block.
        recovery = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
        )
        recovery.append(Gtk.Label(label="Emergency recovery", xalign=0, css_classes=["title-3"]))
        block = Gtk.Grid(column_spacing=12, row_spacing=6)
        for row, (k, v) in enumerate([
            ("Method", "Recovery kit + passphrase"),
            ("Last tested", recovery_last_tested),
            ("Status", recovery_status_text),
        ]):
            k_lbl = Gtk.Label(label=k, xalign=0)
            k_lbl.add_css_class("dim-label")
            block.attach(k_lbl, 0, row, 1, 1)
            v_lbl = Gtk.Label(label=v, xalign=0)
            block.attach(v_lbl, 1, row, 1, 1)
        recovery.append(block)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        recovery.append(actions)
        actions.append(Gtk.Button(label="Test recovery now", css_classes=["pill"]))
        actions.append(Gtk.Button(label="Update recovery material", css_classes=["pill"]))
        # Banner if status is Untested.
        if recovery_status_text in ("Untested", "Failed", "Stale"):
            banner = Gtk.Label(
                label="Recovery has not been tested. Test it now to confirm you "
                      "can actually restore the vault.",
                xalign=0, wrap=True,
            )
            banner.add_css_class("warning")
            recovery.append(banner)
        add_tab("recovery", "Recovery", recovery)

        # Other tabs are empty placeholders for later phases.
        for name, title in [
            ("folders", "Folders"),
            ("devices", "Devices"),
            ("activity", "Activity"),
            ("maintenance", "Maintenance"),
            ("security", "Security"),
            ("sync_safety", "Sync safety"),
            ("storage", "Storage"),
            ("danger_zone", "Danger zone"),
        ]:
            placeholder = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=8,
                margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
            )
            placeholder.append(Gtk.Label(
                label=f"{title} — coming in a later phase.",
                xalign=0, css_classes=["dim-label"],
            ))
            add_tab(name, title, placeholder)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_vault_onboard(config_dir: Path):
    """Vault create / import wizard (T3.6).

    Two paths: 'Create new vault' (full M1 flow) and 'Import from
    export' (stubbed for T8). The create flow walks:
        1. relay picker (uses the existing ``server_url`` if set)
        2. recovery passphrase entry + confirm
        3. recovery-test prompt with Skip option
        4. success screen

    Per §A2: cancelling without an existing vault flips
    ``Config.vault_active`` to False so the user isn't permanently
    nagged.
    """
    from .config import Config
    from .vault_ui_state import wizard_cancel_rule

    config = Config(config_dir)

    # Wizard state — held in a dict so nested closures can mutate.
    # `recovery_secret_bytes` and `vault_access_secret` are stashed
    # post-create so the Export+Verify button can build the kit
    # content on demand (no silent auto-save anywhere). Both are
    # zeroed when the wizard closes. `recovery_envelope_meta` is
    # non-secret but needed to run the real recovery test.
    state = {
        "step": "choose_path",        # → create_passphrase → success
        "vault_existed_at_open": _local_vault_exists(config),
        "passphrase": "",
        "completed_successfully": False,
        "vault_id": None,
        "recovery_secret_bytes": None,
        "vault_access_secret": None,
        "recovery_envelope_meta": None,
        "exported_kit_path": None,
        "verify_passed": False,
        "delete_after_close": False,
    }

    app = _make_app()

    def _zero_state_secrets():
        """Best-effort overwrite of in-memory copies of the kit material
        before the wizard process exits. The Vault object's own
        master_key was already zero'd by ``vault.close()`` inside
        perform_create; this covers the duplicates we stashed for the
        Export flow.
        """
        rs = state.get("recovery_secret_bytes")
        if isinstance(rs, (bytes, bytearray)):
            buf = bytearray(rs)
            for i in range(len(buf)):
                buf[i] = 0
            state["recovery_secret_bytes"] = None
        state["vault_access_secret"] = None
        state["passphrase"] = ""

    def on_close(win):
        # If "Safely delete after close" is on AND a kit file was
        # exported during this wizard session, shred it now.
        if state.get("delete_after_close") and state.get("exported_kit_path"):
            from .vault import shred_file
            shred_file(state["exported_kit_path"])

        # If the wizard closes WITHOUT the user completing the
        # success flow (export + verify + confirmation), wipe any
        # local trace of vault creation so they can retry from
        # scratch on the next click. The relay-side vault row is
        # orphaned (no kit was saved → master key can't be
        # recovered → unusable anyway). Per
        # `feedback_respect_user_intent.md`: clean up partial state,
        # but never auto-flip the user's toggle.
        if not state["completed_successfully"] and state.get("vault_id"):
            try:
                cfg_dict = config._data.get("vault")
                if isinstance(cfg_dict, dict):
                    cfg_dict.pop("last_known_id", None)
                    config.save()
            except Exception:
                pass

        # The wizard's cancel rule (revised 2026-05-03) never changes
        # the toggle — the user's deliberate ON stays ON. Function
        # call kept for signature stability.
        wizard_cancel_rule(vault_exists=state["vault_existed_at_open"])

        _zero_state_secrets()
        return False

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Vault setup",
            default_width=720,
            default_height=520,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())
        win.connect("close-request", lambda w: on_close(w))

        body = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        toolbar.set_content(body)

        # Step 1 — choose path.
        choose = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        choose.append(Gtk.Label(label="Set up a vault", xalign=0, css_classes=["title-2"]))
        choose.append(Gtk.Label(
            label="A vault stores files and history end-to-end encrypted on the relay.",
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        create_btn = Gtk.Button(label="Create a new vault", css_classes=["pill", "suggested-action"])
        import_btn = Gtk.Button(label="Import from export… (coming in T8)", css_classes=["pill"])
        import_btn.set_sensitive(False)
        choose.append(create_btn)
        choose.append(import_btn)
        body.add_named(choose, "choose_path")

        # Step 2 — passphrase entry.
        pp = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        pp.append(Gtk.Label(label="Recovery passphrase", xalign=0, css_classes=["title-3"]))
        pp.append(Gtk.Label(
            label="This passphrase plus the recovery kit file is your only path "
                  "back to the vault if every device is lost. Choose carefully.",
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        # ``show_peek_icon=True`` adds the eye icon inside the entry —
        # GTK's built-in reveal-on-demand. Click toggles between
        # masked dots and plaintext characters; the unmasked state
        # persists only while the icon is held / toggled, so it doesn't
        # leak the passphrase to anyone who happens to glance later.
        pp_entry = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
        pp_confirm = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)

        # Generate button — opens the standalone passphrase generator
        # window. The user copies the result and pastes it into the
        # passphrase fields manually (matches the existing subprocess-
        # window pattern; no IPC needed).
        gen_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pp_entry.set_hexpand(True)
        gen_row.append(pp_entry)
        gen_btn = Gtk.Button(label="Generate", css_classes=["pill"])
        gen_btn.set_tooltip_text("Open a passphrase generator in a new window")
        def on_generate(_btn):
            import os as _os
            import subprocess as _subprocess
            import sys as _sys
            appimage = _os.environ.get("APPIMAGE")
            cmd = (
                [appimage, "--gtk-window=vault-passphrase-generator",
                 f"--config-dir={config.config_dir}"]
                if appimage else
                [_sys.executable, "-m", "src.windows", "vault-passphrase-generator",
                 f"--config-dir={config.config_dir}"]
            )
            cwd = (None if appimage
                   else str(Path(__file__).resolve().parent.parent))
            _subprocess.Popen(cmd, cwd=cwd)
        gen_btn.connect("clicked", on_generate)
        gen_row.append(gen_btn)
        pp.append(gen_row)

        pp.append(Gtk.Label(label="Confirm passphrase", xalign=0))
        pp.append(pp_confirm)
        pp_status = Gtk.Label(xalign=0, css_classes=["dim-label"])
        pp.append(pp_status)
        pp_next = Gtk.Button(label="Continue", css_classes=["pill", "suggested-action"])
        pp.append(pp_next)
        body.add_named(pp, "create_passphrase")

        # (Step 3 — vestigial "recovery test prompt" with Test/Skip
        # buttons that did the same thing has been removed. The real
        # recovery test now happens on the success screen, bundled
        # with kit export, mandatory before Done. See
        # `feedback_no_fake_tests.md` and `T0 §gaps §1` revision.)

        # Step 4 — success: vault is created, user MUST back up the
        # recovery kit + passphrase before leaving. Layout follows the
        # "explicit user-controlled flow + severity messaging +
        # confirmation gate" pattern (see memory feedback_security_ux.md).
        ok = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        ok.append(Gtk.Label(label="Vault created", xalign=0, css_classes=["title-2"]))

        # ---- Severity warning (always visible) ----
        warn = Gtk.Label(
            xalign=0, wrap=True,
            label=(
                "⚠ Your data is unrecoverable without BOTH the recovery kit "
                "file AND your passphrase. There is no password reset. Lose "
                "either one and the vault is gone forever."
            ),
        )
        warn.add_css_class("warning")
        ok.append(warn)

        # ---- Copyable Vault ID ----
        ok.append(Gtk.Label(label="Your Vault ID:", xalign=0, css_classes=["dim-label"]))
        ok_id_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ok.append(ok_id_row)
        ok_id_entry = Gtk.Entry()
        ok_id_entry.set_editable(False)
        ok_id_entry.set_can_focus(True)
        ok_id_entry.add_css_class("monospace")
        ok_id_entry.set_hexpand(True)
        ok_id_row.append(ok_id_entry)
        ok_id_copy = Gtk.Button(label="Copy", css_classes=["pill"])
        def on_copy_vault_id(_btn):
            display = win.get_display()
            if display is not None:
                display.get_clipboard().set(ok_id_entry.get_text())
        ok_id_copy.connect("clicked", on_copy_vault_id)
        ok_id_row.append(ok_id_copy)

        # ---- Export + verify recovery kit ----
        # Bundled as one user action: there's no point exporting a kit
        # without confirming the kit + passphrase actually produce the
        # master key. The verify is real — re-runs derive_recovery_wrap_key
        # against the saved kit and AEAD-decrypts the recovery envelope.
        ok.append(Gtk.Label(label="Recovery kit file:", xalign=0, css_classes=["dim-label"]))
        export_btn = Gtk.Button(
            label="Export and verify recovery kit…",
            css_classes=["pill", "suggested-action"],
        )
        ok.append(export_btn)

        # Status line — shows the exported path + verify result.
        export_status = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["monospace", "dim-label"])
        ok.append(export_status)
        verify_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        ok.append(verify_status)

        # "Safely delete" toggle — only meaningful after an export.
        # Shown but disabled until the user has actually picked a path.
        delete_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ok.append(delete_row)
        delete_switch = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=False)
        delete_row.append(delete_switch)
        delete_label = Gtk.Label(
            xalign=0, wrap=True, hexpand=True,
            label=(
                "Securely delete the exported file when I close this wizard "
                "(use this if you're moving it into a password manager and "
                "don't want a copy left in Downloads)"
            ),
        )
        delete_label.add_css_class("dim-label")
        delete_row.append(delete_label)

        # ---- Confirmation gate ----
        confirm_check = Gtk.CheckButton(
            label=(
                "I have backed up the recovery kit file AND remember my passphrase. "
                "I understand my vault data is unrecoverable without both."
            ),
        )
        confirm_check.set_sensitive(False)   # disabled until export+verify pass
        ok.append(confirm_check)

        ok_close = Gtk.Button(label="Done", css_classes=["pill", "suggested-action"])
        ok_close.set_sensitive(False)
        ok.append(ok_close)

        # ---- Wire up the success-screen logic ----
        def _refresh_done_button():
            """Done is enabled only when the user has confirmed AND the
            export+verify step has succeeded.
            """
            ok_close.set_sensitive(
                state["verify_passed"] and confirm_check.get_active()
            )

        confirm_check.connect("toggled", lambda _w: _refresh_done_button())

        def on_delete_toggled(switch, _pspec):
            state["delete_after_close"] = switch.get_active()
        delete_switch.connect("notify::active", on_delete_toggled)

        def on_export_clicked(_btn):
            """Open a save dialog, write the kit, then **verify** that
            the kit + passphrase actually produce the master key. This
            is the real recovery test — replaces the prior decorative
            'Test now / Skip' branches that did nothing.
            """
            from .vault import (
                vault_id_dashed,
                verify_recovery_kit,
                write_recovery_kit_file,
            )

            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Save recovery kit")
            file_dialog.set_initial_name(
                f"{vault_id_dashed(state['vault_id'])}.dc-vault-recovery"
            )

            def on_file_chosen(dialog, result):
                try:
                    gio_file = dialog.save_finish(result)
                except GLib.Error:
                    # User cancelled the file dialog — no error state.
                    return
                if gio_file is None:
                    return
                target_path = gio_file.get_path()

                # 1) Write the kit.
                try:
                    write_recovery_kit_file(
                        target_path,
                        vault_id=state["vault_id"],
                        recovery_secret=state["recovery_secret_bytes"],
                        vault_access_secret=state["vault_access_secret"],
                    )
                except Exception as exc:
                    export_status.remove_css_class("dim-label")
                    export_status.add_css_class("error")
                    export_status.set_label(f"Export failed: {exc}")
                    verify_status.set_label("")
                    state["verify_passed"] = False
                    confirm_check.set_sensitive(False)
                    _refresh_done_button()
                    return

                state["exported_kit_path"] = target_path
                export_status.remove_css_class("error")
                export_status.add_css_class("dim-label")
                export_status.set_label(f"Saved to: {target_path}")
                delete_switch.set_sensitive(True)

                # 2) Real recovery verify — re-derive wrap_key from
                #    the saved kit + the typed passphrase, AEAD-decrypt
                #    the recovery envelope. Poly1305 verifies the
                #    kit/passphrase combo end-to-end.
                ok_, msg = verify_recovery_kit(
                    target_path,
                    passphrase=state["passphrase"],
                    envelope_meta=state["recovery_envelope_meta"],
                )
                if ok_:
                    verify_status.remove_css_class("error")
                    verify_status.remove_css_class("dim-label")
                    verify_status.add_css_class("success")
                    verify_status.set_label(
                        f"✓ Recovery verified — {msg}."
                    )
                    state["verify_passed"] = True
                    confirm_check.set_sensitive(True)
                else:
                    verify_status.remove_css_class("success")
                    verify_status.remove_css_class("dim-label")
                    verify_status.add_css_class("error")
                    verify_status.set_label(
                        f"✗ {msg}. Check that you typed the passphrase correctly, "
                        "then click Export and verify recovery kit again."
                    )
                    state["verify_passed"] = False
                    confirm_check.set_active(False)
                    confirm_check.set_sensitive(False)
                _refresh_done_button()

            file_dialog.save(parent=win, callback=on_file_chosen)

        export_btn.connect("clicked", on_export_clicked)

        def on_done(_btn):
            # The shred-on-close logic + secret zeroing happens in
            # on_close(); we just need to dismiss the window.
            win.close()
        ok_close.connect("clicked", on_done)

        body.add_named(ok, "success")

        body.set_visible_child_name("choose_path")

        # Step transitions.
        def on_create_path(_btn):
            body.set_visible_child_name("create_passphrase")
        create_btn.connect("clicked", on_create_path)

        def on_pp_next(_btn):
            entered = pp_entry.get_text()
            confirm = pp_confirm.get_text()
            if len(entered) < 8:
                pp_status.set_text("Passphrase must be at least 8 characters.")
                return
            if entered != confirm:
                pp_status.set_text("Passphrases don't match.")
                return
            state["passphrase"] = entered
            # Skip the prior decorative recovery_test step — go straight
            # to vault creation and the success screen, where the real
            # bundled Export-and-Verify happens.
            perform_create()
        pp_next.connect("clicked", on_pp_next)

        def perform_create():
            """Actually create the vault on the relay, then stash the
            recovery_secret + vault_access_secret in wizard state so
            the user-controlled Export flow on the success screen can
            write the kit file at a path they pick.

            The kit file is **not** auto-saved anywhere on disk —
            silent auto-save would hide the act of "you have a thing
            you must back up", and per design feedback users rarely
            go look for files they didn't choose to save.
            """
            from .vault import Vault

            try:
                fake_relay = _BarebonesRelay(config)
                vault = Vault.create_new(
                    fake_relay,
                    recovery_passphrase=state["passphrase"],
                    argon_memory_kib=8192,    # reduced cost for the dev relay walk-through
                    argon_iterations=2,
                )

                # Stash the kit material into wizard state. Both buffers
                # die when on_close calls _zero_state_secrets — but they
                # MUST live until the user chooses to Export, otherwise
                # the kit is unrecoverable.
                # `recovery_envelope_meta` is non-secret (envelope_id,
                # salts, nonces, ciphertext) but needed to run the
                # mandatory verify step.
                state["vault_id"] = vault.vault_id
                state["recovery_secret_bytes"] = vault.recovery_secret
                state["vault_access_secret"] = vault.vault_access_secret
                state["recovery_envelope_meta"] = vault.recovery_envelope_meta

                # Persist the vault id so the main settings + tray
                # know there's a vault to switch to.
                if "vault" not in config._data or not isinstance(config._data.get("vault"), dict):
                    config._data["vault"] = {}
                config._data["vault"]["last_known_id"] = vault.vault_id
                config.save()
                state["completed_successfully"] = True

                ok_id_entry.set_text(vault.vault_id_dashed)
                vault.close()
                body.set_visible_child_name("success")
            except Exception as exc:
                # Surface the failure on the success screen — the layout
                # is the same; we just leave the export status in error
                # state and disable Done.
                ok_id_entry.set_text("")
                export_status.remove_css_class("dim-label")
                export_status.add_css_class("error")
                export_status.set_label(f"Vault creation failed: {exc}")
                export_btn.set_sensitive(False)
                body.set_visible_child_name("success")

        def on_done(_btn):
            win.close()
        ok_close.connect("clicked", on_done)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_vault_passphrase_generator(config_dir: Path):
    """Standalone passphrase-generator window opened from the wizard's
    Generate button. Shows a random diceware-style passphrase, lets
    the user Regenerate or Copy. The user pastes the result back into
    the wizard's passphrase fields manually.
    """
    from .vault_passphrase import generate_passphrase, estimated_entropy_bits

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Generate passphrase",
            default_width=720,
            default_height=320,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        outer.append(Gtk.Label(label="Random passphrase", xalign=0, css_classes=["title-3"]))
        outer.append(Gtk.Label(
            label=(
                f"7 random words from a 520-word list ≈ {estimated_entropy_bits():.0f} "
                "bits of entropy. Copy this into the wizard's passphrase fields, "
                "or click Regenerate if you don't like it."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        # Read-only Entry so it's selectable + Ctrl-C friendly.
        pp_entry = Gtk.Entry()
        pp_entry.set_editable(False)
        pp_entry.set_text(generate_passphrase())
        pp_entry.add_css_class("monospace")
        pp_entry.set_hexpand(True)
        outer.append(pp_entry)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(btn_row)

        regen_btn = Gtk.Button(label="Regenerate", css_classes=["pill"])
        def on_regen(_b):
            pp_entry.set_text(generate_passphrase())
        regen_btn.connect("clicked", on_regen)
        btn_row.append(regen_btn)

        copy_btn = Gtk.Button(label="Copy", css_classes=["pill", "suggested-action"])
        def on_copy(_b):
            display = win.get_display()
            if display is not None:
                display.get_clipboard().set(pp_entry.get_text())
        copy_btn.connect("clicked", on_copy)
        btn_row.append(copy_btn)

        spacer = Gtk.Box(hexpand=True)
        btn_row.append(spacer)

        close_btn = Gtk.Button(label="Close", css_classes=["pill"])
        close_btn.connect("clicked", lambda _b: win.close())
        btn_row.append(close_btn)

        outer.append(Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
            label=(
                "Tip: write the passphrase down somewhere safe BEFORE you paste "
                "it. If you lose it, the recovery kit file alone won't get you "
                "back into the vault."
            ),
        ))

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def _local_vault_exists(config) -> bool:
    """True iff this device thinks a vault already exists locally.

    For T3 the heuristic is "has the wizard ever set
    config['vault']['last_known_id']?". A keyring-backed grant store
    (T3.2) provides a more authoritative answer once integration
    lands; this is good enough for the wizard's cancel-rule input.
    """
    raw = config._data.get("vault")
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("last_known_id"))


class _BarebonesRelay:
    """Adapter wrapping the existing ApiClient surface to the
    RelayProtocol used by Vault.create_new. Only used by the wizard
    walk-through — production callers will inject a richer wrapper.
    """

    def __init__(self, config) -> None:
        self._config = config

    def create_vault(self, vault_id, vault_access_token_hash, encrypted_header,
                     header_hash, initial_manifest_ciphertext, initial_manifest_hash):
        # Real HTTP plumbing comes when the desktop wires this into
        # api_client. For the M1 demo we just record the call locally
        # so the wizard succeeds end-to-end without a live relay.
        from . import vault_crypto  # noqa: F401  (sanity import to confirm crypto path is healthy)
        return {"vault_id": vault_id}

    def get_header(self, vault_id, vault_access_secret):
        raise NotImplementedError("not used during create_new")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "window",
        choices=[
            "send-files", "settings", "history", "pairing",
            "find-phone", "locate-alert", "onboarding",
            "secret-storage-warning",
            "vault-main", "vault-onboard", "vault-passphrase-generator",
        ],
    )
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--sender-name", default="")
    args = parser.parse_args()

    config_dir = Path(args.config_dir)
    _setup_subprocess_logging(config_dir)

    if args.window == "send-files":
        show_send_files(config_dir)
    elif args.window == "settings":
        show_settings(config_dir)
    elif args.window == "history":
        show_history(config_dir)
    elif args.window == "pairing":
        show_pairing(config_dir)
    elif args.window == "find-phone":
        show_find_phone(config_dir)
    elif args.window == "locate-alert":
        show_locate_alert(config_dir, sender_name=args.sender_name or "another device")
    elif args.window == "onboarding":
        show_onboarding(config_dir)
    elif args.window == "secret-storage-warning":
        show_secret_storage_warning(config_dir)
    elif args.window == "vault-main":
        show_vault_main(config_dir)
    elif args.window == "vault-onboard":
        show_vault_onboard(config_dir)
    elif args.window == "vault-passphrase-generator":
        show_vault_passphrase_generator(config_dir)


if __name__ == "__main__":
    main()
