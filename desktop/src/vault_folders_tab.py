"""GTK builder for the Vault settings Folders tab.

F-518 (refactor): vault-mutation business logic now lives in
:class:`vault_folder_runtime.VaultRuntime`. This module keeps GTK
widget assembly + ``threading.Thread`` spawning + ``GLib.idle_add``
result forwarding. The runtime owns the per-tab serialization lock
(see F-517 for why that exists) and exposes named operations
(``fetch_manifest``, ``add_remote_folder``, ``rename_remote_folder``,
``flush_and_sync_binding``, ``run_initial_baseline``) so worker
threads don't reach into ``Vault.*`` directly.

F-LT09: master/detail redesign — replaced the old single-grid +
shared bindings-list layout with an :class:`Adw.NavigationSplitView`.
The sidebar lists every remote folder; the content pane shows the
selected folder's details + bindings + per-folder actions
(Rename, Delete, Connect local folder). Per-binding actions
(Sync now / Pause / Resume / Disconnect / Browse) live next to each
binding inside the same pane. Scales to many folders without
crowding a horizontal toolbar with global buttons.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Pango

from .vault_binding_lifecycle import BindingCancellationRegistry
from .vault_binding_sync import format_sync_outcome_toast
from .vault_bindings import VaultBindingsStore
from .vault_local_index import VaultLocalIndex
from .vault_connect_folder_dialog import present_connect_folder_dialog
from .vault_error_messages import humanize
from .vault_folder_actions import (
    dispatch_disconnect,
    dispatch_pause,
    dispatch_resume,
)
from .vault_folder_runtime import VaultRuntime
from .vault_folder_ui_state import (
    binding_rows_for_render,
    default_ignore_patterns_text,
    folder_rows_from_cache,
    parse_ignore_patterns_text,
)
from .vault_usage import calculate_vault_usage


def _present_response_details_dialog(parent: Gtk.Widget, response_text: str) -> None:
    """Show the raw relay response in a scrollable, read-only window."""
    dialog = Adw.Dialog()
    dialog.set_title("Response details")
    dialog.set_content_width(640)
    dialog.set_content_height(420)

    toolbar = Adw.ToolbarView()
    dialog.set_child(toolbar)
    toolbar.add_top_bar(Adw.HeaderBar())

    body = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=8,
        margin_top=12,
        margin_bottom=12,
        margin_start=12,
        margin_end=12,
    )
    toolbar.set_content(body)

    body.append(Gtk.Label(
        label="Raw relay response",
        xalign=0,
        css_classes=["dim-label"],
    ))

    buf = Gtk.TextBuffer()
    buf.set_text(response_text or "(empty)")
    view = Gtk.TextView(
        buffer=buf,
        monospace=True,
        editable=False,
        cursor_visible=False,
    )
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    scroller.set_child(view)
    body.append(scroller)

    dialog.present(parent)


def build_vault_folders_tab(
    *,
    app: Adw.Application,
    parent_window: Adw.ApplicationWindow,
    config_dir: Path,
    config,
    vault_id: str,
) -> Gtk.Widget:
    """Build the Vault settings Folders tab (master/detail).

    The returned widget is an :class:`Adw.NavigationSplitView`. Sidebar
    lists every remote folder and a "+" header button to add new ones.
    Content pane shows the selected folder's details + bindings +
    per-folder actions, rebuilt from scratch on every refresh so no
    stale references survive a sync/pause/disconnect cycle.
    """
    local_index = VaultLocalIndex(config_dir)
    usage_by_folder_state = {"value": {}}
    selection_state: dict[str, str | None] = {"folder_id": None}
    sync_in_flight: dict[str, bool] = {}
    action_in_flight: dict[str, bool] = {}
    cancellation_registry = BindingCancellationRegistry()

    # F-518: VaultRuntime owns the per-tab vault lock + named ops.
    runtime = VaultRuntime(
        config_dir=config_dir,
        config=config,
        vault_id=vault_id,
        local_index=local_index,
    )

    # F-LT05: surface "real-time detection silently inactive" when the
    # optional watchdog dep is missing. Without it,
    # ``vault_filesystem_watcher.start_watchdog_observer`` returns None
    # and we lose event-driven (sub-second) detection. The tray's
    # autosync loop (F-LT06) still picks files up via a periodic
    # directory scan, so syncing isn't fully dead — just sluggish.
    try:
        import watchdog  # noqa: F401
        watchdog_available = True
    except ImportError:
        watchdog_available = False

    # ----------------------------------------------------------------
    # Top-level layout: NavigationSplitView (sidebar + content).
    # ----------------------------------------------------------------
    split = Adw.NavigationSplitView()
    split.set_min_sidebar_width(240)
    split.set_max_sidebar_width(420)
    split.set_sidebar_width_fraction(0.32)

    # ---- sidebar ----
    sidebar_toolbar = Adw.ToolbarView()
    sidebar_header = Adw.HeaderBar()
    # Embedded header bars (inside an outer Adw.ApplicationWindow) must
    # not paint their own window-control buttons, otherwise we get
    # duplicate min/max/close on every inner pane.
    sidebar_header.set_show_start_title_buttons(False)
    sidebar_header.set_show_end_title_buttons(False)
    add_folder_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
    add_folder_btn.set_tooltip_text("Add a new remote folder to this vault")
    add_folder_btn.set_sensitive(bool(vault_id))
    sidebar_header.pack_end(add_folder_btn)
    sidebar_toolbar.add_top_bar(sidebar_header)

    sidebar_body = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=4,
        margin_top=4, margin_bottom=8, margin_start=4, margin_end=4,
    )
    sidebar_toolbar.set_content(sidebar_body)

    if not watchdog_available:
        sidebar_body.append(Adw.Banner.new(
            "Real-time file detection is off — install the "
            "python3-watchdog package. Files are still picked up by "
            "the periodic auto-sync."
        ))

    folder_list = Gtk.ListBox()
    folder_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
    folder_list.add_css_class("navigation-sidebar")
    folder_list_scroll = Gtk.ScrolledWindow(vexpand=True)
    folder_list_scroll.set_child(folder_list)
    sidebar_body.append(folder_list_scroll)

    sidebar_status = Gtk.Label(
        xalign=0, wrap=True,
        css_classes=["dim-label", "caption"],
        margin_start=8, margin_end=8,
    )
    sidebar_body.append(sidebar_status)

    sidebar_page = Adw.NavigationPage(child=sidebar_toolbar, title="Folders")
    split.set_sidebar(sidebar_page)

    # ---- content ----
    # Skip the per-page Adw.HeaderBar: in wide-window mode it would
    # render an empty 46-px strip above the folder card (the sidebar
    # selection + outer window header already name the folder). On
    # narrow windows users still get keyboard / swipe-back navigation
    # via NavigationSplitView's collapsed mode.
    content_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    content_box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=12,
        margin_top=4, margin_bottom=16, margin_start=20, margin_end=20,
    )
    content_scroll.set_child(content_box)

    content_page = Adw.NavigationPage(child=content_scroll, title="Folder")
    split.set_content(content_page)

    # ``content_status`` survives detail rebuilds — it's appended to the
    # content_box on each render so toast-style messages from in-flight
    # ops persist even as the body is repopulated.
    content_status = Gtk.Label(
        xalign=0, wrap=True, css_classes=["dim-label"],
    )

    # ----------------------------------------------------------------
    # Tiny widget helpers.
    # ----------------------------------------------------------------
    def clear_box(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child

    def clear_listbox(box: Gtk.ListBox) -> None:
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child

    def set_sidebar_status(message: str, css_class: str = "dim-label") -> None:
        for klass in ("dim-label", "error", "success"):
            sidebar_status.remove_css_class(klass)
        sidebar_status.add_css_class(css_class)
        sidebar_status.set_label(message)

    def set_content_status(message: str, css_class: str = "dim-label") -> None:
        for klass in ("dim-label", "error", "success"):
            content_status.remove_css_class(klass)
        content_status.add_css_class(css_class)
        content_status.set_label(message)

    # ----------------------------------------------------------------
    # Data lookups.
    # ----------------------------------------------------------------
    def list_folders() -> list[dict]:
        """Return render-ready folder rows for the sidebar."""
        if not vault_id:
            return []
        try:
            cached = local_index.list_remote_folders(vault_id)
        except Exception as exc:  # noqa: BLE001
            set_sidebar_status(f"Could not load folder cache: {exc}", "error")
            return []
        return folder_rows_from_cache(
            cached, usage_by_folder=usage_by_folder_state["value"] or {},
        )

    def list_bindings_for_folder(remote_folder_id: str) -> list[dict]:
        """Return render-ready binding rows scoped to one remote folder."""
        if not vault_id or not remote_folder_id:
            return []
        try:
            store = VaultBindingsStore(local_index.db_path)
            all_bindings = store.list_bindings(vault_id=vault_id)
        except Exception as exc:  # noqa: BLE001
            set_content_status(f"Could not load bindings: {exc}", "error")
            return []
        binding_records = [
            b for b in all_bindings
            if b.state != "unbound" and b.remote_folder_id == remote_folder_id
        ]
        try:
            cached = local_index.list_remote_folders(vault_id)
        except Exception:  # noqa: BLE001
            cached = []
        names = {
            str(f.get("remote_folder_id", "")):
                str(f.get("display_name_enc") or "")
            for f in cached
        }
        return binding_rows_for_render(
            binding_records, folder_names_by_id=names,
        )

    # ----------------------------------------------------------------
    # Per-binding action runners — same shape as before, only the
    # post-completion refresh now drives ``refresh_all`` instead of the
    # old per-table refresh helpers.
    # ----------------------------------------------------------------
    def run_sync_now(binding_id: str, button: Gtk.Button) -> None:
        if sync_in_flight.get(binding_id):
            return
        sync_in_flight[binding_id] = True
        button.set_sensitive(False)
        set_content_status("Sync now: running…")

        def worker() -> None:
            try:
                author_device_id = config.device_id or ("0" * 32)
                device_name = (
                    str(config.device_name or "").strip() or "this device"
                )
                event = cancellation_registry.register(binding_id)
                try:
                    result = runtime.flush_and_sync_binding(
                        binding_id=binding_id,
                        author_device_id=author_device_id,
                        device_name=device_name,
                        should_continue=lambda: not event.is_set(),
                    )
                finally:
                    cancellation_registry.clear(binding_id)
                toast_text = format_sync_outcome_toast(result)
            except Exception as exc:  # noqa: BLE001
                error_message = humanize(exc)

                def fail() -> bool:
                    sync_in_flight[binding_id] = False
                    button.set_sensitive(True)
                    set_content_status(
                        f"Sync now failed: {error_message}", "error",
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                sync_in_flight[binding_id] = False
                button.set_sensitive(True)
                set_content_status(toast_text, "success")
                refresh_all()
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    def _idle_finish(toast: str | None, error: str | None, prefix: str) -> None:
        def apply() -> bool:
            if error:
                set_content_status(error, "error")
            else:
                set_content_status(toast or f"{prefix} done.", "success")
            refresh_all()
            return False

        GLib.idle_add(apply)

    def run_pause(binding_id: str) -> None:
        if action_in_flight.get(binding_id):
            return
        action_in_flight[binding_id] = True
        set_content_status("Pause: running…")

        def worker() -> None:
            try:
                store = VaultBindingsStore(local_index.db_path)
                toast, error = dispatch_pause(
                    store=store, binding_id=binding_id,
                    cancellation=cancellation_registry,
                )
                _idle_finish(toast, error, "Pause")
            finally:
                action_in_flight[binding_id] = False

        threading.Thread(target=worker, daemon=True).start()

    def run_resume(binding_id: str) -> None:
        if action_in_flight.get(binding_id):
            return
        action_in_flight[binding_id] = True
        set_content_status("Resume: running…")

        def worker() -> None:
            try:
                store = VaultBindingsStore(local_index.db_path)

                def flush(_binding) -> object:
                    # Resume reuses the Sync-now plumbing — same vault
                    # open, same registry registration, same
                    # should_continue gate so a fresh Pause arriving
                    # during the post-resume flush still aborts within
                    # ~1 chunk.
                    event = cancellation_registry.register(binding_id)
                    try:
                        return runtime.flush_and_sync_binding(
                            binding_id=binding_id,
                            author_device_id=config.device_id or ("0" * 32),
                            device_name=(
                                str(config.device_name or "").strip()
                                or "this device"
                            ),
                            should_continue=lambda: not event.is_set(),
                        )
                    finally:
                        cancellation_registry.clear(binding_id)

                toast, error = dispatch_resume(
                    store=store, binding_id=binding_id, flush=flush,
                )
                _idle_finish(toast, error, "Resume")
            finally:
                action_in_flight[binding_id] = False

        threading.Thread(target=worker, daemon=True).start()

    def run_disconnect(binding_id: str) -> None:
        if action_in_flight.get(binding_id):
            return
        action_in_flight[binding_id] = True
        set_content_status("Disconnect: confirming…")

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
            try:
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
                _idle_finish(toast, error, "Disconnect")
            finally:
                action_in_flight[binding_id] = False

        threading.Thread(target=worker, daemon=True).start()

    def open_browse_local(local_path: str) -> None:
        """Open the binding's local directory in the system file manager."""
        try:
            uri = Path(local_path).as_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:  # noqa: BLE001
            set_content_status(
                f"Could not open file manager: {exc}", "error",
            )

    # ----------------------------------------------------------------
    # Folder-level action dialogs.
    # ----------------------------------------------------------------
    def open_add_folder_dialog(_btn=None) -> None:
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
            if not vault_id:
                set_dialog_status("No local vault is connected.", "error")
                return

            patterns = read_ignore_patterns()
            confirm_btn.set_sensitive(False)
            cancel_btn.set_sensitive(False)
            set_dialog_status("Adding folder...", "dim-label")

            def worker() -> None:
                try:
                    author_device_id = config.device_id or ("0" * 32)
                    manifest = runtime.add_remote_folder(
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
                    usage_by_folder_state["value"] = usage
                    dialog.close()
                    # Auto-select the freshly-added folder so the user
                    # lands on its detail pane and can immediately wire
                    # up a binding without a second navigation step.
                    for f in (manifest.get("remote_folders") or []):
                        if str(f.get("display_name_enc") or "") == folder_name:
                            selection_state["folder_id"] = str(
                                f.get("remote_folder_id") or ""
                            )
                            break
                    refresh_all(f"Added {folder_name}.")
                    return False

                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        confirm_btn.connect("clicked", on_confirm)
        dialog.present(parent_window)

    def _lookup_folder_settings(
        remote_folder_id: str,
    ) -> tuple[str, list[str]]:
        """Pull the folder's current display name + ignore patterns out
        of the local manifest cache. Returns ("", []) if missing.
        """
        if not vault_id:
            return "", []
        try:
            cached = local_index.list_remote_folders(vault_id)
        except Exception:  # noqa: BLE001
            return "", []
        for f in cached:
            if str(f.get("remote_folder_id") or "") == remote_folder_id:
                return (
                    str(f.get("display_name_enc") or ""),
                    [str(p) for p in (f.get("ignore_patterns") or [])],
                )
        return "", []

    def open_configure_folder_dialog(remote_folder_id: str) -> None:
        """Edit the remote folder's name + ignore patterns post-create.

        F-LT12: replaces the rename-only dialog with a single Configure
        dialog so users can fix up ignore patterns after the initial
        Add — before this, those patterns were locked in at creation
        time with no later editing path.
        """
        if not vault_id or not remote_folder_id:
            return

        current_name, current_patterns = _lookup_folder_settings(remote_folder_id)

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
                    author_device_id = config.device_id or ("0" * 32)
                    manifest = runtime.update_remote_folder_settings(
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
                    usage_by_folder_state["value"] = usage
                    dialog.close()
                    refresh_all(f"Saved {new_name}.")
                    return False

                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        confirm_btn.connect("clicked", on_confirm)
        dialog.present(parent_window)

    def open_connect_local_dialog(remote_folder_id: str | None = None) -> None:
        if not vault_id:
            return

        def worker() -> None:
            try:
                manifest = runtime.fetch_manifest()
            except Exception as exc:  # noqa: BLE001
                error_message = humanize(exc)

                def fail() -> bool:
                    set_content_status(
                        f"Could not load manifest for connect: "
                        f"{error_message}", "error",
                    )
                    return False

                GLib.idle_add(fail)
                return

            def show() -> bool:
                all_choices = [
                    (str(f.get("display_name_enc", "")),
                     str(f.get("remote_folder_id", "")))
                    for f in manifest.get("remote_folders", []) or []
                    if isinstance(f, dict)
                    and str(f.get("state", "active")) == "active"
                ]
                if not all_choices:
                    set_content_status(
                        "No remote folders yet — create one before "
                        "connecting a local folder.", "error",
                    )
                    return False
                # When invoked from a per-folder card we narrow the
                # dropdown to that folder; otherwise we expose the
                # whole list.
                if remote_folder_id:
                    choices = [
                        c for c in all_choices if c[1] == remote_folder_id
                    ]
                    if not choices:
                        choices = all_choices
                else:
                    choices = all_choices

                store = VaultBindingsStore(local_index.db_path)

                def on_dialog_confirmed(record) -> None:
                    set_content_status(
                        "Binding created — running initial baseline…",
                    )
                    selection_state["folder_id"] = record.remote_folder_id
                    refresh_all()
                    threading.Thread(
                        target=lambda: _run_baseline_for_record(record),
                        daemon=True,
                    ).start()

                def _run_baseline_for_record(record) -> None:
                    try:
                        runtime.run_initial_baseline(record=record)
                    except Exception as exc:  # noqa: BLE001
                        msg = str(exc)

                        def fail() -> bool:
                            set_content_status(
                                f"Initial baseline failed: {msg}", "error",
                            )
                            refresh_all()
                            return False

                        GLib.idle_add(fail)
                        return

                    def succeed() -> bool:
                        set_content_status(
                            "Binding ready — initial baseline complete.",
                            "success",
                        )
                        refresh_all()
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

    # ----------------------------------------------------------------
    # Card builders.
    # ----------------------------------------------------------------
    def _make_flat_action_button(
        label: str,
        icon_name: str,
        callback,
        *,
        tooltip: str | None = None,
        css_classes: list[str] | None = None,
    ) -> Gtk.Button:
        """Flat icon-plus-label button. Lighter than a pill for cards."""
        button = Gtk.Button(css_classes=["flat", *(css_classes or [])])
        if tooltip:
            button.set_tooltip_text(tooltip)
        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            margin_start=4, margin_end=4,
        )
        content.append(Gtk.Image.new_from_icon_name(icon_name))
        content.append(Gtk.Label(label=label))
        button.set_child(content)
        button.connect("clicked", lambda _b: callback())
        return button

    def _make_overflow_button(
        actions: list[tuple[str, str, callable, list[str]]],
    ) -> Gtk.MenuButton:
        """Return a flat ``view-more-symbolic`` button whose popover holds
        the supplied secondary actions.

        Each action is ``(label, icon_name, callback, css_classes)``;
        css_classes is applied to the popover button so destructive ops
        can render in the warning tone. The popover dismisses itself
        after a click so the user gets immediate feedback.
        """
        button = Gtk.MenuButton()
        button.set_icon_name("view-more-symbolic")
        button.add_css_class("flat")
        button.set_tooltip_text("More actions")

        popover = Gtk.Popover()
        popover_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=6, margin_bottom=6,
            margin_start=6, margin_end=6,
        )
        popover.set_child(popover_box)
        button.set_popover(popover)

        for label, icon_name, callback, css_classes in actions:
            row = Gtk.Button()
            row.add_css_class("flat")
            for klass in css_classes:
                row.add_css_class(klass)
            content = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                margin_top=4, margin_bottom=4,
                margin_start=4, margin_end=4,
            )
            if icon_name:
                content.append(Gtk.Image.new_from_icon_name(icon_name))
            content.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            row.set_child(content)

            def _on_click(_b, cb=callback) -> None:
                popover.popdown()
                cb()

            row.connect("clicked", _on_click)
            popover_box.append(row)
        return button

    def _build_binding_row(row: dict) -> Gtk.Widget:
        """Render one binding as an ``Adw.ActionRow`` with a primary
        action button + an overflow popover for the rest. Lighter than
        the old card-of-pills shape and follows the libadwaita idiom for
        per-row controls.
        """
        bid = row["binding_id"]
        state = row["state"]
        sync_mode = row["sync_mode"]
        local_path = row["local_path"]

        path_basename = Path(local_path).name or local_path
        action_row = Adw.ActionRow(
            title=path_basename,
            subtitle=local_path,
            tooltip_text=local_path,
        )
        action_row.set_subtitle_lines(1)
        action_row.set_title_lines(1)
        action_row.set_use_markup(False)
        # Adw.ActionRow's default natural width forces the whole
        # NavigationSplitView's content pane to stay wide — even though
        # the long path is already ellipsized. Override the minimum so
        # the binding row can shrink with the window.
        action_row.set_size_request(150, -1)

        # Meta strip: don't echo "bound" — every row that survives the
        # ``state != "unbound"`` filter and isn't paused is bound, so
        # the word is dead weight. Paused / needs-preflight surface
        # implicitly through the primary action (Resume / no action).
        meta_parts: list[str] = []
        if sync_mode and sync_mode != "paused":
            meta_parts.append(sync_mode)
        meta_parts.append(f"rev {row['last_synced_revision']}")
        meta_label = Gtk.Label(
            label="  ·  ".join(meta_parts),
            xalign=1,
            css_classes=["dim-label", "caption"],
            valign=Gtk.Align.CENTER,
        )
        action_row.add_suffix(meta_label)

        # Primary action — flat icon, not a pill. Keeps the row visually
        # quiet next to the section header. Tooltips carry the verb.
        if state == "bound" and sync_mode != "paused":
            primary = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
            primary.add_css_class("flat")
            primary.set_valign(Gtk.Align.CENTER)
            primary.set_tooltip_text(
                "Sync now — drain pending local changes and push them to "
                "the vault.",
            )
            primary.connect(
                "clicked",
                lambda _b, btn=primary: run_sync_now(bid, btn),
            )
            action_row.add_suffix(primary)

            secondary_actions: list[tuple[str, str, callable, list[str]]] = []
            if local_path:
                secondary_actions.append((
                    "Open in file manager",
                    "folder-open-symbolic",
                    lambda p=local_path: open_browse_local(p),
                    [],
                ))
            secondary_actions.append((
                "Pause sync",
                "media-playback-pause-symbolic",
                lambda: run_pause(bid),
                [],
            ))
            secondary_actions.append((
                "Disconnect",
                "user-trash-symbolic",
                lambda: run_disconnect(bid),
                ["destructive-action"],
            ))
            action_row.add_suffix(_make_overflow_button(secondary_actions))

        elif state == "paused" or sync_mode == "paused":
            primary = Gtk.Button.new_from_icon_name(
                "media-playback-start-symbolic",
            )
            primary.add_css_class("flat")
            primary.set_valign(Gtk.Align.CENTER)
            primary.set_tooltip_text(
                "Resume syncing and drain anything the watcher queued.",
            )
            primary.connect(
                "clicked", lambda _b: run_resume(bid),
            )
            action_row.add_suffix(primary)

            secondary_actions = []
            if local_path:
                secondary_actions.append((
                    "Open in file manager",
                    "folder-open-symbolic",
                    lambda p=local_path: open_browse_local(p),
                    [],
                ))
            secondary_actions.append((
                "Disconnect",
                "user-trash-symbolic",
                lambda: run_disconnect(bid),
                ["destructive-action"],
            ))
            action_row.add_suffix(_make_overflow_button(secondary_actions))

        else:
            # ``needs-preflight`` and other transitional states — only
            # the browse affordance makes sense.
            if local_path:
                browse_btn = Gtk.Button.new_from_icon_name(
                    "folder-open-symbolic",
                )
                browse_btn.add_css_class("flat")
                browse_btn.set_tooltip_text("Open in file manager")
                browse_btn.set_valign(Gtk.Align.CENTER)
                browse_btn.connect(
                    "clicked",
                    lambda _b, p=local_path: open_browse_local(p),
                )
                action_row.add_suffix(browse_btn)
        return action_row

    def _build_sidebar_row(row: dict) -> Gtk.Widget:
        rfid = row["remote_folder_id"]
        bindings = list_bindings_for_folder(rfid)

        # Outer horizontal layout: text cluster on the left, overflow
        # menu icon on the right (so the user can configure the folder
        # without opening the detail pane).
        outer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            margin_top=8, margin_bottom=8,
            margin_start=12, margin_end=8,
        )

        text_cluster = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2,
            hexpand=True, valign=Gtk.Align.CENTER,
        )
        outer.append(text_cluster)

        title = Gtk.Label(
            label=row["name"], xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["heading"],
        )
        text_cluster.append(title)

        sub_parts: list[str] = []
        if not bindings:
            sub_parts.append("Not bound")
        elif len(bindings) == 1:
            b = bindings[0]
            base = Path(b["local_path"]).name or b["local_path"]
            sub_parts.append(base)
            if b["state"] == "paused" or b["sync_mode"] == "paused":
                sub_parts.append("paused")
            elif b["state"] == "needs-preflight":
                sub_parts.append("setting up")
        else:
            sub_parts.append(f"{len(bindings)} bindings")

        subtitle = Gtk.Label(
            label="  ·  ".join(sub_parts),
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["dim-label", "caption"],
        )
        text_cluster.append(subtitle)

        # F-LT12: per-row overflow menu so Configure / Delete are
        # reachable without first selecting the folder. The menu
        # button intercepts the click so it doesn't trigger row
        # selection.
        overflow_btn = _make_overflow_button([
            (
                "Configure folder",
                "document-edit-symbolic",
                lambda r=rfid: open_configure_folder_dialog(r),
                [],
            ),
            (
                "Delete folder",
                "user-trash-symbolic",
                lambda: None,
                ["destructive-action"],
            ),
        ])
        overflow_btn.set_valign(Gtk.Align.CENTER)
        outer.append(overflow_btn)

        listbox_row = Gtk.ListBoxRow(child=outer)
        listbox_row.set_activatable(True)
        return listbox_row

    # ----------------------------------------------------------------
    # Detail pane builder.
    # ----------------------------------------------------------------
    def _build_empty_state() -> Gtk.Widget:
        empty = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
            hexpand=True, vexpand=True,
        )
        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        icon.set_pixel_size(64)
        icon.add_css_class("dim-label")
        empty.append(icon)
        empty.append(Gtk.Label(
            label="Select a folder",
            css_classes=["title-2"],
        ))
        empty.append(Gtk.Label(
            label="Pick a folder from the list, or click + to add a new one.",
            css_classes=["dim-label"],
            wrap=True, justify=Gtk.Justification.CENTER,
        ))
        return empty

    def render_detail() -> None:
        clear_box(content_box)
        rfid = selection_state["folder_id"]
        folder_row = folder_rows_by_id.get(rfid) if rfid else None

        if folder_row is None:
            content_page.set_title("Folders")
            content_box.append(_build_empty_state())
            content_box.append(content_status)
            return

        name = folder_row["name"]
        content_page.set_title(name)

        # F-LT11: stat tiles instead of a one-line caption-sized
        # "Status · Current · Stored · History" string. Each pair sits
        # in a Gtk.FlowBox child so a narrow window wraps the trailing
        # tiles onto a second / third row instead of squashing all four
        # into unreadable column widths. No card chrome — the values
        # are content-level information, not a separate object.
        stats_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=1,
            max_children_per_line=3,
            column_spacing=24,
            row_spacing=12,
        )
        # Status tile dropped: every remote folder is currently
        # "active" (no soft-delete / archive flow yet) so the column
        # was always identical and just stole space. Re-introduce it
        # alongside the state-machine work when those values can vary.
        for caption, value in (
            ("Current size", folder_row["current"]),
            ("Remote stored", folder_row["stored"]),
            ("History", folder_row["history"]),
        ):
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            caption_label = Gtk.Label(
                label=caption, xalign=0,
                css_classes=["dim-label", "caption"],
            )
            value_label = Gtk.Label(
                label=value, xalign=0,
                css_classes=["title-4"],
            )
            value_label.set_ellipsize(Pango.EllipsizeMode.END)
            tile.append(caption_label)
            tile.append(value_label)
            stats_flow.append(tile)
        content_box.append(stats_flow)

        # Binding section (singular — only one binding per remote folder
        # is allowed in practice). When no binding exists yet, the
        # primary call-to-action is "Connect with local folder" and it
        # belongs right under the heading; once a binding exists the
        # action is meaningless (the user already bound this folder)
        # so we hide it instead of leaving a dead button around.
        bindings_heading = Gtk.Label(
            label="Local binding", xalign=0,
            css_classes=["title-3"], margin_top=10,
        )
        content_box.append(bindings_heading)

        bindings = list_bindings_for_folder(rfid)
        if not bindings:
            connect_btn = Gtk.Button(
                label="Connect with local folder",
                css_classes=["pill", "suggested-action"],
                halign=Gtk.Align.START,
                margin_top=4,
            )
            connect_btn.set_tooltip_text(
                "Bind this remote folder to a local path. Default sync "
                "mode is Backup only (uploads local changes; remote "
                "changes never come down).",
            )
            connect_btn.connect(
                "clicked",
                lambda _b: open_connect_local_dialog(remote_folder_id=rfid),
            )
            content_box.append(connect_btn)
        else:
            bindings_listbox = Gtk.ListBox(
                selection_mode=Gtk.SelectionMode.NONE,
                css_classes=["boxed-list"],
            )
            bindings_listbox.set_size_request(150, -1)
            for row in bindings:
                bindings_listbox.append(_build_binding_row(row))
            content_box.append(bindings_listbox)

        content_box.append(content_status)

    # ----------------------------------------------------------------
    # Sidebar render + selection.
    # ----------------------------------------------------------------
    folder_rows_by_id: dict[str, dict] = {}
    suspend_selection_signal = {"value": False}

    def refresh_sidebar() -> None:
        suspend_selection_signal["value"] = True
        try:
            clear_listbox(folder_list)
            folder_rows_by_id.clear()
            rows = list_folders()
            for row in rows:
                folder_rows_by_id[row["remote_folder_id"]] = row
                folder_list.append(_build_sidebar_row(row))

            if not vault_id:
                set_sidebar_status("No local vault is connected.")
            elif not rows:
                set_sidebar_status("No remote folders yet — use + to add one.")
            else:
                set_sidebar_status(f"{len(rows)} folder(s).")

            # Reselect previous selection if still present, else pick the
            # first row so the detail pane is never empty when there are
            # folders to choose from.
            target = selection_state["folder_id"]
            if target not in folder_rows_by_id:
                target = next(iter(folder_rows_by_id), None)
                selection_state["folder_id"] = target
            if target is not None:
                row_index = list(folder_rows_by_id).index(target)
                list_row = folder_list.get_row_at_index(row_index)
                if list_row is not None:
                    folder_list.select_row(list_row)
        finally:
            suspend_selection_signal["value"] = False

    def refresh_all(message: str | None = None) -> None:
        refresh_sidebar()
        render_detail()
        if message is not None:
            set_content_status(message)

    def on_row_selected(_listbox, row) -> None:
        if suspend_selection_signal["value"]:
            return
        if row is None:
            selection_state["folder_id"] = None
            render_detail()
            return
        idx = row.get_index()
        ids = list(folder_rows_by_id)
        if 0 <= idx < len(ids):
            selection_state["folder_id"] = ids[idx]
        render_detail()

    folder_list.connect("row-selected", on_row_selected)
    add_folder_btn.connect("clicked", lambda _b: open_add_folder_dialog())

    def refresh_folders_usage_async(message: str | None = None) -> None:
        if not vault_id:
            return
        set_sidebar_status("Refreshing folder usage…")

        def worker() -> None:
            try:
                manifest = runtime.fetch_manifest()
                usage = calculate_vault_usage(manifest).by_folder
            except Exception as exc:  # noqa: BLE001
                error_message = humanize(exc)

                def fail() -> bool:
                    set_sidebar_status(
                        f"Folder usage unavailable: {error_message}", "error",
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                usage_by_folder_state["value"] = usage
                refresh_all(message)
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    refresh_all()
    refresh_folders_usage_async()
    return split
