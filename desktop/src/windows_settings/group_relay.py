"""Connection group: status row, relay URL entry, long-poll status.

Pre-split this lived inline in ``on_activate`` as the
``conn_group = Adw.PreferencesGroup(title="Connection")`` block plus
the ``on_retry_lp``, ``refresh_lp_status`` and ``on_save`` closures.

The 3 s ``GLib.timeout_add(3000, refresh_lp_status)`` cadence is
preserved; ``refresh_lp_status`` returns ``True`` to keep the timer
alive, same as the original.
"""

from __future__ import annotations

import json

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

from .context import SettingsContext


def build(ctx: SettingsContext) -> Adw.PreferencesGroup:
    config = ctx.config
    conn = ctx.conn

    conn_group = Adw.PreferencesGroup(title="Connection")
    ctx.content.append(conn_group)

    # Quick health check
    try:
        reachable = conn.check_connection()
    except Exception:
        reachable = False
    status_text = "Connected" if reachable else "Disconnected"

    status_row = Adw.ActionRow(title="Status", subtitle=status_text)
    conn_group.add(status_row)

    server_row = Adw.EntryRow(title="Relay Server URL", text=config.server_url)
    conn_group.add(server_row)
    ctx.server_row = server_row

    # Long poll status (auto-refreshes every 3s)
    poll_status_file = ctx.config_dir / "poll_status.json"
    ctx.poll_status_file = poll_status_file
    lp_labels = {"active": "Active", "unavailable": "Not available", "testing": "Testing (may take up to 30s)...", "offline": "Offline", "unknown": "Unknown"}
    lp_row = Adw.ActionRow(title="Long polling", subtitle="...")
    retry_btn = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
    retry_btn.add_css_class("suggested-action")
    ctx.lp_row = lp_row
    ctx.retry_btn = retry_btn

    def on_retry_lp(btn):
        poll_status_file.write_text(json.dumps({"long_poll": "testing"}))
        lp_row.set_subtitle("Testing (may take up to 30s)...")
        btn.set_visible(False)
    retry_btn.connect("clicked", on_retry_lp)
    lp_row.add_suffix(retry_btn)
    conn_group.add(lp_row)

    def refresh_lp_status():
        try:
            s = json.loads(poll_status_file.read_text()).get("long_poll", "unknown") if poll_status_file.exists() else "unknown"
        except Exception:
            s = "unknown"
        lp_row.set_subtitle(lp_labels.get(s, s))
        retry_btn.set_visible(s != "active")
        return True  # Keep timer
    refresh_lp_status()
    GLib.timeout_add(3000, refresh_lp_status)

    save_btn = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
    save_btn.add_css_class("suggested-action")
    server_row.add_suffix(save_btn)
    ctx.save_btn = save_btn

    def on_save(btn):
        new_url = server_row.get_text().strip()
        if new_url:
            config.server_url = new_url
            btn.set_label("✓ Saved")
            btn.set_sensitive(False)
            GLib.timeout_add(2000, lambda: (btn.set_label("Save"), btn.set_sensitive(True), False)[-1])

    save_btn.connect("clicked", on_save)

    return conn_group
