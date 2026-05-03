"""GTK builder for the Vault settings Folders tab."""

from __future__ import annotations

import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

from .vault_cache import VaultLocalIndex
from .vault_folder_ui_state import (
    FOLDER_COLUMNS,
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
    delete_folder_btn = Gtk.Button(label="Delete", css_classes=["pill", "destructive-action"])
    delete_folder_btn.set_sensitive(False)
    delete_folder_btn.set_tooltip_text("Folder delete is implemented in T7/T14")
    folder_actions.append(add_folder_btn)
    folder_actions.append(rename_folder_btn)
    folder_actions.append(delete_folder_btn)

    folders_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    folders.append(folders_status)

    folders_grid = Gtk.Grid(column_spacing=16, row_spacing=8, hexpand=True)
    folders.append(folders_grid)

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
                error_message = str(exc)

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
                    error_message = str(exc)

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
                    error_message = str(exc)

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
    refresh_folders_table()
    refresh_folders_usage_async()
    return folders
