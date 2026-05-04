"""GTK builder for the Vault browser window (T5.1)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .vault_browser_model import list_folder, list_versions
from .vault_cache import VaultLocalIndex
from .vault_download import previous_version_filename
from .vault_relay_errors import VaultQuotaExceededError, VaultRelayError
from .vault_upload import (
    default_upload_resume_dir,
    describe_quota_exceeded,
    list_resumable_sessions,
)
from .vault_runtime import create_vault_relay, open_local_vault_from_grant
from .windows_common import _make_app


def show_vault_browser(config_dir: Path) -> None:
    """Show the read-only Vault browser shell.

    T5.1 wires browse navigation and manifest rendering. T5.3 wires
    single-file download. Upload, delete, and version actions are
    present in the toolbar but remain disabled until their owning
    T6/T7/T5.5 subtasks land.
    """
    from .config import Config

    config = Config(config_dir)
    local_index = VaultLocalIndex(config_dir)
    app = _make_app()

    state = {
        "manifest": None,
        "path": "",
        "back": [],
        "forward": [],
        "selected_file": None,
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
            title="Vault",
            default_width=1040,
            default_height=680,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        toolbar.set_content(outer)

        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.append(action_bar)

        back_btn = Gtk.Button(label="Back", css_classes=["pill"])
        forward_btn = Gtk.Button(label="Forward", css_classes=["pill"])
        refresh_btn = Gtk.Button(label="Refresh", css_classes=["pill"])
        upload_btn = Gtk.Button(label="Upload", css_classes=["pill"])
        upload_folder_btn = Gtk.Button(label="Upload folder", css_classes=["pill"])
        delete_btn = Gtk.Button(label="Delete", css_classes=["pill", "destructive-action"])
        versions_btn = Gtk.Button(label="Versions", css_classes=["pill"])
        download_btn = Gtk.Button(label="Download", css_classes=["pill", "suggested-action"])
        for button in (
            back_btn,
            forward_btn,
            refresh_btn,
            upload_btn,
            upload_folder_btn,
            delete_btn,
            versions_btn,
            download_btn,
        ):
            action_bar.append(button)
        upload_btn.set_sensitive(False)
        upload_folder_btn.set_sensitive(False)
        delete_btn.set_sensitive(False)
        versions_btn.set_sensitive(False)
        download_btn.set_sensitive(False)
        upload_btn.set_tooltip_text("Open a remote folder, then click Upload to add a file")
        upload_folder_btn.set_tooltip_text("Open a remote folder, then click to upload a local folder recursively")
        delete_btn.set_tooltip_text("Soft-delete the selected file or current folder")
        show_deleted_toggle = Gtk.CheckButton(label="Show deleted")
        show_deleted_toggle.set_tooltip_text(
            "Reveal soft-deleted files; they stay until eviction or retention claims them."
        )
        action_bar.append(show_deleted_toggle)
        versions_btn.set_tooltip_text("Choose a version below to download")
        download_btn.set_tooltip_text("Download selected file or current folder")

        resume_banner = Adw.Banner.new("")
        resume_banner.set_button_label("Resume")
        resume_banner.set_revealed(False)
        outer.append(resume_banner)

        quota_banner = Adw.Banner.new("")
        quota_banner.set_revealed(False)
        outer.append(quota_banner)

        breadcrumb = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        breadcrumb.add_css_class("title-4")
        outer.append(breadcrumb)

        status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        outer.append(status)

        progress_bar = Gtk.ProgressBar(show_text=True)
        progress_bar.set_visible(False)
        outer.append(progress_bar)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        outer.append(paned)

        tree_scroller = Gtk.ScrolledWindow(min_content_width=220)
        tree_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        tree_scroller.set_child(tree_box)
        paned.set_start_child(tree_scroller)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)

        right = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_end_child(right)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(False)
        paned.set_position(220)

        list_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        list_grid = Gtk.Grid(
            column_spacing=18,
            row_spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
            hexpand=True,
            vexpand=True,
        )
        list_scroller.set_child(list_grid)
        right.set_start_child(list_scroller)
        right.set_resize_start_child(True)
        right.set_shrink_start_child(False)

        detail_scroller = Gtk.ScrolledWindow(min_content_width=280)
        detail_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=8,
        )
        detail_scroller.set_child(detail_box)
        right.set_end_child(detail_scroller)
        right.set_resize_end_child(False)
        right.set_shrink_end_child(False)
        right.set_position(540)

        def set_status(message: str, css_class: str = "dim-label") -> None:
            for klass in ("dim-label", "error", "success"):
                status.remove_css_class(klass)
            status.add_css_class(css_class)
            status.set_label(message)

        def clear_box(box: Gtk.Box) -> None:
            child = box.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                box.remove(child)
                child = next_child

        def clear_grid(grid: Gtk.Grid) -> None:
            child = grid.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                grid.remove(child)
                child = next_child

        def current_path_label() -> str:
            path = str(state["path"])
            return "Vault" if not path else "Vault / " + path.replace("/", " / ")

        def update_nav_buttons() -> None:
            back_btn.set_sensitive(bool(state["back"]))
            forward_btn.set_sensitive(bool(state["forward"]))

        def render_detail(file_row: dict | None) -> None:
            clear_box(detail_box)
            versions_btn.set_sensitive(False)
            detail_box.append(Gtk.Label(label="Details", xalign=0, css_classes=["title-3"]))
            if not file_row:
                current_path = str(state["path"])
                download_btn.set_sensitive(bool(current_path))
                if current_path:
                    detail_box.append(Gtk.Label(
                        label="Current folder",
                        xalign=0,
                        wrap=True,
                        css_classes=["dim-label"],
                    ))
                    detail_box.append(Gtk.Label(
                        label=current_path,
                        xalign=0,
                        wrap=True,
                    ))
                    detail_box.append(Gtk.Label(
                        label="Download saves this folder recursively.",
                        xalign=0,
                        wrap=True,
                        css_classes=["dim-label"],
                    ))
                    return
                detail_box.append(Gtk.Label(
                    label="No file selected.",
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                ))
                return
            download_btn.set_sensitive(True)

            rows = [
                ("Name", str(file_row.get("name", ""))),
                ("Path", str(file_row.get("path", ""))),
                ("Logical size", _format_bytes(int(file_row.get("size", 0)))),
                ("Remote stored size", _format_bytes(int(file_row.get("stored_size", 0)))),
                ("Modified", str(file_row.get("modified", "")) or "-"),
                ("Current version", str(file_row.get("latest_version_id", "")) or "-"),
                ("Versions", str(file_row.get("versions", 0))),
                ("Status", str(file_row.get("status", ""))),
            ]
            grid = Gtk.Grid(column_spacing=12, row_spacing=6)
            detail_box.append(grid)
            for row_index, (label, value) in enumerate(rows):
                key = Gtk.Label(label=label, xalign=0, css_classes=["dim-label"])
                val = Gtk.Label(label=value, xalign=0, wrap=True)
                val.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                grid.attach(key, 0, row_index, 1, 1)
                grid.attach(val, 1, row_index, 1, 1)

            render_versions_section(file_row)

        def render_versions_section(file_row: dict) -> None:
            manifest = state["manifest"]
            if not manifest:
                return
            try:
                versions = list_versions(
                    manifest,
                    str(file_row.get("path", "")),
                    include_deleted=bool(file_row.get("deleted")),
                )
            except Exception:
                versions = []

            detail_box.append(Gtk.Label(
                label="Versions",
                xalign=0,
                css_classes=["title-3"],
            ))
            if bool(file_row.get("deleted")):
                tombstone_label = Gtk.Label(
                    label=(
                        f"Deleted {file_row.get('deleted_at') or ''}".strip()
                        + (
                            f" — recoverable until {file_row.get('recoverable_until')}"
                            if file_row.get("recoverable_until") else ""
                        )
                    ),
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                )
                detail_box.append(tombstone_label)
            if not versions:
                detail_box.append(Gtk.Label(
                    label="No version history yet.",
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                ))
                return

            versions_btn.set_sensitive(True)

            grid = Gtk.Grid(column_spacing=12, row_spacing=6)
            detail_box.append(grid)
            for col, header in enumerate(
                ("Modified", "Device", "Size", "Status", "", "")
            ):
                grid.attach(
                    Gtk.Label(label=header, xalign=0, css_classes=["dim-label"]),
                    col, 0, 1, 1,
                )

            entry_deleted = bool(file_row.get("deleted"))
            for row_index, version in enumerate(versions, start=1):
                modified = str(version.get("modified") or "-")
                grid.attach(Gtk.Label(label=modified, xalign=0), 0, row_index, 1, 1)
                device = str(version.get("author_device_id") or "")
                grid.attach(
                    Gtk.Label(label=device[:12] if device else "-", xalign=0),
                    1, row_index, 1, 1,
                )
                size_label = _format_bytes(int(version.get("size", 0) or 0))
                grid.attach(Gtk.Label(label=size_label, xalign=0), 2, row_index, 1, 1)
                if entry_deleted and version.get("is_current"):
                    status_label = "Latest (deleted)"
                elif version.get("is_current"):
                    status_label = "Current"
                else:
                    status_label = "Previous"
                grid.attach(Gtk.Label(label=status_label, xalign=0), 3, row_index, 1, 1)

                download_btn_inline = Gtk.Button(label="Download…", css_classes=["pill"])
                download_btn_inline.set_tooltip_text(
                    "Save this version to a side path — the current file is never overwritten."
                )
                download_btn_inline.connect(
                    "clicked",
                    lambda _b, v=dict(version), f=dict(file_row): choose_version_destination(f, v),
                )
                if version.get("is_current") and not entry_deleted:
                    download_btn_inline.set_sensitive(False)
                grid.attach(download_btn_inline, 4, row_index, 1, 1)

                # Restore makes sense for any non-current version, plus
                # the latest version of a tombstoned entry (T7.4 shortcut).
                show_restore = (not version.get("is_current")) or entry_deleted
                if show_restore:
                    restore_btn = Gtk.Button(
                        label="Restore as current",
                        css_classes=["pill", "suggested-action"],
                    )
                    restore_btn.set_tooltip_text(
                        "Promote this version to the current one. Tombstone is lifted."
                        if entry_deleted else
                        "Promote this version to the current one. The previous "
                        "current becomes restorable history."
                    )
                    restore_btn.connect(
                        "clicked",
                        lambda _b, v=dict(version), f=dict(file_row): _confirm_restore_version(f, v),
                    )
                    grid.attach(restore_btn, 5, row_index, 1, 1)

        def select_file(file_row: dict) -> None:
            state["selected_file"] = file_row
            render_detail(file_row)

        def attach_cell(widget: Gtk.Widget, col: int, row: int) -> None:
            list_grid.attach(widget, col, row, 1, 1)

        def attach_label(text: str, col: int, row: int, *, header: bool = False) -> None:
            label = Gtk.Label(label=text, xalign=0, hexpand=(col == 0))
            label.set_wrap(True)
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if header:
                label.add_css_class("dim-label")
            attach_cell(label, col, row)

        def render_file_list() -> None:
            clear_grid(list_grid)
            for col, title in enumerate(("Name", "Size", "Modified", "Versions", "Status")):
                attach_label(title, col, 0, header=True)

            manifest = state["manifest"]
            if not manifest:
                attach_label("Open or refresh a vault to browse files.", 0, 1)
                return

            include_deleted = bool(state.get("show_deleted"))
            try:
                folders, files = list_folder(
                    manifest, str(state["path"]), include_deleted=include_deleted,
                )
            except Exception as exc:
                attach_label(f"Could not list this folder: {exc}", 0, 1)
                return

            row = 1
            for folder in folders:
                button = Gtk.Button(label=str(folder["name"]), halign=Gtk.Align.START)
                button.add_css_class("flat")
                button.connect("clicked", lambda _btn, p=folder["path"]: navigate_to(str(p)))
                attach_cell(button, 0, row)
                attach_label("-", 1, row)
                attach_label("-", 2, row)
                attach_label("-", 3, row)
                attach_label("Folder", 4, row)
                row += 1

            for file_row in files:
                deleted = str(file_row.get("status", "")) == "Deleted"
                button = Gtk.Button(label=str(file_row["name"]), halign=Gtk.Align.START)
                button.add_css_class("flat")
                if deleted:
                    button.add_css_class("dim-label")
                button.connect("clicked", lambda _btn, f=file_row: select_file(dict(f)))
                attach_cell(button, 0, row)
                size_label = _format_bytes(int(file_row.get("size", 0)))
                attach_label(size_label, 1, row)
                attach_label(str(file_row.get("modified", "")) or "-", 2, row)
                attach_label(str(file_row.get("versions", 0)), 3, row)
                status_label = str(file_row.get("status", ""))
                if deleted:
                    recoverable = str(file_row.get("recoverable_until") or "").strip()
                    if recoverable:
                        status_label = f"Deleted — recoverable until {recoverable}"
                attach_label(status_label, 4, row)
                row += 1

            if row == 1:
                if state["path"]:
                    attach_label("Folder is empty — drag files here or click Upload", 0, 1)
                else:
                    attach_label("No remote folders yet.", 0, 1)

        def render_tree() -> None:
            clear_box(tree_box)
            root = Gtk.Button(label="Vault", halign=Gtk.Align.START)
            root.add_css_class("flat")
            root.connect("clicked", lambda _btn: navigate_to(""))
            tree_box.append(root)

            manifest = state["manifest"]
            if not manifest:
                return

            def add_path_button(path: str, depth: int) -> None:
                name = path.split("/")[-1] if path else "Vault"
                button = Gtk.Button(label=("  " * depth) + name, halign=Gtk.Align.START)
                button.add_css_class("flat")
                button.connect("clicked", lambda _btn, p=path: navigate_to(p))
                tree_box.append(button)

            def walk(path: str, depth: int) -> None:
                try:
                    children, _files = list_folder(manifest, path)
                except Exception:
                    return
                for child in children:
                    child_path = str(child["path"])
                    add_path_button(child_path, depth)
                    walk(child_path, depth + 1)

            walk("", 1)

        def render_all(message: str | None = None, css_class: str = "dim-label") -> None:
            breadcrumb.set_label(current_path_label())
            update_nav_buttons()
            render_tree()
            render_file_list()
            render_detail(state.get("selected_file"))
            upload_destination = _resolve_upload_destination()
            upload_btn.set_sensitive(upload_destination is not None)
            upload_folder_btn.set_sensitive(upload_destination is not None)
            # Delete is enabled for a selected (non-deleted) file or for an
            # open remote-folder path (bulk soft-delete of its contents).
            selected_file = state.get("selected_file") or {}
            can_delete_file = (
                bool(selected_file)
                and not bool(selected_file.get("deleted"))
            )
            can_delete_folder = upload_destination is not None
            delete_btn.set_sensitive(can_delete_file or can_delete_folder)
            if message is not None:
                set_status(message, css_class)

        def _resolve_upload_destination() -> tuple[str, str] | None:
            """Return (remote_folder_id, sub_path) for the current location, or None."""
            manifest = state.get("manifest")
            path = str(state.get("path") or "")
            if not manifest or not path:
                return None
            parts = [p for p in path.split("/") if p]
            if not parts:
                return None
            head, *rest = parts
            for folder in manifest.get("remote_folders", []) or []:
                if not isinstance(folder, dict):
                    continue
                if str(folder.get("state", "active")) != "active":
                    continue
                if str(folder.get("display_name_enc", "")) == head:
                    return str(folder["remote_folder_id"]), "/".join(rest)
            return None

        def navigate_to(path: str, *, record: bool = True) -> None:
            new_path = str(path)
            if new_path == state["path"]:
                render_all()
                return
            if record:
                state["back"].append(state["path"])
                state["forward"] = []
            state["path"] = new_path
            state["selected_file"] = None
            render_all()

        def go_back(_btn) -> None:
            if not state["back"]:
                return
            state["forward"].append(state["path"])
            previous = state["back"].pop()
            navigate_to(str(previous), record=False)

        def go_forward(_btn) -> None:
            if not state["forward"]:
                return
            state["back"].append(state["path"])
            next_path = state["forward"].pop()
            navigate_to(str(next_path), record=False)

        def refresh_manifest_async(_btn=None) -> None:
            vault_id = local_vault_id()
            if not vault_id:
                state["manifest"] = None
                state["path"] = ""
                state["back"] = []
                state["forward"] = []
                state["selected_file"] = None
                render_all("No local vault is connected.", "error")
                return

            refresh_btn.set_sensitive(False)
            set_status("Refreshing vault manifest...")

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
                    error_message = str(exc)

                    def fail() -> bool:
                        refresh_btn.set_sensitive(True)
                        render_all(f"Could not refresh vault browser: {error_message}", "error")
                        return False

                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    refresh_btn.set_sensitive(True)
                    state["manifest"] = manifest
                    try:
                        list_folder(manifest, str(state["path"]))
                    except Exception:
                        state["path"] = ""
                        state["back"] = []
                        state["forward"] = []
                    state["selected_file"] = None
                    render_all("Vault browser refreshed.", "success")
                    _refresh_resume_banner(vault_id)
                    return False

                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        def _handle_quota_exceeded(exc: VaultQuotaExceededError, *, action: str) -> None:
            """T6.6 + T7.5: route a 507 into either the eviction prompt or the
            vault-full banner depending on ``eviction_available``."""
            info = describe_quota_exceeded(exc)
            if info["eviction_available"]:
                quota_banner.set_revealed(False)
                dlg = Adw.AlertDialog(
                    heading=info["heading"],
                    body=info["body"],
                )
                dlg.add_response("cancel", "Cancel")
                dlg.add_response("evict", info["primary_action_label"])
                dlg.set_default_response("evict")
                dlg.set_close_response("cancel")
                dlg.set_response_appearance("evict", Adw.ResponseAppearance.SUGGESTED)

                def on_response(_dialog, response: str) -> None:
                    if response == "evict":
                        delta = max(1, exc.used_bytes - exc.quota_bytes + 1)
                        _run_eviction_pass(action=action, target_bytes=delta)
                    else:
                        set_status(
                            f"{action} paused — vault is full ({info['percent']}%).",
                            "error",
                        )
                dlg.connect("response", on_response)
                dlg.present(win)
                return

            # No history left → terminal sync-stop banner per §D2 step 4.
            quota_banner.set_title(info["body"])
            quota_banner.set_button_label(info["primary_action_label"])
            quota_banner.set_revealed(True)
            set_status(
                f"{action} stopped: vault full and no backup history remains.",
                "error",
            )

        def _run_eviction_pass(*, action: str, target_bytes: int) -> None:
            """T7.5: run the §D2 eviction pipeline in a worker thread."""
            vault_id = local_vault_id()
            if not vault_id:
                set_status("No local vault is connected.", "error")
                return

            refresh_btn.set_sensitive(False)
            progress_bar.set_visible(True)
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("Reclaiming space...")
            set_status(f"{action}: running eviction to free {target_bytes} bytes...")

            def worker() -> None:
                try:
                    from .vault_eviction import eviction_pass

                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        current_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        device_id = str(getattr(config, "device_id", "") or "0" * 32)
                        result = eviction_pass(
                            vault=vault, relay=relay,
                            manifest=current_manifest,
                            author_device_id=device_id,
                            target_bytes_to_free=target_bytes,
                            local_index=local_index,
                        )
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        refresh_btn.set_sensitive(True)
                        set_status(f"Eviction failed: {error_message}", "error")
                        return False
                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    state["manifest"] = result.manifest
                    state["selected_file"] = None
                    progress_bar.set_visible(False)
                    refresh_btn.set_sensitive(True)
                    if result.no_more_candidates:
                        quota_banner.set_title(
                            "Vault is full and no backup history remains. Sync is "
                            "stopped. Free space by deleting files, or export and "
                            "migrate to a relay with more capacity."
                        )
                        quota_banner.set_button_label("Open vault settings")
                        quota_banner.set_revealed(True)
                        render_all(
                            f"Eviction stopped — no more candidates. Freed {result.bytes_freed} bytes.",
                            "error",
                        )
                    else:
                        render_all(
                            f"Eviction freed {result.bytes_freed} bytes "
                            f"({result.chunks_freed} chunks). "
                            f"Try {action.lower()} again.",
                            "success",
                        )
                    return False
                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        def _refresh_resume_banner(vault_id: str) -> None:
            try:
                sessions = list_resumable_sessions(vault_id, default_upload_resume_dir())
            except Exception:
                sessions = []
            state["resume_sessions"] = sessions
            if not sessions:
                resume_banner.set_revealed(False)
                return
            count = len(sessions)
            label = (
                "1 upload was interrupted — click Resume to finish it."
                if count == 1
                else f"{count} uploads were interrupted — click Resume to finish them."
            )
            resume_banner.set_title(label)
            resume_banner.set_revealed(True)

        def start_resume_pending(_btn=None) -> None:
            sessions = list(state.get("resume_sessions") or [])
            if not sessions:
                return
            vault_id = local_vault_id()
            if not vault_id:
                set_status("No local vault is connected.", "error")
                return

            refresh_btn.set_sensitive(False)
            upload_btn.set_sensitive(False)
            upload_folder_btn.set_sensitive(False)
            resume_banner.set_revealed(False)
            progress_bar.set_visible(True)
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("Resuming uploads...")
            set_status(f"Resuming {len(sessions)} interrupted upload(s)...")

            def worker() -> None:
                from .vault_upload import resume_upload

                completed = 0
                failed = 0
                last_manifest = state.get("manifest")
                try:
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        for session in sessions:
                            try:
                                current_manifest = vault.fetch_manifest(relay, local_index=local_index)
                                result = resume_upload(
                                    vault=vault,
                                    relay=relay,
                                    manifest=current_manifest,
                                    session=session,
                                    local_index=local_index,
                                )
                                last_manifest = result.manifest
                                completed += 1
                            except Exception:
                                failed += 1
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        refresh_btn.set_sensitive(True)
                        upload_btn.set_sensitive(_resolve_upload_destination() is not None)
                        upload_folder_btn.set_sensitive(_resolve_upload_destination() is not None)
                        set_status(f"Resume failed: {error_message}", "error")
                        _refresh_resume_banner(vault_id)
                        return False
                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    progress_bar.set_visible(False)
                    refresh_btn.set_sensitive(True)
                    upload_btn.set_sensitive(_resolve_upload_destination() is not None)
                    upload_folder_btn.set_sensitive(_resolve_upload_destination() is not None)
                    if last_manifest is not None:
                        state["manifest"] = last_manifest
                    state["selected_file"] = None
                    render_all()
                    if failed == 0:
                        set_status(
                            f"Resumed {completed} upload(s).", "success",
                        )
                    else:
                        set_status(
                            f"Resumed {completed} upload(s); {failed} failed (will retry next time).",
                            "error",
                        )
                    _refresh_resume_banner(vault_id)
                    return False
                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        resume_banner.connect("button-clicked", start_resume_pending)

        def start_download(destination: Path, existing_policy: str) -> None:
            file_row = state.get("selected_file")
            folder_path = str(state["path"])
            is_folder_download = file_row is None
            if is_folder_download and not folder_path:
                set_status("Open a remote folder before downloading a folder.", "error")
                return
            vault_id = local_vault_id()
            if not vault_id:
                set_status("No local vault is connected.", "error")
                return

            selected_path = folder_path if is_folder_download else str(file_row.get("path", ""))
            download_label = "folder" if is_folder_download else selected_path
            download_btn.set_sensitive(False)
            progress_bar.set_visible(True)
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("Preparing download...")
            set_status(f"Downloading {download_label}...")

            def report_progress(progress) -> None:
                def update_progress() -> bool:
                    total = max(1, int(progress.total_chunks))
                    fraction = 1.0 if progress.phase == "done" else progress.completed_chunks / total
                    progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                    progress_bar.set_text(
                        f"{progress.completed_chunks}/{progress.total_chunks} chunks"
                    )
                    return False

                GLib.idle_add(update_progress)

            def worker() -> None:
                try:
                    from .vault_download import (
                        default_vault_download_cache_dir,
                        download_folder,
                        download_latest_file,
                    )

                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        current_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        if is_folder_download:
                            final_path = download_folder(
                                vault=vault,
                                relay=relay,
                                manifest=current_manifest,
                                path=selected_path,
                                destination=destination,
                                existing_policy=existing_policy,
                                chunk_cache_dir=default_vault_download_cache_dir(),
                                progress=report_progress,
                            )
                        else:
                            final_path = download_latest_file(
                                vault=vault,
                                relay=relay,
                                manifest=current_manifest,
                                path=selected_path,
                                destination=destination,
                                existing_policy=existing_policy,
                                chunk_cache_dir=default_vault_download_cache_dir(),
                                progress=report_progress,
                            )
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        download_btn.set_sensitive(bool(state.get("selected_file")) or bool(state["path"]))
                        set_status(f"Download failed: {error_message}", "error")
                        return False

                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    state["manifest"] = current_manifest
                    progress_bar.set_visible(False)
                    download_btn.set_sensitive(bool(state.get("selected_file")) or bool(state["path"]))
                    noun = "folder" if is_folder_download else "file"
                    set_status(f"Downloaded {noun} to {final_path}.", "success")
                    return False

                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        def prompt_existing_destination(destination: Path, *, is_folder: bool = False) -> None:
            dlg = Adw.AlertDialog(
                heading="Folder exists" if is_folder else "File exists",
                body=(
                    "A folder with this name already exists. Overwrite replaces matching "
                    "files but keeps unrelated local files."
                    if is_folder
                    else "Choose how to handle the selected destination."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("keep_both", "Keep both")
            dlg.add_response("overwrite", "Overwrite matching files" if is_folder else "Overwrite")
            dlg.set_default_response("keep_both")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)

            def on_response(_dialog, response: str) -> None:
                if response == "overwrite":
                    start_download(destination, "overwrite")
                elif response == "keep_both":
                    start_download(destination, "keep_both")

            dlg.connect("response", on_response)
            dlg.present(win)

        def start_version_download(
            file_row: dict,
            version: dict,
            destination: Path,
            existing_policy: str,
        ) -> None:
            vault_id = local_vault_id()
            if not vault_id:
                set_status("No local vault is connected.", "error")
                return

            file_path = str(file_row.get("path") or "")
            version_id = str(version.get("version_id") or "")
            if not file_path or not version_id:
                set_status("Cannot download this version.", "error")
                return

            label = file_row.get("name") or file_path
            modified = str(version.get("modified") or "?")
            download_btn.set_sensitive(False)
            versions_btn.set_sensitive(False)
            progress_bar.set_visible(True)
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("Preparing version download...")
            set_status(f"Downloading {label} (version {modified})...")

            def report_progress(progress) -> None:
                def update_progress() -> bool:
                    total = max(1, int(progress.total_chunks))
                    fraction = 1.0 if progress.phase == "done" else progress.completed_chunks / total
                    progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                    progress_bar.set_text(
                        f"{progress.completed_chunks}/{progress.total_chunks} chunks"
                    )
                    return False

                GLib.idle_add(update_progress)

            def worker() -> None:
                try:
                    from .vault_download import (
                        default_vault_download_cache_dir,
                        download_version,
                    )

                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        current_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        final_path = download_version(
                            vault=vault,
                            relay=relay,
                            manifest=current_manifest,
                            path=file_path,
                            version_id=version_id,
                            destination=destination,
                            existing_policy=existing_policy,
                            chunk_cache_dir=default_vault_download_cache_dir(),
                            progress=report_progress,
                        )
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        download_btn.set_sensitive(
                            bool(state.get("selected_file")) or bool(state["path"])
                        )
                        versions_btn.set_sensitive(bool(state.get("selected_file")))
                        set_status(f"Version download failed: {error_message}", "error")
                        return False

                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    state["manifest"] = current_manifest
                    progress_bar.set_visible(False)
                    download_btn.set_sensitive(
                        bool(state.get("selected_file")) or bool(state["path"])
                    )
                    versions_btn.set_sensitive(bool(state.get("selected_file")))
                    set_status(f"Downloaded version to {final_path}.", "success")
                    return False

                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        def prompt_existing_version_destination(
            file_row: dict,
            version: dict,
            destination: Path,
        ) -> None:
            dlg = Adw.AlertDialog(
                heading="Version file exists",
                body=(
                    "A file with this version's side-path name already exists. "
                    "Choose how to handle it — the current file is never overwritten."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("keep_both", "Keep both")
            dlg.add_response("overwrite", "Overwrite")
            dlg.set_default_response("keep_both")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)

            def on_response(_dialog, response: str) -> None:
                if response == "overwrite":
                    start_version_download(file_row, version, destination, "overwrite")
                elif response == "keep_both":
                    start_version_download(file_row, version, destination, "keep_both")

            dlg.connect("response", on_response)
            dlg.present(win)

        def choose_version_destination(file_row: dict, version: dict) -> None:
            base_name = str(file_row.get("name") or "vault-download")
            initial_name = previous_version_filename(base_name, version)

            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Download previous version")
            file_dialog.set_initial_name(initial_name)

            def on_destination_chosen(file_dialog, result) -> None:
                try:
                    gio_file = file_dialog.save_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path = gio_file.get_path()
                if not path:
                    set_status("Choose a local file destination.", "error")
                    return
                destination = Path(path)
                if destination.exists():
                    prompt_existing_version_destination(file_row, version, destination)
                else:
                    start_version_download(file_row, version, destination, "fail")

            file_dialog.save(parent=win, callback=on_destination_chosen)

        def choose_download_destination(_btn) -> None:
            file_row = state.get("selected_file")
            if not file_row and not state["path"]:
                set_status("Open a remote folder before downloading a folder.", "error")
                return
            if not file_row:
                file_dialog = Gtk.FileDialog()
                file_dialog.set_title("Download folder")

                def on_folder_chosen(file_dialog, result) -> None:
                    try:
                        gio_file = file_dialog.select_folder_finish(result)
                    except GLib.Error:
                        return
                    if gio_file is None:
                        return
                    path = gio_file.get_path()
                    if not path:
                        set_status("Choose a local folder destination.", "error")
                        return
                    destination = Path(path) / _download_folder_name(str(state["path"]))
                    if destination.exists():
                        prompt_existing_destination(destination, is_folder=True)
                    else:
                        start_download(destination, "fail")

                file_dialog.select_folder(parent=win, callback=on_folder_chosen)
                return

            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Download file")
            file_dialog.set_initial_name(str(file_row.get("name") or "vault-download"))

            def on_destination_chosen(file_dialog, result) -> None:
                try:
                    gio_file = file_dialog.save_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path = gio_file.get_path()
                if not path:
                    set_status("Choose a local file destination.", "error")
                    return
                destination = Path(path)
                if destination.exists():
                    prompt_existing_destination(destination)
                else:
                    start_download(destination, "fail")

            file_dialog.save(parent=win, callback=on_destination_chosen)

        def start_upload(
            local_path: Path,
            remote_folder_id: str,
            sub_path: str,
            *,
            override_remote_path: str | None = None,
            upload_mode: str = "new_file_or_version",
        ) -> None:
            vault_id = local_vault_id()
            if not vault_id:
                set_status("No local vault is connected.", "error")
                return

            remote_path = override_remote_path or (
                sub_path + "/" + local_path.name if sub_path else local_path.name
            )
            upload_btn.set_sensitive(False)
            refresh_btn.set_sensitive(False)
            progress_bar.set_visible(True)
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("Preparing upload...")
            set_status(f"Uploading {local_path.name}...")

            def report_progress(progress) -> None:
                def update() -> bool:
                    total = max(1, int(progress.total_chunks))
                    fraction = 1.0 if progress.phase == "done" else progress.completed_chunks / total
                    progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                    progress_bar.set_text(
                        f"{progress.completed_chunks}/{progress.total_chunks} chunks"
                    )
                    return False
                GLib.idle_add(update)

            def worker() -> None:
                try:
                    from .vault_upload import upload_file

                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        current_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        device_id = str(getattr(config, "device_id", "") or "0" * 32)
                        result = upload_file(
                            vault=vault,
                            relay=relay,
                            manifest=current_manifest,
                            local_path=local_path,
                            remote_folder_id=remote_folder_id,
                            remote_path=remote_path,
                            author_device_id=device_id,
                            mode=upload_mode,
                            progress=report_progress,
                            local_index=local_index,
                        )
                    finally:
                        vault.close()
                except VaultQuotaExceededError as exc:
                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        upload_btn.set_sensitive(_resolve_upload_destination() is not None)
                        refresh_btn.set_sensitive(True)
                        _handle_quota_exceeded(exc, action="Upload")
                        return False
                    GLib.idle_add(fail)
                    return
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        upload_btn.set_sensitive(_resolve_upload_destination() is not None)
                        refresh_btn.set_sensitive(True)
                        set_status(f"Upload failed: {error_message}", "error")
                        return False
                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    state["manifest"] = result.manifest
                    progress_bar.set_visible(False)
                    refresh_btn.set_sensitive(True)
                    state["selected_file"] = None
                    render_all()
                    if result.skipped_identical:
                        set_status(
                            f"{remote_path} already has identical content — no upload needed.",
                            "success",
                        )
                    else:
                        set_status(
                            f"Uploaded {result.chunks_uploaded} chunks "
                            f"({result.bytes_uploaded} bytes) to {remote_path}.",
                            "success",
                        )
                    return False
                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        def choose_upload_source(_btn) -> None:
            destination = _resolve_upload_destination()
            if destination is None:
                set_status("Open a remote folder before uploading.", "error")
                return
            remote_folder_id, sub_path = destination

            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Upload file to vault")

            def on_source_chosen(file_dialog, result) -> None:
                try:
                    gio_file = file_dialog.open_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path = gio_file.get_path()
                if not path:
                    set_status("Choose a local file to upload.", "error")
                    return
                local_path = Path(path)
                if not local_path.is_file():
                    set_status("Selected entry is not a file.", "error")
                    return
                _maybe_prompt_conflict_then_upload(local_path, remote_folder_id, sub_path)

            file_dialog.open(parent=win, callback=on_source_chosen)

        def _maybe_prompt_conflict_then_upload(
            local_path: Path,
            remote_folder_id: str,
            sub_path: str,
        ) -> None:
            from .vault_upload import detect_path_conflict, make_conflict_renamed_path

            remote_path = (
                sub_path + "/" + local_path.name if sub_path else local_path.name
            )
            manifest = state.get("manifest")
            if manifest is None or not detect_path_conflict(
                manifest, remote_folder_id, remote_path
            ):
                start_upload(local_path, remote_folder_id, sub_path)
                return

            dlg = Adw.AlertDialog(
                heading=f"{remote_path} already exists",
                body=(
                    "A file with this name is already in the remote folder. "
                    "Choose what to do — identical content is detected automatically "
                    "and skipped, so this prompt only appears for new bytes."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("skip", "Skip")
            dlg.add_response("keep_both", "Keep both with rename")
            dlg.add_response("new_version", "Add as new version")
            dlg.set_default_response("new_version")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance("new_version", Adw.ResponseAppearance.SUGGESTED)

            def on_response(_dialog, response: str) -> None:
                if response == "new_version":
                    start_upload(local_path, remote_folder_id, sub_path)
                elif response == "keep_both":
                    config.reload()
                    device_name = str(getattr(config, "device_name", "") or "device")
                    renamed = make_conflict_renamed_path(remote_path, device_name)
                    new_sub_parts = [p for p in renamed.split("/") if p][:-1]
                    new_sub_path = "/".join(new_sub_parts)
                    start_upload(
                        local_path,
                        remote_folder_id,
                        new_sub_path,
                        override_remote_path=renamed,
                        upload_mode="new_file_only",
                    )
                elif response == "skip":
                    set_status(f"Skipped uploading {local_path.name}.", "dim-label")
                # "cancel" → fall through and do nothing.

            dlg.connect("response", on_response)
            dlg.present(win)

        def start_folder_upload(local_root: Path, remote_folder_id: str, sub_path: str) -> None:
            vault_id = local_vault_id()
            if not vault_id:
                set_status("No local vault is connected.", "error")
                return

            upload_btn.set_sensitive(False)
            upload_folder_btn.set_sensitive(False)
            refresh_btn.set_sensitive(False)
            progress_bar.set_visible(True)
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("Walking folder...")
            set_status(f"Uploading folder {local_root.name}...")

            def report_progress(folder_progress) -> None:
                def update() -> bool:
                    if folder_progress.bytes_total > 0:
                        fraction = folder_progress.bytes_completed / folder_progress.bytes_total
                    elif folder_progress.files_total > 0:
                        fraction = folder_progress.files_completed / max(1, folder_progress.files_total)
                    else:
                        fraction = 1.0
                    progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                    progress_bar.set_text(
                        f"{folder_progress.phase}: "
                        f"{folder_progress.files_completed}/{folder_progress.files_total} files"
                    )
                    return False
                GLib.idle_add(update)

            def worker() -> None:
                try:
                    from .vault_upload import upload_folder

                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        current_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        device_id = str(getattr(config, "device_id", "") or "0" * 32)
                        result = upload_folder(
                            vault=vault,
                            relay=relay,
                            manifest=current_manifest,
                            local_root=local_root,
                            remote_folder_id=remote_folder_id,
                            remote_sub_path=sub_path,
                            author_device_id=device_id,
                            progress=report_progress,
                            local_index=local_index,
                        )
                    finally:
                        vault.close()
                except VaultQuotaExceededError as exc:
                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        upload_btn.set_sensitive(_resolve_upload_destination() is not None)
                        upload_folder_btn.set_sensitive(_resolve_upload_destination() is not None)
                        refresh_btn.set_sensitive(True)
                        _handle_quota_exceeded(exc, action="Folder upload")
                        return False
                    GLib.idle_add(fail)
                    return
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        upload_btn.set_sensitive(_resolve_upload_destination() is not None)
                        upload_folder_btn.set_sensitive(_resolve_upload_destination() is not None)
                        refresh_btn.set_sensitive(True)
                        set_status(f"Folder upload failed: {error_message}", "error")
                        return False
                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    state["manifest"] = result.manifest
                    progress_bar.set_visible(False)
                    refresh_btn.set_sensitive(True)
                    state["selected_file"] = None
                    render_all()
                    skipped = len(result.skipped)
                    set_status(
                        f"Uploaded {len(result.uploaded)} files "
                        f"({result.bytes_uploaded} bytes); skipped {skipped}.",
                        "success",
                    )
                    return False
                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        def choose_upload_folder_source(_btn) -> None:
            destination = _resolve_upload_destination()
            if destination is None:
                set_status("Open a remote folder before uploading.", "error")
                return
            remote_folder_id, sub_path = destination

            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Upload folder to vault")

            def on_source_chosen(file_dialog, result) -> None:
                try:
                    gio_file = file_dialog.select_folder_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path = gio_file.get_path()
                if not path:
                    set_status("Choose a local folder to upload.", "error")
                    return
                local_root = Path(path)
                if not local_root.is_dir():
                    set_status("Selected entry is not a folder.", "error")
                    return
                start_folder_upload(local_root, remote_folder_id, sub_path)

            file_dialog.select_folder(parent=win, callback=on_source_chosen)

        def _run_delete_worker(
            *,
            label: str,
            mutate: Callable[[dict], dict],
        ) -> None:
            """Execute ``mutate(current_manifest)`` in a worker thread.

            ``mutate`` is expected to fetch the latest manifest, call into
            :mod:`vault_delete`, and return the published manifest.
            Thread-safe UI updates land via ``GLib.idle_add``.
            """
            vault_id = local_vault_id()
            if not vault_id:
                set_status("No local vault is connected.", "error")
                return

            delete_btn.set_sensitive(False)
            refresh_btn.set_sensitive(False)
            progress_bar.set_visible(True)
            progress_bar.set_fraction(0.0)
            progress_bar.set_text(label)
            set_status(f"{label}...")

            def worker() -> None:
                try:
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        current_manifest = vault.fetch_manifest(relay, local_index=local_index)
                        published = mutate({
                            "vault": vault,
                            "relay": relay,
                            "manifest": current_manifest,
                        })
                    finally:
                        vault.close()
                except Exception as exc:
                    error_message = str(exc)

                    def fail() -> bool:
                        progress_bar.set_visible(False)
                        refresh_btn.set_sensitive(True)
                        render_all(f"{label} failed: {error_message}", "error")
                        return False
                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    state["manifest"] = published
                    state["selected_file"] = None
                    progress_bar.set_visible(False)
                    refresh_btn.set_sensitive(True)
                    render_all(f"{label} succeeded.", "success")
                    return False
                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        def _confirm_delete_file(file_row: dict) -> None:
            from .vault_delete import delete_file

            remote_folder_id = str(file_row.get("remote_folder_id") or "")
            relative_path = str(file_row.get("relative_path") or "")
            if not remote_folder_id or not relative_path:
                set_status("Cannot delete: missing folder/path metadata.", "error")
                return

            display_path = str(file_row.get("path") or relative_path)
            dlg = Adw.AlertDialog(
                heading=f"Delete {display_path}?",
                body=(
                    "This removes the file from the current remote view. Previous "
                    "versions are kept for the retention period and can be restored."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("delete", "Delete")
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

            def on_response(_dialog, response: str) -> None:
                if response != "delete":
                    return
                config.reload()
                device_id = str(getattr(config, "device_id", "") or "0" * 32)

                def mutate(ctx: dict) -> dict:
                    return delete_file(
                        vault=ctx["vault"], relay=ctx["relay"],
                        manifest=ctx["manifest"],
                        remote_folder_id=remote_folder_id,
                        remote_path=relative_path,
                        author_device_id=device_id,
                        local_index=local_index,
                    )
                _run_delete_worker(
                    label=f"Deleting {display_path}",
                    mutate=mutate,
                )

            dlg.connect("response", on_response)
            dlg.present(win)

        def _confirm_delete_folder(remote_folder_id: str, sub_path: str) -> None:
            from .vault_delete import delete_folder_contents

            target_label = sub_path or "this remote folder's contents"
            dlg = Adw.AlertDialog(
                heading=f"Delete contents of {target_label}?",
                body=(
                    "Every file under this path becomes a tombstone. Previous "
                    "versions stay until eviction or retention claims them."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("delete", "Delete folder contents")
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

            def on_response(_dialog, response: str) -> None:
                if response != "delete":
                    return
                config.reload()
                device_id = str(getattr(config, "device_id", "") or "0" * 32)

                def mutate(ctx: dict) -> dict:
                    published, _tombstoned = delete_folder_contents(
                        vault=ctx["vault"], relay=ctx["relay"],
                        manifest=ctx["manifest"],
                        remote_folder_id=remote_folder_id,
                        path_prefix=sub_path,
                        author_device_id=device_id,
                        local_index=local_index,
                    )
                    return published
                _run_delete_worker(
                    label=f"Deleting contents of {target_label}",
                    mutate=mutate,
                )

            dlg.connect("response", on_response)
            dlg.present(win)

        def _confirm_restore_version(file_row: dict, version: dict) -> None:
            from .vault_delete import restore_version_to_current

            remote_folder_id = str(file_row.get("remote_folder_id") or "")
            relative_path = str(file_row.get("relative_path") or "")
            source_version_id = str(version.get("version_id") or "")
            if not remote_folder_id or not relative_path or not source_version_id:
                set_status("Cannot restore: missing metadata.", "error")
                return

            display_path = str(file_row.get("path") or relative_path)
            modified = str(version.get("modified") or "?")
            heading = f"Restore {display_path} to {modified}?"
            body = (
                "A new version will be added on top, pointing at this version's "
                "stored chunks. The previous current version stays in history."
            )
            if bool(file_row.get("deleted")):
                body = (
                    "This file is currently deleted. Restoring lifts the tombstone "
                    "and adds a new version on top of the chosen one."
                )
            dlg = Adw.AlertDialog(heading=heading, body=body)
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("restore", "Restore")
            dlg.set_default_response("restore")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)

            def on_response(_dialog, response: str) -> None:
                if response != "restore":
                    return
                config.reload()
                device_id = str(getattr(config, "device_id", "") or "0" * 32)

                def mutate(ctx: dict) -> dict:
                    return restore_version_to_current(
                        vault=ctx["vault"], relay=ctx["relay"],
                        manifest=ctx["manifest"],
                        remote_folder_id=remote_folder_id,
                        remote_path=relative_path,
                        source_version_id=source_version_id,
                        author_device_id=device_id,
                        local_index=local_index,
                    )
                _run_delete_worker(
                    label=f"Restoring {display_path}",
                    mutate=mutate,
                )

            dlg.connect("response", on_response)
            dlg.present(win)

        def on_show_deleted_toggled(_btn) -> None:
            state["show_deleted"] = bool(show_deleted_toggle.get_active())
            state["selected_file"] = None
            render_all()

        def confirm_and_delete(_btn) -> None:
            file_row = state.get("selected_file")
            destination = _resolve_upload_destination()
            if file_row and not bool(file_row.get("deleted")):
                _confirm_delete_file(dict(file_row))
                return
            if destination is None:
                set_status(
                    "Open a remote folder or select a file before deleting.",
                    "error",
                )
                return
            remote_folder_id, sub_path = destination
            _confirm_delete_folder(remote_folder_id, sub_path)

        back_btn.connect("clicked", go_back)
        forward_btn.connect("clicked", go_forward)
        refresh_btn.connect("clicked", refresh_manifest_async)
        upload_btn.connect("clicked", choose_upload_source)
        upload_folder_btn.connect("clicked", choose_upload_folder_source)
        delete_btn.connect("clicked", confirm_and_delete)
        download_btn.connect("clicked", choose_download_destination)
        show_deleted_toggle.connect("toggled", on_show_deleted_toggled)

        render_all("Open or refresh a vault to browse files.")
        refresh_manifest_async()
        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


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


def _download_folder_name(path: str) -> str:
    parts = [
        part for part in str(path).replace("\\", "/").split("/")
        if part and part != "."
    ]
    return parts[-1] if parts else "Vault"
