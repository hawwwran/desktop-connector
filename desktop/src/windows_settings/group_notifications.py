"""Notifications group: per-feature toast toggles.

Currently exposes a single switch — "Connection state messages" —
which gates the desktop-toast emitted on "Connection lost" / "Connection
restored" transitions. Default ON to preserve historical behaviour;
users who find the toasts noisy can flip it off without restarting
the tray (the receiver reloads config on each state-change callback).
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .context import SettingsContext


def build(ctx: SettingsContext) -> Adw.PreferencesGroup:
    config = ctx.config

    group = Adw.PreferencesGroup(title="Notifications")
    ctx.content.append(group)

    conn_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
    conn_switch.set_active(config.connection_state_notifications)
    conn_switch.connect(
        "notify::active",
        lambda sw, _: setattr(
            config, "connection_state_notifications", sw.get_active(),
        ),
    )
    conn_row = Adw.ActionRow(
        title="Connection state messages",
        subtitle=(
            "Show a desktop toast when the relay connection is lost "
            "or restored"
        ),
    )
    conn_row.add_suffix(conn_switch)
    conn_row.set_activatable_widget(conn_switch)
    group.add(conn_row)

    return group
