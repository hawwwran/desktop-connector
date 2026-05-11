"""Configure-folder dialog for the Folders tab.

Edit the remote folder's name + ignore patterns post-create.

F-LT12: replaces the rename-only dialog with a single Configure
dialog so users can fix up ignore patterns after the initial Add —
before this, those patterns were locked in at creation time with no
later editing path.
"""

from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

from ..vault.error_messages import humanize
from ..vault.folder.ui_state import parse_ignore_patterns_text
from ..vault_usage import calculate_vault_usage
from .context import FoldersContext
from .data import lookup_folder_settings


def open_configure_folder_dialog(
    ctx: FoldersContext, remote_folder_id: str,
) -> None:
    """Edit the remote folder's name + ignore patterns post-create."""
    if not ctx.vault_id or not remote_folder_id:
        return

    current_name, current_patterns = lookup_folder_settings(ctx, remote_folder_id)

    dialog = Adw.Dialog()
    dialog.set_title("Configure folder")
    dialog.set_content_width(540)
    dialog_toolbar = Adw.ToolbarView()
    dialog.set_child(dialog_toolbar)
    dialog_toolbar.add_top_bar(Adw.HeaderBar())

    body_box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        margin_top=16, margin_bottom=16,
        margin_start=16, margin_end=16,
    )
    dialog_toolbar.set_content(body_box)
    body_box.append(Gtk.Label(
        label="Configure folder",
        xalign=0, css_classes=["title-2"],
    ))

    body_box.append(Gtk.Label(
        label="Name", xalign=0, css_classes=["dim-label"],
    ))
    name_entry = Gtk.Entry(hexpand=True)
    name_entry.set_text(current_name)
    body_box.append(name_entry)

    body_box.append(Gtk.Label(
        label="Ignore patterns", xalign=0, css_classes=["dim-label"],
    ))
    ignore_buffer = Gtk.TextBuffer()
    ignore_buffer.set_text(
        "\n".join(current_patterns) + ("\n" if current_patterns else "")
    )
    ignore_view = Gtk.TextView(buffer=ignore_buffer, monospace=True)
    ignore_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    ignore_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    ignore_scroller.set_min_content_height(140)
    ignore_scroller.set_child(ignore_view)
    body_box.append(ignore_scroller)

    dialog_status = Gtk.Label(
        xalign=0, wrap=True, css_classes=["dim-label"],
    )
    body_box.append(dialog_status)

    dialog_buttons = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL,
        spacing=8,
        halign=Gtk.Align.END,
        margin_top=8, margin_bottom=12,
        margin_start=16, margin_end=16,
    )
    dialog_toolbar.add_bottom_bar(dialog_buttons)
    cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
    confirm_btn = Gtk.Button(
        label="Save", css_classes=["pill", "suggested-action"],
    )
    dialog_buttons.append(cancel_btn)
    dialog_buttons.append(confirm_btn)

    cancel_btn.connect("clicked", lambda _button: dialog.close())

    def set_dialog_status(
        message: str, css_class: str = "dim-label",
    ) -> None:
        for klass in ("dim-label", "error", "success"):
            dialog_status.remove_css_class(klass)
        dialog_status.add_css_class(css_class)
        dialog_status.set_label(message)

    def read_ignore_patterns() -> list[str]:
        start = ignore_buffer.get_start_iter()
        end = ignore_buffer.get_end_iter()
        return parse_ignore_patterns_text(
            ignore_buffer.get_text(start, end, False),
        )

    def on_confirm(_button) -> None:
        new_name = name_entry.get_text().strip()
        if not new_name:
            set_dialog_status("Enter a folder name.", "error")
            return
        new_patterns = read_ignore_patterns()
        name_changed = new_name != current_name
        patterns_changed = new_patterns != current_patterns
        if not name_changed and not patterns_changed:
            set_dialog_status("Nothing to save.", "error")
            return

        confirm_btn.set_sensitive(False)
        cancel_btn.set_sensitive(False)
        name_entry.set_sensitive(False)
        ignore_view.set_sensitive(False)
        set_dialog_status("Saving folder configuration…", "dim-label")

        def worker() -> None:
            try:
                author_device_id = ctx.config.device_id or ("0" * 32)
                manifest = ctx.runtime.update_remote_folder_settings(
                    remote_folder_id=remote_folder_id,
                    author_device_id=author_device_id,
                    new_display_name=new_name if name_changed else None,
                    ignore_patterns=(
                        new_patterns if patterns_changed else None
                    ),
                )
                usage = calculate_vault_usage(manifest).by_folder
            except Exception as exc:  # noqa: BLE001
                error_message = humanize(exc)

                def fail() -> bool:
                    confirm_btn.set_sensitive(True)
                    cancel_btn.set_sensitive(True)
                    name_entry.set_sensitive(True)
                    ignore_view.set_sensitive(True)
                    set_dialog_status(
                        f"Could not save folder: {error_message}",
                        "error",
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                ctx.usage_by_folder_state["value"] = usage
                dialog.close()
                ctx.refresh_all(f"Saved {new_name}.")
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    confirm_btn.connect("clicked", on_confirm)
    dialog.present(ctx.parent_window)
