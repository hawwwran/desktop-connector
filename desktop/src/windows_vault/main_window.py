"""Vault settings GTK window — sidebar + tab stack composer.

Pre-split this lived in ``windows_vault.show_vault_main`` (~1500 lines
of nested closures). The shell — header bar, sidebar, ``Gtk.Stack`` — and
the lifecycle wiring stay here; each tab's body comes from a sibling
``tab_*.py`` module via a ``build_*_tab(ctx, win) -> Gtk.Widget`` shape.
"""

import logging
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ..brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..windows_common import _make_app
from ._main_context import MainContext
from .tab_activity import build_activity_tab
from .tab_danger import build_danger_tab
from .tab_maintenance import build_maintenance_tab
from .tab_migration import build_migration_tab
from .tab_recovery import build_recovery_tab


def show_vault_main(config_dir: Path, vault_id_override: str | None = None):
    """Vault settings GTK window skeleton (T3.4).

    Top: Vault ID with copy button + (placeholder) QR icon.
    Body: a left-hand vertical sidebar (``Gtk.StackSidebar``) over a
    ``Gtk.Stack`` with one page per section — Recovery, Folders,
    Devices, Security, Sync safety, Storage, Activity, Maintenance,
    Migration, Danger zone. Recovery / Folders / Activity / Maintenance
    / Migration / Danger zone are real; Devices / Security / Sync
    safety / Storage are deliberate placeholders awaiting later
    development.

    ``vault_id_override`` (F-U14): optional 12-char canonical vault id
    threaded through every tab and worker that uses ``vault_id_undashed``.
    When omitted, falls back to ``config['vault']['last_known_id']`` for
    backwards compatibility with the tray's current single-vault wiring.

    M1 manual-smoke surface; later phases populate the empty tabs.
    """
    from ..config import Config
    from ..vault_window_args import resolve_active_vault_id

    log = logging.getLogger("desktop-connector.vault-ui")
    config = Config(config_dir)
    app = _make_app()

    # Reading the vault id from local grant storage is T3.2's surface;
    # for the M1 walk-through we surface whatever's currently stashed
    # under config["vault"]["last_known_id"] (set by the wizard on
    # successful create) — unless an explicit ``--vault-id`` override
    # was passed to the dispatcher (F-U14), in which case that wins.
    vault_id_undashed = resolve_active_vault_id(config, vault_id_override)

    ctx = MainContext(
        app=app,
        config=config,
        config_dir=config_dir,
        vault_id_undashed=vault_id_undashed,
        log=log,
    )

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Vault settings",
            default_width=880,
            default_height=560,
        )
        toolbar = Adw.ToolbarView()
        # Hairline separator under the header bar so the chrome is
        # visually delimited from the content below.
        toolbar.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        win.set_content(toolbar)

        # ---- header bar: window title centered, Vault ID cluster on
        # the start edge so the ID + its actions live in the chrome
        # (no dead-space row inside the body for them).
        header_bar = Adw.HeaderBar()

        id_cluster = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        id_label = Gtk.Label(label="Vault ID:", xalign=0)
        id_label.add_css_class("dim-label")
        id_cluster.append(id_label)

        id_value = Gtk.Label(label=ctx.vault_id_dashed(), xalign=0)
        id_value.add_css_class("monospace")
        id_cluster.append(id_value)

        def on_copy(_btn):
            display = win.get_display()
            if display is not None:
                display.get_clipboard().set(ctx.vault_id_dashed())
        copy_btn = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        copy_btn.add_css_class("flat")
        copy_btn.set_tooltip_text("Copy Vault ID to clipboard")
        copy_btn.connect("clicked", on_copy)
        id_cluster.append(copy_btn)

        # ``view-grid-symbolic`` is the closest reliable Adwaita
        # equivalent to a QR glyph; ``qr-code-symbolic`` isn't in the
        # freedesktop set so it rendered as a missing-icon stub.
        qr_btn = Gtk.Button.new_from_icon_name("view-grid-symbolic")
        qr_btn.add_css_class("flat")
        qr_btn.set_tooltip_text("Show Vault ID as a QR code (post-v1)")
        qr_btn.set_sensitive(False)
        id_cluster.append(qr_btn)

        header_bar.pack_start(id_cluster)
        toolbar.add_top_bar(header_bar)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=0, margin_bottom=16, margin_start=16, margin_end=16,
        )
        toolbar.set_content(outer)

        # ---- sidebar + stack pane ----
        # Vertical sidebar on the left, page content on the right. Uses
        # ``Gtk.Stack`` (not ``Adw.ViewStack``) because ``Gtk.StackSidebar``
        # binds directly to a ``Gtk.Stack``; same ``add_titled`` shape, no
        # behavioural difference for our use.
        view_stack = Gtk.Stack()
        view_stack.set_hexpand(True)
        view_stack.set_vexpand(True)
        view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(view_stack)
        sidebar.set_size_request(180, -1)
        sidebar.set_vexpand(True)

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        split.set_vexpand(True)
        split.append(sidebar)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        split.append(view_stack)
        outer.append(split)

        def add_tab(name: str, title: str, body: Gtk.Widget) -> None:
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_child(body)
            view_stack.add_titled(scroller, name, title)

        add_tab("recovery", "Recovery", build_recovery_tab(ctx, win))

        from ..vault_folders import build_vault_folders_tab
        add_tab("folders", "Folders", build_vault_folders_tab(
            app=app,
            parent_window=win,
            config_dir=config_dir,
            config=config,
            vault_id=vault_id_undashed,
        ))

        # Other tabs are empty placeholders for later phases.
        # (Activity + Maintenance get real implementations below — F-501.)
        for name, title in [
            ("devices", "Devices"),
            ("security", "Security"),
            ("sync_safety", "Sync safety"),
            ("storage", "Storage"),
        ]:
            placeholder = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=8,
                margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
            )
            placeholder.append(Gtk.Label(
                label=title, xalign=0, css_classes=["title-3"],
            ))
            placeholder.append(Gtk.Label(
                label="This panel is reserved for later development. "
                      "No controls are available yet.",
                xalign=0, wrap=True, css_classes=["dim-label"],
            ))
            add_tab(name, title, placeholder)

        add_tab("activity", "Activity", build_activity_tab(ctx, win))
        add_tab("maintenance", "Maintenance", build_maintenance_tab(ctx, win))
        add_tab("migration", "Migration", build_migration_tab(ctx, win))
        add_tab("danger_zone", "Danger zone", build_danger_tab(ctx, win))

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
