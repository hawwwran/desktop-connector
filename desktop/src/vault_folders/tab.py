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

from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio  # noqa: E402

from ..vault.binding.lifecycle import BindingCancellationRegistry
from ..vault.folder.runtime import VaultRuntime
from ..vault.state.local_index import VaultLocalIndex
from .context import FoldersContext
from .data import refresh_folders_usage_async as _refresh_folders_usage_async
from .dialog_add_folder import open_add_folder_dialog
from .rows import refresh_sidebar as _refresh_sidebar, render_detail as _render_detail


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
    cancellation_registry = BindingCancellationRegistry()

    # F-518: VaultRuntime owns the per-tab vault lock + named ops.
    runtime = VaultRuntime(
        config_dir=config_dir,
        config=config,
        vault_id=vault_id,
        local_index=local_index,
    )

    ctx = FoldersContext(
        app=app,
        parent_window=parent_window,
        config_dir=config_dir,
        config=config,
        vault_id=vault_id,
        local_index=local_index,
        runtime=runtime,
        cancellation_registry=cancellation_registry,
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

    # Stash widget refs on the context so helpers can reach them.
    ctx.sidebar_status = sidebar_status
    ctx.content_status = content_status
    ctx.content_box = content_box
    ctx.folder_list = folder_list
    ctx.split = split
    ctx.content_page = content_page

    # ----------------------------------------------------------------
    # Status setters + tiny helpers that close over the widgets above.
    # ----------------------------------------------------------------
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

    def open_browse_local(local_path: str) -> None:
        """Open the binding's local directory in the system file manager."""
        try:
            uri = Path(local_path).as_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:  # noqa: BLE001
            set_content_status(
                f"Could not open file manager: {exc}", "error",
            )

    ctx.set_sidebar_status = set_sidebar_status
    ctx.set_content_status = set_content_status
    ctx.open_browse_local = open_browse_local

    # ----------------------------------------------------------------
    # Refresh callables — must be assigned to ctx BEFORE any signal
    # that could fire them is connected, since helpers call back into
    # them via the context.
    # ----------------------------------------------------------------
    def refresh_sidebar() -> None:
        _refresh_sidebar(ctx)

    def render_detail() -> None:
        _render_detail(ctx)

    def refresh_all(message: str | None = None) -> None:
        refresh_sidebar()
        render_detail()
        if message is not None:
            set_content_status(message)

    def refresh_folders_usage_async(message: str | None = None) -> None:
        _refresh_folders_usage_async(ctx, message)

    ctx.refresh_sidebar = refresh_sidebar
    ctx.render_detail = render_detail
    ctx.refresh_all = refresh_all
    ctx.refresh_folders_usage_async = refresh_folders_usage_async

    # ----------------------------------------------------------------
    # Selection wiring.
    # ----------------------------------------------------------------
    def on_row_selected(_listbox, row) -> None:
        if ctx.suspend_selection_signal["value"]:
            return
        if row is None:
            ctx.selection_state["folder_id"] = None
            render_detail()
            return
        idx = row.get_index()
        ids = list(ctx.folder_rows_by_id)
        if 0 <= idx < len(ids):
            ctx.selection_state["folder_id"] = ids[idx]
        render_detail()

    folder_list.connect("row-selected", on_row_selected)
    add_folder_btn.connect("clicked", lambda _b: open_add_folder_dialog(ctx))

    refresh_all()
    # F-510 follow-up: the manifest fetch behind ``refresh_folders_usage_async``
    # makes ``1 + N`` auth-billed calls (root + one shard per folder).
    # Firing it eagerly at build time tripped the server-side
    # ``vaultAuthLimit`` (default 60-second rolling window) the moment
    # Vault Settings opened — even before the user clicked the Folders
    # tab — because every Vault Settings open builds every tab. Defer
    # to the first ``map`` so the fetch only runs when the tab is
    # actually shown. Idempotent: subsequent tab switches do **not**
    # re-fetch (would burn budget on every click); user-initiated
    # actions still fan out to ``refresh_folders_usage_async`` via
    # ``ctx`` if they need a fresh snapshot.
    usage_load_state = {"loaded": False}

    def _on_first_map(_widget) -> None:
        if usage_load_state["loaded"]:
            return
        usage_load_state["loaded"] = True
        refresh_folders_usage_async()

    split.connect("map", _on_first_map)
    return split
