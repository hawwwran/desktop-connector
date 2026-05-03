"""Send Files window (`python -m src.windows send-files`)."""

import base64
import json
import threading
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .notifications import notify
from .windows_common import (
    _create_device_picker,
    _make_app,
    _notify_folders_skipped,
    format_size,
)


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
                        subtitle=f"{f.parent}  ·  {format_size(f.stat().st_size)}",
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
                row.set_subtitle(f"Uploading...  ·  {format_size(filepath.stat().st_size)}")

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
