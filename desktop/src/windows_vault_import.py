"""GTK4 import wizard for the Vault (T8.5).

A linear ``Gtk.Stack`` with five pages — pick file, enter passphrase,
review preview, run import, summary. The wizard delegates all the
heavy lifting to :mod:`vault_import_runner`; this module is just the
GTK glue, page transitions, and worker-thread plumbing.

Conflict-resolution UI (per-folder mode picker + "apply to remaining")
is not yet wired here: T8.4's `merge_import_into` accepts an
`ImportMergeResolution` so we'll layer the conflict UX on the same
backbone in a follow-up. For T8.5 the wizard always uses the §D9
default (`rename`), which is the conservative choice — bundle copies
land beside the active vault's files instead of overwriting them.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .vault_cache import VaultLocalIndex
from .vault_export import ExportError
from .vault_export_reminder import normalize_cadence
from .vault_import import ImportMergeResolution
from .vault_runtime import create_vault_relay, open_local_vault_from_grant
from .windows_common import _make_app


def show_vault_import(config_dir: Path) -> None:
    """Top-level entry point for ``--gtk-window=vault-import``."""
    from .config import Config

    config = Config(config_dir)
    local_index = VaultLocalIndex(config_dir)
    app = _make_app()

    state: dict = {
        "bundle_path": None,
        "passphrase": None,
        "preview": None,
        "bundle_contents": None,
        "bundle_manifest": None,
        "active_manifest": None,
        "result": None,
    }

    def local_vault_id() -> str:
        config.reload()
        raw = config._data.get("vault")
        if not isinstance(raw, dict):
            return ""
        return str(raw.get("last_known_id") or "")

    def on_activate(app: Adw.Application) -> None:
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)

        win = Adw.ApplicationWindow(
            application=app,
            title="Import vault bundle",
            default_width=720,
            default_height=520,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        toolbar.set_content(stack)

        def go_to(name: str) -> None:
            stack.set_visible_child_name(name)

        # ---- Page 1: pick file + passphrase ---------------------------
        pick_box = _vbox(margin=24, spacing=14)
        pick_box.append(Gtk.Label(
            label="Import a vault export bundle",
            xalign=0,
            css_classes=["title-2"],
        ))
        pick_box.append(Gtk.Label(
            label=(
                "Choose a .dcvault file and enter the export passphrase. "
                "Imports merge into the active vault per the §D9 rules; "
                "conflicts default to a (conflict imported) rename."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        file_row = Adw.ActionRow(title="Bundle file", subtitle="No file selected")
        pick_btn = Gtk.Button(label="Choose…", css_classes=["pill"])
        file_row.add_suffix(pick_btn)
        pick_box.append(file_row)

        passphrase_entry = Gtk.PasswordEntry(placeholder_text="Export passphrase")
        passphrase_entry.set_show_peek_icon(True)
        pick_box.append(passphrase_entry)

        pick_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        pick_box.append(pick_status)

        pick_actions = _hbox(spacing=8)
        pick_actions.set_halign(Gtk.Align.END)
        cancel_btn_1 = Gtk.Button(label="Cancel", css_classes=["pill"])
        open_btn = Gtk.Button(
            label="Open bundle",
            css_classes=["pill", "suggested-action"],
        )
        open_btn.set_sensitive(False)
        pick_actions.append(cancel_btn_1)
        pick_actions.append(open_btn)
        pick_box.append(pick_actions)

        stack.add_named(pick_box, "pick")

        # ---- Page 2: preview ------------------------------------------
        preview_box = _vbox(margin=24, spacing=12)
        preview_box.append(Gtk.Label(
            label="Bundle preview",
            xalign=0,
            css_classes=["title-2"],
        ))
        preview_grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        preview_box.append(preview_grid)
        preview_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        preview_box.append(preview_status)
        preview_actions = _hbox(spacing=8)
        preview_actions.set_halign(Gtk.Align.END)
        cancel_btn_2 = Gtk.Button(label="Cancel", css_classes=["pill"])
        import_btn = Gtk.Button(
            label="Import",
            css_classes=["pill", "suggested-action"],
        )
        preview_actions.append(cancel_btn_2)
        preview_actions.append(import_btn)
        preview_box.append(preview_actions)
        stack.add_named(preview_box, "preview")

        # ---- Page 3: progress -----------------------------------------
        progress_box = _vbox(margin=24, spacing=14)
        progress_box.append(Gtk.Label(
            label="Importing…",
            xalign=0, css_classes=["title-2"],
        ))
        progress_label = Gtk.Label(xalign=0, wrap=True)
        progress_bar = Gtk.ProgressBar(show_text=True)
        progress_box.append(progress_label)
        progress_box.append(progress_bar)
        stack.add_named(progress_box, "progress")

        # ---- Page 4: summary ------------------------------------------
        summary_box = _vbox(margin=24, spacing=12)
        summary_title = Gtk.Label(xalign=0, css_classes=["title-2"])
        summary_body = Gtk.Label(xalign=0, wrap=True)
        summary_actions = _hbox(spacing=8)
        summary_actions.set_halign(Gtk.Align.END)
        close_btn = Gtk.Button(label="Close", css_classes=["pill", "suggested-action"])
        summary_actions.append(close_btn)
        summary_box.append(summary_title)
        summary_box.append(summary_body)
        summary_box.append(summary_actions)
        stack.add_named(summary_box, "summary")

        # ---------------------------------------------------------------

        def cancel(_btn=None) -> None:
            win.close()

        def update_open_btn_sensitive() -> None:
            open_btn.set_sensitive(
                state["bundle_path"] is not None
                and bool(passphrase_entry.get_text())
            )

        def on_pick(_btn) -> None:
            dlg = Gtk.FileDialog()
            dlg.set_title("Select vault export bundle")

            def on_file_chosen(file_dialog, result) -> None:
                try:
                    gio_file = file_dialog.open_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path_text = gio_file.get_path()
                if not path_text:
                    return
                state["bundle_path"] = Path(path_text)
                file_row.set_subtitle(path_text)
                update_open_btn_sensitive()

            dlg.open(parent=win, callback=on_file_chosen)

        def on_passphrase_changed(_entry) -> None:
            update_open_btn_sensitive()

        def on_open(_btn) -> None:
            state["passphrase"] = passphrase_entry.get_text()
            vault_id = local_vault_id()
            if not vault_id:
                pick_status.set_label("No active vault on this device.")
                return
            open_btn.set_sensitive(False)
            pick_status.set_label("Opening bundle…")

            def worker() -> None:
                try:
                    from .vault_import_runner import open_bundle_for_preview

                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        active_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        contents, bundle_manifest, preview = open_bundle_for_preview(
                            vault=vault,
                            bundle_path=state["bundle_path"],
                            passphrase=state["passphrase"],
                            active_manifest=active_manifest,
                            active_genesis_fingerprint=None,
                            bundle_genesis_fingerprint=None,
                            chunks_already_on_relay=0,  # filled at run-time
                        )
                    finally:
                        vault.close()
                except ExportError as exc:
                    def fail() -> bool:
                        open_btn.set_sensitive(True)
                        pick_status.set_label(f"Could not open bundle: {exc.code}: {exc}")
                        return False
                    GLib.idle_add(fail)
                    return
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        open_btn.set_sensitive(True)
                        pick_status.set_label(f"Could not open bundle: {error_message}")
                        return False
                    GLib.idle_add(fail)
                    return

                def show_preview() -> bool:
                    state["bundle_contents"] = contents
                    state["bundle_manifest"] = bundle_manifest
                    state["preview"] = preview
                    state["active_manifest"] = active_manifest
                    _render_preview(preview_grid, preview)
                    if preview.fingerprint_status == "different_vault":
                        preview_status.set_label(
                            "This bundle is for a *different* vault. Import is "
                            "refused — switch active vault first."
                        )
                        import_btn.set_sensitive(False)
                    else:
                        preview_status.set_label("")
                        import_btn.set_sensitive(True)
                    go_to("preview")
                    return False
                GLib.idle_add(show_preview)

            threading.Thread(target=worker, daemon=True).start()

        def on_import(_btn) -> None:
            import_btn.set_sensitive(False)
            progress_label.set_label("Uploading missing chunks…")
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("")
            go_to("progress")

            def report(progress) -> None:
                def update() -> bool:
                    if progress.chunks_total > 0:
                        fraction = (progress.chunks_uploaded + progress.chunks_skipped) / max(
                            1, progress.chunks_total,
                        )
                        progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                        progress_bar.set_text(
                            f"{progress.chunks_uploaded + progress.chunks_skipped}/"
                            f"{progress.chunks_total} chunks"
                        )
                    progress_label.set_label({
                        "uploading_chunks": "Uploading missing chunks…",
                        "publishing": "Publishing merged manifest…",
                        "done": "Finished.",
                    }.get(progress.phase, progress.phase))
                    return False
                GLib.idle_add(update)

            def worker() -> None:
                try:
                    from .vault_import_runner import run_import

                    vault_id = local_vault_id()
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        active_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        device_id = str(getattr(config, "device_id", "") or "0" * 32)
                        result = run_import(
                            vault=vault, relay=relay,
                            bundle_path=state["bundle_path"],
                            passphrase=state["passphrase"],
                            active_manifest=active_manifest,
                            resolution=ImportMergeResolution(per_folder={}),
                            author_device_id=device_id,
                            progress=report,
                            local_index=local_index,
                        )
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        summary_title.set_label("Import failed")
                        summary_body.set_label(error_message)
                        go_to("summary")
                        return False
                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    state["result"] = result
                    if result.action == "refuse":
                        summary_title.set_label("Import refused")
                        summary_body.set_label(
                            "The bundle is for a different vault. Import was not "
                            "performed; switch active vault and try again."
                        )
                    else:
                        merge_summary = result.merge
                        renamed = (
                            len(merge_summary.renamed_paths) if merge_summary else 0
                        )
                        new = len(merge_summary.new_paths) if merge_summary else 0
                        overwritten = (
                            len(merge_summary.overwritten_paths) if merge_summary else 0
                        )
                        skipped = (
                            len(merge_summary.skipped_paths) if merge_summary else 0
                        )
                        summary_title.set_label("Import complete")
                        summary_body.set_label(
                            f"Uploaded {result.chunks_uploaded} chunks, "
                            f"skipped {result.chunks_skipped}.\n"
                            f"New: {new}; renamed: {renamed}; "
                            f"overwritten: {overwritten}; skipped: {skipped}.\n"
                            "The browser shows the merged result on next refresh."
                        )
                    go_to("summary")
                    return False
                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        pick_btn.connect("clicked", on_pick)
        passphrase_entry.connect("changed", on_passphrase_changed)
        open_btn.connect("clicked", on_open)
        cancel_btn_1.connect("clicked", cancel)
        cancel_btn_2.connect("clicked", cancel)
        import_btn.connect("clicked", on_import)
        close_btn.connect("clicked", cancel)

        go_to("pick")
        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def _vbox(*, margin: int = 0, spacing: int = 6) -> Gtk.Box:
    return Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=spacing,
        margin_top=margin, margin_bottom=margin,
        margin_start=margin, margin_end=margin,
    )


def _hbox(*, spacing: int = 6) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)


def _render_preview(grid: Gtk.Grid, preview) -> None:
    """Lay out the §gaps §17 preview fields into a 2-column grid."""
    child = grid.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        grid.remove(child)
        child = nxt

    rows = [
        ("Vault fingerprint",
         f"{preview.bundle_genesis_fingerprint or '(unknown)'} — {_fp_status_text(preview.fingerprint_status)}"),
        ("Source", preview.source_label),
        ("Vault size",
         f"{_format_bytes(preview.bundle_logical_size)} / "
         f"{_format_bytes(preview.bundle_ciphertext_size)} ciphertext"),
        ("Remote folders",
         f"{len(preview.folders)} ({', '.join(f.display_name for f in preview.folders[:10])}"
         + (f", … +{len(preview.folders) - 10} more" if len(preview.folders) > 10 else "")
         + ")"),
        ("History",
         f"{preview.current_files} current / {preview.total_versions} versions / {preview.tombstones} tombstones"),
        ("Conflicts with active vault", str(preview.conflicts)),
        ("Head impact",
         "Will change current head: yes" if preview.will_change_head else "Will change current head: no"),
        ("Bandwidth preview",
         f"{preview.chunks_already_on_relay} of {preview.chunks_total} chunks already on this relay (will skip)"),
    ]
    for row_index, (label, value) in enumerate(rows):
        key = Gtk.Label(label=label, xalign=0, css_classes=["dim-label"])
        val = Gtk.Label(label=value, xalign=0, wrap=True)
        val.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        grid.attach(key, 0, row_index, 1, 1)
        grid.attach(val, 1, row_index, 1, 1)


def _fp_status_text(status: str) -> str:
    return {
        "new_vault": "no active vault on this device",
        "matches_active": "matches active vault",
        "different_vault": "different vault",
    }.get(status, status)


def _format_bytes(value: int) -> str:
    size = max(0, int(value))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.1f} {unit}"
