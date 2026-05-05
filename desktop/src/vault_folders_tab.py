"""GTK builder for the Vault settings Folders tab."""

from __future__ import annotations

import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

from .vault_binding_baseline import run_initial_baseline
from .vault_binding_lifecycle import BindingCancellationRegistry
from .vault_binding_sync import (
    flush_and_sync_binding,
    format_sync_outcome_toast,
)
from .vault_bindings import VaultBindingsStore
from .vault_cache import VaultLocalIndex
from .vault_connect_folder_dialog import present_connect_folder_dialog
from .vault_error_messages import humanize
from .vault_folder_actions import (
    dispatch_disconnect,
    dispatch_pause,
    dispatch_resume,
)
from .vault_folder_ui_state import (
    BINDING_COLUMNS,
    FOLDER_COLUMNS,
    binding_rows_for_render,
    default_ignore_patterns_text,
    folder_rows_from_cache,
    parse_ignore_patterns_text,
)
from .vault_runtime import create_vault_relay, open_local_vault_from_grant
from .vault_usage import calculate_vault_usage


def build_vault_folders_tab(
    *,
    app: Adw.Application,
    parent_window: Adw.ApplicationWindow,
    config_dir: Path,
    config,
    vault_id: str,
) -> Gtk.Widget:
    """Build the Vault settings Folders tab."""
    local_index = VaultLocalIndex(config_dir)
    usage_by_folder_state = {"value": {}}
    folders = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=12,
        margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
    )
    folders.append(Gtk.Label(label="Remote folders", xalign=0, css_classes=["title-3"]))

    folder_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    folders.append(folder_actions)
    add_folder_btn = Gtk.Button(label="Add", css_classes=["pill", "suggested-action"])
    add_folder_btn.set_sensitive(bool(vault_id))
    rename_folder_btn = Gtk.Button(label="Rename", css_classes=["pill"])
    rename_folder_btn.set_sensitive(bool(vault_id))
    connect_local_btn = Gtk.Button(label="Connect local folder…", css_classes=["pill"])
    connect_local_btn.set_sensitive(False)
    connect_local_btn.set_tooltip_text(
        "Bind this remote folder to a local path. Default sync mode is "
        "Backup only (uploads local changes; remote changes never come down)."
    )
    delete_folder_btn = Gtk.Button(label="Delete", css_classes=["pill", "destructive-action"])
    delete_folder_btn.set_sensitive(False)
    delete_folder_btn.set_tooltip_text("Folder delete is implemented in T7/T14")
    folder_actions.append(add_folder_btn)
    folder_actions.append(rename_folder_btn)
    folder_actions.append(connect_local_btn)
    folder_actions.append(delete_folder_btn)

    folders_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    folders.append(folders_status)

    folders_grid = Gtk.Grid(column_spacing=16, row_spacing=8, hexpand=True)
    folders.append(folders_grid)

    folders.append(Gtk.Label(
        label="Local bindings", xalign=0, css_classes=["title-3"],
        margin_top=12,
    ))
    bindings_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    folders.append(bindings_status)
    bindings_grid = Gtk.Grid(column_spacing=16, row_spacing=8, hexpand=True)
    folders.append(bindings_grid)

    def clear_folders_grid() -> None:
        child = folders_grid.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            folders_grid.remove(child)
            child = next_child

    def attach_folder_cell(text: str, col: int, row: int, *, header: bool = False) -> None:
        label = Gtk.Label(label=text, xalign=0, hexpand=(col == 0))
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if header:
            label.add_css_class("dim-label")
        folders_grid.attach(label, col, row, 1, 1)

    def refresh_folders_table(message: str | None = None) -> None:
        clear_folders_grid()
        for col, title in enumerate(FOLDER_COLUMNS):
            attach_folder_cell(title, col, 0, header=True)

        rows = []
        if vault_id:
            try:
                rows = folder_rows_from_cache(
                    local_index.list_remote_folders(vault_id),
                    usage_by_folder=usage_by_folder_state["value"],
                )
            except Exception as exc:
                folders_status.set_label(f"Could not load the local folder cache: {exc}")
        for index, row in enumerate(rows, start=1):
            attach_folder_cell(row["name"], 0, index)
            attach_folder_cell(row["binding"], 1, index)
            attach_folder_cell(row["current"], 2, index)
            attach_folder_cell(row["stored"], 3, index)
            attach_folder_cell(row["history"], 4, index)
            attach_folder_cell(row["status"], 5, index)

        if not rows:
            empty = "No remote folders yet." if vault_id else "Open a vault before adding folders."
            attach_folder_cell(empty, 0, 1)

        if message is not None:
            folders_status.set_label(message)
        elif vault_id:
            folders_status.set_label(f"{len(rows)} remote folder(s).")
        else:
            folders_status.set_label("No local vault is connected.")

    def refresh_folders_usage_async(message: str | None = None) -> None:
        if not vault_id:
            return
        folders_status.set_label("Refreshing folder usage...")

        def worker() -> None:
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(config_dir, config, vault_id)
                try:
                    manifest = vault.fetch_manifest(relay, local_index=local_index)
                finally:
                    vault.close()
                usage = calculate_vault_usage(manifest).by_folder
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    refresh_folders_table(f"Folder usage unavailable: {error_message}")
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                usage_by_folder_state["value"] = usage
                refresh_folders_table(message)
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    def open_add_folder_dialog(_btn) -> None:
        dialog = Adw.ApplicationWindow(
            application=app,
            title="Add folder",
            default_width=540,
            default_height=420,
        )
        dialog.set_transient_for(parent_window)
        dialog.set_modal(True)
        dialog_toolbar = Adw.ToolbarView()
        dialog.set_content(dialog_toolbar)
        dialog_toolbar.add_top_bar(Adw.HeaderBar())

        body_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
        )
        dialog_toolbar.set_content(body_box)
        body_box.append(Gtk.Label(label="Add remote folder", xalign=0, css_classes=["title-2"]))

        body_box.append(Gtk.Label(label="Name", xalign=0, css_classes=["dim-label"]))
        name_entry = Gtk.Entry(hexpand=True)
        name_entry.set_placeholder_text("Folder name")
        body_box.append(name_entry)

        body_box.append(Gtk.Label(label="Ignore patterns", xalign=0, css_classes=["dim-label"]))
        ignore_buffer = Gtk.TextBuffer()
        ignore_buffer.set_text(default_ignore_patterns_text())
        ignore_view = Gtk.TextView(buffer=ignore_buffer, monospace=True)
        ignore_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        ignore_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        ignore_scroller.set_min_content_height(140)
        ignore_scroller.set_child(ignore_view)
        body_box.append(ignore_scroller)

        dialog_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        body_box.append(dialog_status)

        dialog_buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
        )
        body_box.append(dialog_buttons)
        cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
        confirm_btn = Gtk.Button(label="Add", css_classes=["pill", "suggested-action"])
        dialog_buttons.append(cancel_btn)
        dialog_buttons.append(confirm_btn)

        cancel_btn.connect("clicked", lambda _button: dialog.close())

        def set_dialog_status(message: str, css_class: str = "dim-label") -> None:
            for klass in ("dim-label", "error", "success"):
                dialog_status.remove_css_class(klass)
            dialog_status.add_css_class(css_class)
            dialog_status.set_label(message)

        def read_ignore_patterns() -> list[str]:
            start = ignore_buffer.get_start_iter()
            end = ignore_buffer.get_end_iter()
            return parse_ignore_patterns_text(ignore_buffer.get_text(start, end, False))

        def on_confirm(_button) -> None:
            folder_name = name_entry.get_text().strip()
            if not folder_name:
                set_dialog_status("Enter a folder name.", "error")
                return
            if not vault_id:
                set_dialog_status("No local vault is connected.", "error")
                return

            patterns = read_ignore_patterns()
            confirm_btn.set_sensitive(False)
            cancel_btn.set_sensitive(False)
            set_dialog_status("Adding folder...", "dim-label")

            def worker() -> None:
                try:
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        author_device_id = config.device_id or ("0" * 32)
                        manifest = vault.add_remote_folder(
                            relay,
                            display_name=folder_name,
                            ignore_patterns=patterns,
                            author_device_id=author_device_id,
                            local_index=local_index,
                        )
                        usage = calculate_vault_usage(manifest).by_folder
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = humanize(exc)

                    def fail() -> bool:
                        confirm_btn.set_sensitive(True)
                        cancel_btn.set_sensitive(True)
                        set_dialog_status(f"Could not add folder: {error_message}", "error")
                        return False

                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    usage_by_folder_state["value"] = usage
                    dialog.close()
                    refresh_folders_table(f"Added {folder_name}.")
                    return False

                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        confirm_btn.connect("clicked", on_confirm)
        dialog.present()

    def open_rename_folder_dialog(_btn) -> None:
        if not vault_id:
            return
        try:
            cached = local_index.list_remote_folders(vault_id)
        except Exception as exc:
            folders_status.set_label(f"Could not load the local folder cache: {exc}")
            return
        if not cached:
            folders_status.set_label("No remote folders to rename.")
            return

        choices: list[tuple[str, str]] = [
            (str(f.get("display_name_enc", "")), str(f.get("remote_folder_id", "")))
            for f in cached
            if f.get("remote_folder_id")
        ]

        dialog = Adw.ApplicationWindow(
            application=app,
            title="Rename folder",
            default_width=480,
            default_height=260,
        )
        dialog.set_transient_for(parent_window)
        dialog.set_modal(True)
        dialog_toolbar = Adw.ToolbarView()
        dialog.set_content(dialog_toolbar)
        dialog_toolbar.add_top_bar(Adw.HeaderBar())

        body_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
        )
        dialog_toolbar.set_content(body_box)
        body_box.append(Gtk.Label(label="Rename remote folder", xalign=0, css_classes=["title-2"]))

        body_box.append(Gtk.Label(label="Folder", xalign=0, css_classes=["dim-label"]))
        folder_dropdown = Gtk.DropDown.new_from_strings(
            [name for name, _ in choices],
        )
        folder_dropdown.set_hexpand(True)
        body_box.append(folder_dropdown)

        body_box.append(Gtk.Label(label="New name", xalign=0, css_classes=["dim-label"]))
        name_entry = Gtk.Entry(hexpand=True)
        name_entry.set_text(choices[0][0])
        body_box.append(name_entry)

        dialog_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        body_box.append(dialog_status)

        dialog_buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
        )
        body_box.append(dialog_buttons)
        cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
        confirm_btn = Gtk.Button(label="Save", css_classes=["pill", "suggested-action"])
        dialog_buttons.append(cancel_btn)
        dialog_buttons.append(confirm_btn)

        cancel_btn.connect("clicked", lambda _button: dialog.close())

        def set_dialog_status(message: str, css_class: str = "dim-label") -> None:
            for klass in ("dim-label", "error", "success"):
                dialog_status.remove_css_class(klass)
            dialog_status.add_css_class(css_class)
            dialog_status.set_label(message)

        def selected_folder() -> tuple[str, str]:
            i = folder_dropdown.get_selected()
            if 0 <= i < len(choices):
                return choices[i]
            return choices[0]

        def on_dropdown_changed(_combo, _pspec) -> None:
            current_name, _rfid = selected_folder()
            name_entry.set_text(current_name)

        folder_dropdown.connect("notify::selected", on_dropdown_changed)

        def on_confirm(_button) -> None:
            current_name, rfid = selected_folder()
            new_name = name_entry.get_text().strip()
            if not new_name:
                set_dialog_status("Enter a folder name.", "error")
                return
            if new_name == current_name:
                set_dialog_status("Name is unchanged.", "error")
                return

            confirm_btn.set_sensitive(False)
            cancel_btn.set_sensitive(False)
            folder_dropdown.set_sensitive(False)
            name_entry.set_sensitive(False)
            set_dialog_status("Renaming folder...", "dim-label")

            def worker() -> None:
                try:
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        author_device_id = config.device_id or ("0" * 32)
                        manifest = vault.rename_remote_folder(
                            relay,
                            remote_folder_id=rfid,
                            new_display_name=new_name,
                            author_device_id=author_device_id,
                            local_index=local_index,
                        )
                        usage = calculate_vault_usage(manifest).by_folder
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = humanize(exc)

                    def fail() -> bool:
                        confirm_btn.set_sensitive(True)
                        cancel_btn.set_sensitive(True)
                        folder_dropdown.set_sensitive(True)
                        name_entry.set_sensitive(True)
                        set_dialog_status(f"Could not rename folder: {error_message}", "error")
                        return False

                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    usage_by_folder_state["value"] = usage
                    dialog.close()
                    refresh_folders_table(f"Renamed to {new_name}.")
                    return False

                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        confirm_btn.connect("clicked", on_confirm)
        dialog.present()

    add_folder_btn.connect("clicked", open_add_folder_dialog)
    rename_folder_btn.connect("clicked", open_rename_folder_dialog)

    def open_connect_local_dialog(_btn) -> None:
        if not vault_id:
            return
        # Fetch the live manifest in a worker so the UI thread stays
        # responsive while the relay round-trip resolves.
        connect_local_btn.set_sensitive(False)

        def worker() -> None:
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(config_dir, config, vault_id)
                try:
                    manifest = vault.fetch_manifest(relay, local_index=local_index)
                finally:
                    vault.close()
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    connect_local_btn.set_sensitive(True)
                    folders_status.set_label(
                        f"Could not load manifest for connect: {error_message}"
                    )
                    return False

                GLib.idle_add(fail)
                return

            def show() -> bool:
                connect_local_btn.set_sensitive(True)
                choices = [
                    (str(f.get("display_name_enc", "")),
                     str(f.get("remote_folder_id", "")))
                    for f in manifest.get("remote_folders", []) or []
                    if isinstance(f, dict)
                    and str(f.get("state", "active")) == "active"
                ]
                if not choices:
                    folders_status.set_label(
                        "No remote folders yet — create one before connecting a "
                        "local folder."
                    )
                    return False
                store = VaultBindingsStore(local_index.db_path)

                def on_dialog_confirmed(record) -> None:
                    # Binding row was just written with state="needs-preflight";
                    # baseline below drives it to "bound" once the remote folder
                    # is materialized locally.
                    folders_status.set_label(
                        "Binding created — running initial baseline…"
                    )
                    threading.Thread(
                        target=lambda: _run_baseline_for_record(record),
                        daemon=True,
                    ).start()

                def _run_baseline_for_record(record) -> None:
                    try:
                        config.reload()
                        baseline_relay = create_vault_relay(config)
                        baseline_vault = open_local_vault_from_grant(
                            config_dir, config, vault_id,
                        )
                        try:
                            baseline_manifest = baseline_vault.fetch_manifest(
                                baseline_relay, local_index=local_index,
                            )
                            store_for_baseline = VaultBindingsStore(local_index.db_path)
                            binding_for_baseline = store_for_baseline.get_binding(
                                record.binding_id
                            )
                            if binding_for_baseline is None:
                                raise RuntimeError(
                                    f"binding row vanished: {record.binding_id}"
                                )
                            run_initial_baseline(
                                vault=baseline_vault,
                                relay=baseline_relay,
                                manifest=baseline_manifest,
                                store=store_for_baseline,
                                binding=binding_for_baseline,
                            )
                        finally:
                            baseline_vault.close()
                    except Exception as exc:  # noqa: BLE001
                        msg = str(exc)

                        def fail() -> bool:
                            folders_status.set_label(
                                f"Initial baseline failed: {msg}"
                            )
                            return False

                        GLib.idle_add(fail)
                        return

                    def succeed() -> bool:
                        folders_status.set_label(
                            "Binding ready — initial baseline complete."
                        )
                        return False

                    GLib.idle_add(succeed)

                present_connect_folder_dialog(
                    parent_window=parent_window,
                    folder_choices=choices,
                    manifest=manifest,
                    vault_id=vault_id,
                    store=store,
                    on_confirmed=on_dialog_confirmed,
                )
                return False

            GLib.idle_add(show)

        threading.Thread(target=worker, daemon=True).start()

    connect_local_btn.set_sensitive(bool(vault_id))
    connect_local_btn.connect("clicked", open_connect_local_dialog)

    # ----------------------- Bindings panel (T10.6) -----------------------

    sync_in_flight: dict[str, bool] = {}
    # F-Y08: per-binding cancellation registry. The Sync-now worker
    # registers an event before each cycle so a future Pause /
    # Disconnect button can call ``cancellation_registry.cancel(id)``
    # to abort an in-flight chunk loop. F-Y15 adds the buttons; the
    # registry plumbing here is the backbone they hook into.
    cancellation_registry = BindingCancellationRegistry()

    def clear_bindings_grid() -> None:
        child = bindings_grid.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            bindings_grid.remove(child)
            child = next_child

    def attach_binding_cell(text: str, col: int, row: int, *, header: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0, hexpand=(col == 0))
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if header:
            label.add_css_class("dim-label")
        bindings_grid.attach(label, col, row, 1, 1)
        return label

    def run_sync_now(binding_id: str, button: Gtk.Button) -> None:
        if sync_in_flight.get(binding_id):
            return
        sync_in_flight[binding_id] = True
        button.set_sensitive(False)
        bindings_status.set_label("Sync now: running…")

        def worker() -> None:
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(config_dir, config, vault_id)
                store = VaultBindingsStore(local_index.db_path)
                binding = store.get_binding(binding_id)
                if binding is None:
                    raise RuntimeError(f"binding not found: {binding_id}")
                author_device_id = config.device_id or ("0" * 32)
                device_name = (
                    str(config.device_name or "").strip() or "this device"
                )
                event = cancellation_registry.register(binding_id)
                try:
                    result = flush_and_sync_binding(
                        vault=vault, relay=relay, store=store,
                        binding=binding, author_device_id=author_device_id,
                        device_name=device_name,
                        should_continue=lambda: not event.is_set(),
                    )
                finally:
                    vault.close()
                    cancellation_registry.clear(binding_id)
                toast_text = format_sync_outcome_toast(result)
            except Exception as exc:  # noqa: BLE001
                error_message = humanize(exc)

                def fail() -> bool:
                    sync_in_flight[binding_id] = False
                    button.set_sensitive(True)
                    bindings_status.set_label(
                        f"Sync now failed: {error_message}"
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                sync_in_flight[binding_id] = False
                button.set_sensitive(True)
                bindings_status.set_label(toast_text)
                refresh_bindings_table(toast_text)
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------- F-Y15: Pause / Resume / Disconnect ------

    action_in_flight: dict[str, bool] = {}

    def _set_inflight(binding_id: str, value: bool) -> None:
        action_in_flight[binding_id] = value

    def _idle_finish(
        toast: str | None,
        error: str | None,
        binding_id: str,
        prefix: str,
    ) -> None:
        def apply() -> bool:
            _set_inflight(binding_id, False)
            if error:
                bindings_status.set_label(error)
                refresh_bindings_table(error)
            else:
                bindings_status.set_label(toast or f"{prefix} done.")
                refresh_bindings_table(toast)
            return False
        GLib.idle_add(apply)

    def run_pause(binding_id: str) -> None:
        if action_in_flight.get(binding_id):
            return
        _set_inflight(binding_id, True)
        bindings_status.set_label("Pause: running…")

        def worker() -> None:
            store = VaultBindingsStore(local_index.db_path)
            toast, error = dispatch_pause(
                store=store, binding_id=binding_id,
                cancellation=cancellation_registry,
            )
            _idle_finish(toast, error, binding_id, "Pause")

        threading.Thread(target=worker, daemon=True).start()

    def run_resume(binding_id: str) -> None:
        if action_in_flight.get(binding_id):
            return
        _set_inflight(binding_id, True)
        bindings_status.set_label("Resume: running…")

        def worker() -> None:
            store = VaultBindingsStore(local_index.db_path)

            def flush(binding) -> object:
                # Resume reuses the Sync-now plumbing — same vault open,
                # same registry registration, same should_continue gate
                # so a fresh Pause arriving during the post-resume flush
                # still aborts within ~1 chunk.
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(config_dir, config, vault_id)
                event = cancellation_registry.register(binding_id)
                try:
                    return flush_and_sync_binding(
                        vault=vault, relay=relay, store=store,
                        binding=binding,
                        author_device_id=config.device_id or ("0" * 32),
                        device_name=(
                            str(config.device_name or "").strip()
                            or "this device"
                        ),
                        should_continue=lambda: not event.is_set(),
                    )
                finally:
                    vault.close()
                    cancellation_registry.clear(binding_id)

            toast, error = dispatch_resume(
                store=store, binding_id=binding_id, flush=flush,
            )
            _idle_finish(toast, error, binding_id, "Resume")

        threading.Thread(target=worker, daemon=True).start()

    def run_disconnect(binding_id: str) -> None:
        if action_in_flight.get(binding_id):
            return
        _set_inflight(binding_id, True)
        bindings_status.set_label("Disconnect: confirming…")

        # Dialog runs on GTK main thread; worker waits via Condition.
        decision: dict[str, bool] = {}
        cv = threading.Condition()

        def show_dialog() -> bool:
            dialog = Adw.AlertDialog(
                heading="Disconnect this folder?",
                body=(
                    "Pending sync operations for this binding will be "
                    "dropped. Local files and the remote vault are not "
                    "touched. You can re-connect the folder later."
                ),
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("disconnect", "Disconnect")
            dialog.set_response_appearance(
                "disconnect", Adw.ResponseAppearance.DESTRUCTIVE,
            )
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(_d, response: str) -> None:
                with cv:
                    decision["confirmed"] = (response == "disconnect")
                    cv.notify_all()

            dialog.connect("response", on_response)
            dialog.present(parent_window)
            return False

        GLib.idle_add(show_dialog)

        def worker() -> None:
            with cv:
                while "confirmed" not in decision:
                    cv.wait()
            confirmed = decision["confirmed"]
            store = VaultBindingsStore(local_index.db_path)
            toast, error = dispatch_disconnect(
                store=store, binding_id=binding_id,
                confirm=lambda: confirmed,
                cancellation=cancellation_registry,
            )
            _idle_finish(toast, error, binding_id, "Disconnect")

        threading.Thread(target=worker, daemon=True).start()

    def refresh_bindings_table(message: str | None = None) -> None:
        clear_bindings_grid()
        # Header row + Action column
        for col, title in enumerate(BINDING_COLUMNS):
            attach_binding_cell(title, col, 0, header=True)
        attach_binding_cell("Action", len(BINDING_COLUMNS), 0, header=True)

        store = VaultBindingsStore(local_index.db_path)
        try:
            binding_records = (
                store.list_bindings(vault_id=vault_id) if vault_id else []
            )
        except Exception as exc:  # noqa: BLE001
            bindings_status.set_label(f"Could not load bindings: {exc}")
            binding_records = []

        try:
            cached_folders = (
                local_index.list_remote_folders(vault_id) if vault_id else []
            )
        except Exception:
            cached_folders = []
        folder_names = {
            str(f.get("remote_folder_id", "")): str(f.get("display_name_enc") or "")
            for f in cached_folders
        }

        rows = binding_rows_for_render(
            binding_records, folder_names_by_id=folder_names,
        )

        if not rows:
            empty = (
                "No local bindings yet. Use 'Connect local folder…' above."
                if vault_id else
                "Open a vault before connecting bindings."
            )
            attach_binding_cell(empty, 0, 1)
        else:
            for row_index, row in enumerate(rows, start=1):
                attach_binding_cell(row["local_path"], 0, row_index)
                attach_binding_cell(row["remote_folder"], 1, row_index)
                attach_binding_cell(row["state"], 2, row_index)
                attach_binding_cell(row["sync_mode"], 3, row_index)
                attach_binding_cell(row["last_synced_revision"], 4, row_index)
                # F-Y15: action cluster keyed on state. ``bound`` shows
                # Sync now / Pause / Disconnect; ``paused`` swaps Sync
                # now → Resume; ``unbound`` is read-only.
                action_box = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                )
                bid = row["binding_id"]
                state = row["state"]

                if state == "bound":
                    sync_btn = Gtk.Button(label="Sync now", css_classes=["pill"])
                    sync_btn.set_tooltip_text(
                        "Drain pending local changes and push them to the vault now."
                    )
                    sync_btn.connect(
                        "clicked",
                        lambda _b, bid=bid, btn=sync_btn: run_sync_now(bid, btn),
                    )
                    action_box.append(sync_btn)

                    pause_btn = Gtk.Button(label="Pause", css_classes=["pill"])
                    pause_btn.set_tooltip_text(
                        "Pause syncing. The watcher keeps queuing ops; "
                        "Resume picks up where you left off."
                    )
                    pause_btn.connect(
                        "clicked", lambda _b, bid=bid: run_pause(bid),
                    )
                    action_box.append(pause_btn)

                    disconnect_btn = Gtk.Button(
                        label="Disconnect",
                        css_classes=["pill", "destructive-action"],
                    )
                    disconnect_btn.set_tooltip_text(
                        "Stop syncing this folder. Pending ops are dropped; "
                        "local files and the remote vault are not touched."
                    )
                    disconnect_btn.connect(
                        "clicked", lambda _b, bid=bid: run_disconnect(bid),
                    )
                    action_box.append(disconnect_btn)
                elif state == "paused":
                    resume_btn = Gtk.Button(
                        label="Resume",
                        css_classes=["pill", "suggested-action"],
                    )
                    resume_btn.set_tooltip_text(
                        "Resume syncing and drain anything the watcher queued."
                    )
                    resume_btn.connect(
                        "clicked", lambda _b, bid=bid: run_resume(bid),
                    )
                    action_box.append(resume_btn)

                    disconnect_btn = Gtk.Button(
                        label="Disconnect",
                        css_classes=["pill", "destructive-action"],
                    )
                    disconnect_btn.set_tooltip_text(
                        "Stop syncing this folder. Pending ops are dropped; "
                        "local files and the remote vault are not touched."
                    )
                    disconnect_btn.connect(
                        "clicked", lambda _b, bid=bid: run_disconnect(bid),
                    )
                    action_box.append(disconnect_btn)
                else:
                    # ``unbound`` / ``needs-preflight`` etc. — render an
                    # empty cell so the column width stays stable.
                    pass

                bindings_grid.attach(
                    action_box, len(BINDING_COLUMNS), row_index, 1, 1,
                )

        if message is not None:
            bindings_status.set_label(message)
        elif rows:
            bindings_status.set_label(f"{len(rows)} binding(s).")
        elif vault_id:
            bindings_status.set_label("No bindings yet.")
        else:
            bindings_status.set_label("")

    refresh_folders_table()
    refresh_folders_usage_async()
    refresh_bindings_table()
    return folders
