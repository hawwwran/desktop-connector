"""Transfer History window shell (`python -m src.windows history`).

``show_history`` constructs a :class:`HistoryContext`, runs the
zombie-WAITING scrubber once, builds the Adw application, and dispatches
``on_activate``. ``on_activate`` lays out the window (header bar,
device picker, scrolling list container), wires every signal, and
fires the first ``build_list`` + the adaptive refresh tick.

Late-bound callables (``build_list``, ``refresh_tick``,
``reset_history_view``, ``show_toast``) are assigned onto the context
BEFORE any signal that could fire them is connected — same constraint
as the original closures held by reference.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..config import Config
from ..crypto import KeyManager
from ..history import TransferHistory
from ..windows_common import (
    _create_device_picker,
    _make_app,
)
from .clear_all import on_clear_all as _on_clear_all
from .context import HistoryContext
from .device_filter import on_history_device_changed as _on_history_device_changed
from .refresh import build_list as _build_list, refresh_tick as _refresh_tick
from .device_filter import _reset_history_view
from .toast import show_toast as _show_toast
from .zombie_scrub import scrub_zombie_waiting as _scrub_zombie_waiting


def show_history(config_dir: Path):
    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    history = TransferHistory(config_dir)

    ctx = HistoryContext(
        config_dir=config_dir,
        config=config,
        crypto=crypto,
        history=history,
    )

    _scrub_zombie_waiting(ctx)

    app = _make_app()
    ctx.app = app

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Transfer History",
                                     default_width=500, default_height=480)
        win.set_size_request(400, 300)
        ctx.win = win

        # Card-per-item styling + flush progress bar.
        css = Gtk.CssProvider()
        css.load_from_string("""
            .transfer-card {
                padding-top: 5px;
                padding-bottom: 5px;
                transition: background-color 120ms ease,
                            opacity 300ms ease-out,
                            min-height 300ms ease-out,
                            padding 300ms ease-out,
                            margin 300ms ease-out;
            }
            .transfer-card.has-progress {
                padding-bottom: 0;
            }
            /* Shrink + fade when deleting. Matching Python timeout
               removes the widget from the tree after the transition. */
            .transfer-card.removing {
                opacity: 0;
                min-height: 0;
                padding-top: 0;
                padding-bottom: 0;
                margin-top: 0;
                margin-bottom: 0;
            }
            .transfer-card:hover {
                background-color: mix(@card_bg_color, @window_fg_color, 0.06);
            }
            .transfer-card:active {
                background-color: mix(@card_bg_color, @window_fg_color, 0.12);
            }
            .transfer-card-list,
            .transfer-card-list > row,
            .transfer-card-list > row.activatable {
                background: transparent;
                border: 0;
                padding-left: 3px;
                margin-left: 0px;
            }
            .transfer-card-list > row > box {
                margin-left: 0px;
                padding-left: 0px;
            }
            .transfer-card-list > row frame {
                min-width: 50px;
                min-height: 50px;
                background: alpha(@card_shade_color, 0.3);
                border-radius: 6px;
            }
            .transfer-card-list > row.activatable:hover,
            .transfer-card-list > row.activatable:active {
                background: transparent;
            }
            .upload-bar, .download-bar, .delivery-bar {
                min-height: 5px;
            }
            .upload-bar trough, .download-bar trough, .delivery-bar trough {
                min-height: 5px;
                background: alpha(@card_shade_color, 0.35);
                border-radius: 0;
            }
            .upload-bar progress, .download-bar progress, .delivery-bar progress {
                min-height: 5px;
                border-radius: 0;
            }
            .upload-bar progress {
                background-color: #FDD00C;
            }
            .download-bar progress {
                background-color: #5898FB;
            }
            .delivery-bar progress {
                background-color: #3986FC;
            }
            @keyframes pulse {
                0% { opacity: 0.5; }
                50% { opacity: 1.0; }
                100% { opacity: 0.5; }
            }
            .pulse-bar progress {
                animation: pulse 2s ease-in-out infinite;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        toolbar_view = Adw.ToolbarView()
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(toolbar_view)
        win.set_content(toast_overlay)

        header = Adw.HeaderBar()
        header.set_decoration_layout(":close")
        folder_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        folder_btn.set_tooltip_text("Open save folder")
        folder_btn.add_css_class("brand-action-accent")
        folder_btn.connect("clicked", lambda b: subprocess.Popen([
            "xdg-open", str(config.save_directory)
        ]))
        header.pack_start(folder_btn)

        clear_all_btn = Gtk.Button.new_from_icon_name("edit-clear-all-symbolic")
        clear_all_btn.set_tooltip_text("Clear visible history")
        clear_all_btn.add_css_class("brand-action-destructive")
        clear_all_btn.connect("clicked", lambda b: _on_clear_all(ctx, b))
        header.pack_start(clear_all_btn)

        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroll)

        clamp = Adw.Clamp(maximum_size=9999, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        scroll.set_child(clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        clamp.set_child(content_box)

        device_picker, selected_device, paired_devices = _create_device_picker(
            config,
            title="History for",
            subtitle="Connected device",
        )
        device_group = Adw.PreferencesGroup()
        device_group.add(device_picker)
        content_box.append(device_group)
        clear_all_btn.set_sensitive(selected_device[0] is not None)

        list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content_box.append(list_container)

        # Stash the picker / clear-all state + container on the context so
        # the lifted helpers can reach them.
        ctx.device_picker = device_picker
        ctx.selected_device = selected_device
        ctx.paired_devices = paired_devices
        ctx.clear_all_btn = clear_all_btn
        ctx.list_container = list_container

        # Late-bound callables. Assigned BEFORE any signal that could
        # fire them is connected, since `on_history_device_changed`
        # calls ``ctx.build_list`` and ``refresh_tick`` calls itself
        # via the same slot.
        ctx.show_toast = _show_toast
        ctx.reset_history_view = lambda: _reset_history_view(ctx)
        ctx.build_list = lambda: _build_list(ctx)
        ctx.refresh_tick = lambda: _refresh_tick(ctx)

        device_picker.connect(
            "notify::selected",
            lambda combo, pspec: _on_history_device_changed(ctx, combo, pspec),
        )

        ctx.build_list()

        # Adaptive refresh: 1s during active transfers, 3s otherwise
        GLib.timeout_add(1000, ctx.refresh_tick)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
