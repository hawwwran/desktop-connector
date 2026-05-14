"""Settings window shell (`python -m src.windows settings`).

``show_settings`` constructs the ``Config`` / ``KeyManager`` /
``ConnectionManager`` / ``ConnectedDeviceRegistry``, fetches an optional
stats snapshot from the relay, builds a :class:`SettingsContext`,
constructs the Adw application and dispatches ``on_activate``.
``on_activate`` lays out the window (toolbar view, scrolled clamped
content box) and dispatches each group builder in the same order the
original monolith rendered them: Connection → Appearance → Vault →
Receive Actions → Receive Action Flood Protection → This Device →
Connected Devices → (optional) Connection Statistics → Security → Logs
→ version/shape footer.

The Logs group builder is invoked AFTER the security group, matching
the original ``add_logs_group()`` call site (the function was defined
early but called late, so the layout pinned by the source-pin tests is
preserved).
"""

from __future__ import annotations

import os as _os
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from ..api_client import ApiClient
from ..bootstrap.app_version import get_app_version
from ..brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..config import Config
from ..connection import ConnectionManager
from ..crypto import KeyManager
from ..devices import ConnectedDeviceRegistry
from ..windows_common import _make_app
from . import (
    group_logs,
    group_notifications,
    group_pairings,
    group_receive_actions,
    group_relay,
    group_secret_storage,
    group_theme,
    group_vault,
)
from .context import SettingsContext


def show_settings(config_dir: Path):
    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")

    # Fetch stats from server. Short timeout so the window doesn't sit
    # behind a hung TCP connect when the relay is unreachable — the stats
    # group is optional, and the rest of the UI shouldn't wait for it.
    stats = None
    settings_registry = ConnectedDeviceRegistry(config)
    settings_active_device = settings_registry.get_active_device()
    try:
        api = ApiClient(conn, crypto)
        stats = api.get_stats(
            paired_with=settings_active_device.device_id
            if settings_active_device else None,
            timeout=2.0,
        )
    except Exception:
        pass

    ctx = SettingsContext(
        config_dir=config_dir,
        config=config,
        crypto=crypto,
        conn=conn,
        settings_registry=settings_registry,
        settings_active_device=settings_active_device,
        stats=stats,
    )

    app = _make_app()
    ctx.app = app

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Settings", default_width=630, default_height=624)
        win.set_resizable(True)
        ctx.win = win

        toolbar_view = Adw.ToolbarView()
        win.set_content(toolbar_view)
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroll)

        clamp = Adw.Clamp(maximum_size=500, margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        scroll.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(content)
        ctx.content = content

        # Per-group builders, dispatched in the same order the original
        # monolith rendered them. ``group_logs.build`` is invoked AFTER
        # ``group_secret_storage.build`` to mirror the original
        # ``add_logs_group()`` call site.
        group_relay.build(ctx)
        group_theme.build(ctx)
        group_vault.build(ctx)
        group_receive_actions.build(ctx)
        group_notifications.build(ctx)
        group_pairings.build(ctx)
        group_secret_storage.build(ctx)
        group_logs.build(ctx)

        # --- Footer: version + install shape ---------------------------------
        # $APPIMAGE is set by AppRun when running inside the AppImage; absent
        # for install-from-source.sh layouts and for dev-tree runs of
        # `python3 -m src.main`. Both non-AppImage paths get the same shape
        # label since they share the lifecycle (manual install / no in-app
        # updater).
        version_str = get_app_version()
        appimage_env = _os.environ.get("APPIMAGE")
        if appimage_env:
            shape_str = "AppImage release"
        else:
            shape_str = "Installed from source"

        version_label = Gtk.Label(label=f"Desktop Connector {version_str}",
                                   xalign=0.5)
        version_label.add_css_class("dim-label")
        version_label.add_css_class("caption-heading")
        version_label.set_margin_top(8)
        content.append(version_label)

        shape_label = Gtk.Label(label=shape_str, xalign=0.5)
        shape_label.add_css_class("dim-label")
        shape_label.add_css_class("caption")
        if appimage_env:
            shape_label.set_tooltip_text(appimage_env)
        content.append(shape_label)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
