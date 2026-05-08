"""Logs group: allow-logging toggle, download + clear buttons.

Pre-split this lived inline in ``on_activate`` as the
``add_logs_group()`` helper (defined early but called late, after the
Connection Statistics block, so the layout order is preserved). The
``on_download_logs`` and ``on_clear_logs`` closures keep their
``GLib.timeout_add`` UI-recovery cadences (3 s for downloads, 2 s for
clear).
"""

from __future__ import annotations

import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

from ..windows_common import _format_bytes
from .context import SettingsContext


def build(ctx: SettingsContext) -> Adw.PreferencesGroup:
    """Mirrors the original ``add_logs_group()``."""
    config = ctx.config
    config_dir = ctx.config_dir

    logs_group = Adw.PreferencesGroup(title="Logs")
    ctx.content.append(logs_group)

    log_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
    log_switch.set_active(config.allow_logging)
    log_switch.connect("notify::active", lambda sw, _: setattr(config, 'allow_logging', sw.get_active()))
    log_toggle_row = Adw.ActionRow(title="Allow logging", subtitle="Write logs to file (requires restart)")
    log_toggle_row.add_suffix(log_switch)
    log_toggle_row.set_activatable_widget(log_switch)
    logs_group.add(log_toggle_row)

    log_dir = config_dir / "logs"
    log_files = sorted(log_dir.glob("desktop-connector.log*")) if log_dir.exists() else []
    total_size = sum(f.stat().st_size for f in log_files) if log_files else 0
    log_size_text = _format_bytes(total_size) if total_size > 0 else "No logs"

    log_row = Adw.ActionRow(title="Log files", subtitle=log_size_text)
    download_btn = Gtk.Button(label="Download Logs", valign=Gtk.Align.CENTER)

    def on_download_logs(btn):
        import shutil, subprocess as _sp
        downloads = Path.home() / "Downloads"
        downloads.mkdir(exist_ok=True)
        dest = downloads / f"desktop-connector-logs-{time.strftime('%Y%m%d-%H%M%S')}"
        dest.mkdir(exist_ok=True)
        copied = 0
        for f in (log_dir.glob("desktop-connector.log*") if log_dir.exists() else []):
            shutil.copy2(f, dest / f.name)
            copied += 1
        if copied > 0:
            _sp.Popen(["xdg-open", str(dest)])
            btn.set_label(f"✓ Saved to Downloads")
            btn.set_sensitive(False)
            GLib.timeout_add(3000, lambda: (btn.set_label("Download Logs"), btn.set_sensitive(True), False)[-1])
        else:
            btn.set_label("No logs found")
            GLib.timeout_add(2000, lambda: (btn.set_label("Download Logs"), btn.set_sensitive(True), False)[-1])

    download_btn.connect("clicked", on_download_logs)
    log_row.add_suffix(download_btn)

    clear_btn = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
    clear_btn.add_css_class("destructive-action")

    def on_clear_logs(btn):
        cleared = 0
        # Truncate (don't unlink) so any running logger keeps its file handle.
        for f in (log_dir.glob("desktop-connector.log*") if log_dir.exists() else []):
            try:
                f.open("w").close()
                cleared += 1
            except OSError:
                pass
        log_row.set_subtitle("No logs")
        if cleared > 0:
            btn.set_label("✓ Cleared")
        else:
            btn.set_label("No logs found")
        btn.set_sensitive(False)
        GLib.timeout_add(2000, lambda: (btn.set_label("Clear"), btn.set_sensitive(True), False)[-1])

    clear_btn.connect("clicked", on_clear_logs)
    log_row.add_suffix(clear_btn)
    logs_group.add(log_row)

    return logs_group
