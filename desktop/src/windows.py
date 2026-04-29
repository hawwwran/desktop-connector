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
                                            chunks_downloaded=0, chunks_total=total_chunks)
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
    stats = None
    try:
        api = ApiClient(conn, crypto)
        paired_dev = config.get_first_paired_device()
        stats = api.get_stats(paired_with=paired_dev[0] if paired_dev else None)
    except Exception:
        pass

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
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
                        # Remove local pairing — go through the Config
                        # method so the secret store entry (libsecret
                        # keyring or JSON fallback field) is cleaned up
                        # alongside the JSON metadata. Direct dict
                        # manipulation here would either leak keyring
                        # entries or re-introduce hydrated plaintext.
                        config.remove_paired_device(device_id)
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
            # Match by device ID (may have multiple pairings)
            target_id = paired[0]
            pd = next((d for d in paired_devs if d.get("device_id") == target_id), paired_devs[0] if paired_devs else None)
            if pd:
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
                stats_group.add(Adw.ActionRow(
                    title="Paired device status",
                    subtitle=status_str,
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
        clear_all_btn.set_tooltip_text("Clear all history")
        clear_all_btn.add_css_class("brand-action-destructive")
        def on_clear_all(b):
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="Clear history?",
                body="This will remove all transfer history entries.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("clear", "Clear All")
            dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
            def on_response(dlg, response):
                if response == "clear":
                    history.clear()
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

        list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        clamp.set_child(list_container)

        has_active = [False]
        # row_widgets[transfer_id] = (box_widget, row, progress_bar_or_None)
        row_widgets = {}
        all_widgets = []  # ordered list of group children
        structural_sig = [None]  # (transfer_id, ...) — triggers structural diff
        empty_label = [None]  # holds the "No transfers yet" label when shown
        progress_sig = [None]    # mutable fields — triggers in-place update

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

            row = Adw.ActionRow(
                title=f"{direction_prefix}  {label}",
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
                tid_key = it.get("transfer_id", id(it))
                row_widgets.pop(tid_key, None)
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

        def build_list():
            _scrub_zombie_waiting()
            history._items = history._load()
            items = history.items

            # Structural sig: item identity and base state
            s_sig = tuple(
                (i.get("transfer_id", i.get("timestamp")), i.get("direction"))
                for i in items
            )
            # Progress sig: all mutable fields
            p_sig = tuple(
                (i.get("transfer_id"), i.get("status"), i.get("delivered"),
                 i.get("chunks_downloaded", 0), i.get("recipient_chunks_downloaded", 0))
                for i in items
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
                new_tids_ordered = [
                    i.get("transfer_id", i.get("timestamp")) for i in items
                ]
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
                if not items and empty_label[0] is None:
                    empty = Gtk.Label(label="No transfers yet")
                    empty.add_css_class("dim-label")
                    empty.set_margin_top(48)
                    empty.set_margin_bottom(48)
                    list_container.append(empty)
                    all_widgets.append(empty)
                    empty_label[0] = empty

            # In-place update on every tick — refresh subtitles and
            # progress bars for rows that existed before AND for rows we
            # just added (cheap, idempotent, makes the code branch-free).
            for item in items:
                tid = item.get("transfer_id", item.get("timestamp"))
                entry = row_widgets.get(tid)
                if entry:
                    box, row, old_pbar = entry
                    new_pbar = _update_row(item, row, old_pbar, box)
                    row_widgets[tid] = (box, row, new_pbar)

            return True

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
    from .pairing import generate_qr_data, generate_qr_image

    import io

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")
    api = ApiClient(conn, crypto)

    qr_data = generate_qr_data(config, crypto)
    qr_pil = generate_qr_image(qr_data)
    server_url = json.loads(qr_data)["server"]
    device_id = crypto.get_device_id()

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
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

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ─── Find My Phone Window ───────────────────────────────────────────

def show_find_phone(config_dir: Path):
    import logging
    log = logging.getLogger("desktop-connector.find-phone")

    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .messaging import FasttrackAdapter, MessageType

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)

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
        win = Adw.ApplicationWindow(application=app, title="Find my Phone",
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

        # Settings group
        settings_group = Adw.PreferencesGroup(title="Settings")
        content.append(settings_group)

        # Silent search toggle
        silent_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        silent_switch.set_active(False)
        silent_row = Adw.ActionRow(title="Silent search", subtitle="Track location without alarm (stolen phone)")
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
        btn_box.append(start_btn)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("pill")
        stop_btn.set_visible(False)
        btn_box.append(stop_btn)

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
            start_btn.set_visible(show_start)
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
            paired = config.get_first_paired_device()
            if not paired:
                toast_overlay.add_toast(Adw.Toast(title="Not paired with any device", timeout=3))
                return

            target_id, target_info = paired
            symmetric_key = base64.b64decode(target_info["symmetric_key_b64"])
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

                # Flush stale messages from previous sessions
                stale = api.fasttrack_pending()
                for m in stale:
                    mid = m.get("id")
                    if mid:
                        api.fasttrack_ack(mid)
                if stale:
                    log.info("fasttrack.message.flushed_stale count=%d", len(stale))

                log.info("fasttrack.command.sent fn=find-phone action=start volume=%d silent=%s recipient=%s",
                         volume, is_silent[0], target_id[:12])
                msg_id = api.fasttrack_send(target_id, encrypted_b64)
                if msg_id is None:
                    log.error("fasttrack.command.send_failed fn=find-phone")
                    GLib.idle_add(set_ui, "Failed to reach phone", True, True, False)
                    return

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
                            mid = m.get("id")
                            enc_data = m.get("encrypted_data", "")
                            try:
                                enc_bytes = base64.b64decode(enc_data)
                                plain = crypto.decrypt_blob(enc_bytes, symmetric_key)
                                resp = json.loads(plain)
                                # Never log resp directly — it contains GPS coordinates for find-phone.
                                log.info("Response: fn=%s state=%s", resp.get("fn"), resp.get("state"))

                                msg = FasttrackAdapter.to_device_message(resp)
                                if msg and msg.type == MessageType.FIND_PHONE_LOCATION_UPDATE:
                                    resp_state = resp.get("state", "")
                                    lat = resp.get("lat")
                                    lng = resp.get("lng")
                                    accuracy = resp.get("accuracy")

                                    if resp_state == "ringing":
                                        last_heartbeat = time.time()
                                        comms_lost_shown = False
                                        label = "Search in progress" if is_silent[0] else "Phone is ringing!"
                                        GLib.idle_add(set_ui, label, False, False, True)
                                        if lat is not None:
                                            # Never log raw lat/lng — accuracy only.
                                            log.info("GPS fix received acc=%.1f", accuracy or 0)
                                            GLib.idle_add(update_location, lat, lng, accuracy)
                                    elif resp_state == "stopped":
                                        log.info("Phone confirmed stopped")
                                        GLib.idle_add(set_ui, "Alarm stopped", True, True, False)
                                        if mid:
                                            api.fasttrack_ack(mid)
                                        return  # clean exit
                            except Exception as e:
                                log.error("Decrypt failed: %s", e)
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
            label="Connect to your relay server to pair with your phone.",
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "window",
        choices=[
            "send-files", "settings", "history", "pairing",
            "find-phone", "onboarding", "secret-storage-warning",
        ],
    )
    parser.add_argument("--config-dir", required=True)
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
    elif args.window == "onboarding":
        show_onboarding(config_dir)
    elif args.window == "secret-storage-warning":
        show_secret_storage_warning(config_dir)


if __name__ == "__main__":
    main()
