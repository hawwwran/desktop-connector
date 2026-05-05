"""Connect-local-folder dialog (T10.2).

Opens an Adw.Dialog over the Vault settings window:

    [Remote folder dropdown ▾]
    [Local path: ____  Browse…]
    [Preflight summary panel — §D15 wording with separate tombstone line]
    [Sync mode dropdown ▾ default Backup only]
    [Cancel]   [Connect]

On Connect: writes a row to vault_bindings with state =
``needs-preflight``. The initial baseline (T10.3) flips it to ``bound``
once it's downloaded the remote folder's current state to the local
path. On Cancel: no row is written — the dialog is the only mutation
point per T10.2 acceptance.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

from .vault_binding_preflight import (
    PreflightSummary,
    compute_preflight,
    render_preflight_text,
)
from .vault_bindings import (
    DEFAULT_SYNC_MODE,
    VaultBindingsStore,
)


SYNC_MODE_LABELS = [
    ("backup-only",   "Backup only — uploads local changes; never pulls remote down"),
    ("two-way",       "Two-way sync — push and pull"),
    ("download-only", "Download only — pull remote changes; never upload local"),
    ("paused",        "Paused — keep the binding but don't move data"),
]
DEFAULT_MODE_INDEX = next(
    i for i, (mode, _label) in enumerate(SYNC_MODE_LABELS)
    if mode == DEFAULT_SYNC_MODE
)


def present_connect_folder_dialog(
    *,
    parent_window: Gtk.Window,
    folder_choices: list[tuple[str, str]],   # [(display_name, remote_folder_id), ...]
    manifest: dict[str, Any],
    vault_id: str,
    store: VaultBindingsStore,
    on_confirmed: Callable[[Any], None] | None = None,
) -> Adw.Dialog:
    """Build and present the connect-folder dialog."""
    dialog = Adw.Dialog()
    dialog.set_title("Connect local folder")
    dialog.set_content_width(560)

    container = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=14,
        margin_top=20, margin_bottom=20, margin_start=20, margin_end=20,
    )
    dialog.set_child(container)

    container.append(Gtk.Label(
        label="Connect a remote vault folder to a local path.",
        xalign=0, css_classes=["title-3"],
    ))

    # ---- remote folder dropdown -----------------------------------------
    folder_row = Adw.ActionRow(title="Remote folder")
    folder_dropdown = Gtk.DropDown.new_from_strings(
        [name for name, _rfid in folder_choices] or ["(no remote folders yet)"],
    )
    folder_dropdown.set_hexpand(True)
    folder_row.add_suffix(folder_dropdown)
    container.append(folder_row)

    # ---- local path picker ---------------------------------------------
    state: dict[str, Any] = {"local_path": None, "preflight": None}
    path_row = Adw.ActionRow(title="Local folder", subtitle="No folder selected")
    pick_btn = Gtk.Button(label="Choose…", css_classes=["pill"])
    path_row.add_suffix(pick_btn)
    container.append(path_row)

    # ---- preflight summary panel ----------------------------------------
    preflight_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    preflight_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    container.append(preflight_label)

    # ---- sync mode dropdown --------------------------------------------
    mode_row = Adw.ActionRow(title="Sync mode")
    mode_dropdown = Gtk.DropDown.new_from_strings(
        [label for _mode, label in SYNC_MODE_LABELS],
    )
    mode_dropdown.set_selected(DEFAULT_MODE_INDEX)
    mode_dropdown.set_hexpand(True)
    mode_row.add_suffix(mode_dropdown)
    container.append(mode_row)

    # ---- actions -------------------------------------------------------
    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    actions.set_halign(Gtk.Align.END)
    cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
    connect_btn = Gtk.Button(
        label="Connect",
        css_classes=["pill", "suggested-action"],
    )
    connect_btn.set_sensitive(False)
    actions.append(cancel_btn)
    actions.append(connect_btn)
    container.append(actions)

    status_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    container.append(status_label)

    # ---- handlers ------------------------------------------------------

    def selected_remote() -> tuple[str, str] | None:
        idx = folder_dropdown.get_selected()
        if 0 <= idx < len(folder_choices):
            return folder_choices[idx]
        return None

    def selected_mode() -> str:
        idx = mode_dropdown.get_selected()
        if 0 <= idx < len(SYNC_MODE_LABELS):
            return SYNC_MODE_LABELS[idx][0]
        return DEFAULT_SYNC_MODE

    def refresh_preflight() -> None:
        local_path = state.get("local_path")
        remote = selected_remote()
        if local_path is None or remote is None:
            preflight_label.set_label(
                "Pick a remote folder and a local path to see the preflight summary."
            )
            connect_btn.set_sensitive(False)
            return
        try:
            summary = compute_preflight(
                manifest=manifest,
                remote_folder_id=remote[1],
                local_root=local_path,
            )
        except Exception as exc:
            preflight_label.set_label(f"Could not run preflight: {exc}")
            connect_btn.set_sensitive(False)
            return
        state["preflight"] = summary
        preflight_label.set_label(render_preflight_text(summary))
        # Connect is enabled when the preflight had no fatal warning.
        # F-512: writable is the only authoritative gate.
        # ``compute_preflight`` already covers the "doesn't exist yet
        # but parent is writable" case, so OR-ing in
        # ``local_path_exists`` was theatre that allowed read-only
        # destinations through.
        connect_btn.set_sensitive(bool(summary.local_path_writable))

    def on_pick_local(_btn) -> None:
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title("Choose local folder for the binding")

        def on_chosen(file_dialog, result) -> None:
            try:
                gio_file = file_dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if gio_file is None:
                return
            chosen = gio_file.get_path()
            if not chosen:
                return
            state["local_path"] = Path(chosen)
            path_row.set_subtitle(chosen)
            refresh_preflight()

        file_dialog.select_folder(parent=parent_window, callback=on_chosen)

    def on_dropdown_changed(_dropdown, _pspec) -> None:
        refresh_preflight()

    def on_cancel(_btn) -> None:
        # Acceptance: cancellation leaves no rows.
        dialog.close()

    def on_connect(_btn) -> None:
        remote = selected_remote()
        if remote is None or state["local_path"] is None:
            status_label.set_label("Pick both a remote folder and a local path.")
            return
        connect_btn.set_sensitive(False)
        cancel_btn.set_sensitive(False)
        status_label.set_label("Creating binding…")

        def worker() -> None:
            try:
                record = store.create_binding(
                    vault_id=vault_id,
                    remote_folder_id=remote[1],
                    local_path=str(state["local_path"]),
                    state="needs-preflight",
                    sync_mode=selected_mode(),
                )
            except Exception as exc:
                msg = str(exc)

                def fail() -> bool:
                    connect_btn.set_sensitive(True)
                    cancel_btn.set_sensitive(True)
                    status_label.set_label(f"Could not connect: {msg}")
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                if on_confirmed is not None:
                    try:
                        on_confirmed(record)
                    except Exception:
                        pass
                dialog.close()
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    folder_dropdown.connect("notify::selected", on_dropdown_changed)
    pick_btn.connect("clicked", on_pick_local)
    cancel_btn.connect("clicked", on_cancel)
    connect_btn.connect("clicked", on_connect)

    refresh_preflight()
    dialog.present(parent_window)
    return dialog


__all__ = ["present_connect_folder_dialog", "SYNC_MODE_LABELS"]
