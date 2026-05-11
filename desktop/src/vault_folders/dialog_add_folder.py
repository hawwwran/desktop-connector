"""Add-folder dialog for the Folders tab.

Wraps the ``open_add_folder_dialog`` closure that lived in the
pre-split ``build_vault_folders_tab``. Builds the GTK form, spawns a
worker thread on confirm to call ``runtime.add_remote_folder``, and
auto-selects the freshly-added folder so the user lands on its detail
pane.
"""

from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

from ..vault.error_messages import humanize
from ..vault_folder_ui_state import (
    default_ignore_patterns_text,
    parse_ignore_patterns_text,
)
from ..vault_usage import calculate_vault_usage
from .context import FoldersContext
from .dialog_response_details import _present_response_details_dialog


def open_add_folder_dialog(ctx: FoldersContext, _btn=None) -> None:
    dialog = Adw.Dialog()
    dialog.set_title("Add folder")
    dialog.set_content_width(540)
    dialog_toolbar = Adw.ToolbarView()
    dialog.set_child(dialog_toolbar)
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
    body_box.append(Gtk.Label(
        label="Add remote folder", xalign=0, css_classes=["title-2"],
    ))

    body_box.append(Gtk.Label(
        label="Name", xalign=0, css_classes=["dim-label"],
    ))
    name_entry = Gtk.Entry(hexpand=True)
    name_entry.set_placeholder_text("Folder name")
    body_box.append(name_entry)

    body_box.append(Gtk.Label(
        label="Ignore patterns", xalign=0, css_classes=["dim-label"],
    ))
    ignore_buffer = Gtk.TextBuffer()
    ignore_buffer.set_text(default_ignore_patterns_text())
    ignore_view = Gtk.TextView(buffer=ignore_buffer, monospace=True)
    ignore_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    ignore_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    ignore_scroller.set_min_content_height(140)
    ignore_scroller.set_child(ignore_view)
    body_box.append(ignore_scroller)

    dialog_status_row = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL,
        spacing=6,
    )
    dialog_status = Gtk.Label(
        xalign=0,
        wrap=True,
        hexpand=True,
        css_classes=["dim-label"],
    )
    dialog_status_row.append(dialog_status)
    dialog_details_btn = Gtk.Button(
        icon_name="dialog-information-symbolic",
        tooltip_text="Show details",
        valign=Gtk.Align.START,
        css_classes=["flat", "circular"],
        visible=False,
    )
    dialog_status_row.append(dialog_details_btn)
    body_box.append(dialog_status_row)

    dialog_details_state: dict[str, str] = {"text": ""}

    def on_details_clicked(_btn) -> None:
        _present_response_details_dialog(
            dialog, dialog_details_state["text"],
        )

    dialog_details_btn.connect("clicked", on_details_clicked)

    dialog_buttons = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL,
        spacing=8,
        halign=Gtk.Align.END,
        margin_top=8,
        margin_bottom=12,
        margin_start=16,
        margin_end=16,
    )
    dialog_toolbar.add_bottom_bar(dialog_buttons)
    cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
    confirm_btn = Gtk.Button(
        label="Add", css_classes=["pill", "suggested-action"],
    )
    dialog_buttons.append(cancel_btn)
    dialog_buttons.append(confirm_btn)

    cancel_btn.connect("clicked", lambda _button: dialog.close())

    def set_dialog_status(
        message: str,
        css_class: str = "dim-label",
        *,
        details: str | None = None,
    ) -> None:
        for klass in ("dim-label", "error", "success"):
            dialog_status.remove_css_class(klass)
        dialog_status.add_css_class(css_class)
        dialog_status.set_label(message)
        if details:
            dialog_details_state["text"] = details
            dialog_details_btn.set_visible(True)
        else:
            dialog_details_state["text"] = ""
            dialog_details_btn.set_visible(False)

    def read_ignore_patterns() -> list[str]:
        start = ignore_buffer.get_start_iter()
        end = ignore_buffer.get_end_iter()
        return parse_ignore_patterns_text(
            ignore_buffer.get_text(start, end, False),
        )

    def on_confirm(_button) -> None:
        folder_name = name_entry.get_text().strip()
        if not folder_name:
            set_dialog_status("Enter a folder name.", "error")
            return
        if not ctx.vault_id:
            set_dialog_status("No local vault is connected.", "error")
            return

        patterns = read_ignore_patterns()
        confirm_btn.set_sensitive(False)
        cancel_btn.set_sensitive(False)
        set_dialog_status("Adding folder...", "dim-label")

        def worker() -> None:
            try:
                author_device_id = ctx.config.device_id or ("0" * 32)
                manifest = ctx.runtime.add_remote_folder(
                    display_name=folder_name,
                    ignore_patterns=patterns,
                    author_device_id=author_device_id,
                )
                usage = calculate_vault_usage(manifest).by_folder
            except Exception as exc:  # noqa: BLE001
                error_message = humanize(exc)
                error_details = getattr(exc, "response_text", None) or None

                def fail() -> bool:
                    confirm_btn.set_sensitive(True)
                    cancel_btn.set_sensitive(True)
                    set_dialog_status(
                        f"Could not add folder: {error_message}",
                        "error",
                        details=error_details,
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                ctx.usage_by_folder_state["value"] = usage
                dialog.close()
                # Auto-select the freshly-added folder so the user
                # lands on its detail pane and can immediately wire
                # up a binding without a second navigation step.
                for f in (manifest.get("remote_folders") or []):
                    if str(f.get("display_name_enc") or "") == folder_name:
                        ctx.selection_state["folder_id"] = str(
                            f.get("remote_folder_id") or ""
                        )
                        break
                ctx.refresh_all(f"Added {folder_name}.")
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    confirm_btn.connect("clicked", on_confirm)
    dialog.present(ctx.parent_window)
