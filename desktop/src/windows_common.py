"""Shared helpers for the GTK4/libadwaita windows subprocess (`src.windows`)."""

from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

from .brand import APP_ID, claim_gtk_identity
from .notifications import notify


def _notify_folders_skipped(count: int) -> None:
    word = "folder" if count == 1 else "folders"
    notify(
        "Folder transport is not supported",
        f"Skipped {count} {word}. Send individual files instead.",
        icon="dialog-warning",
    )


def _make_app() -> Adw.Application:
    """Shared Adw.Application factory.

    NON_UNIQUE lets multiple window subprocesses coexist under one app_id,
    which is what makes the compositor group them into a single taskbar
    entry tagged with the brand icon."""
    claim_gtk_identity()
    return Adw.Application(application_id=APP_ID,
                            flags=Gio.ApplicationFlags.NON_UNIQUE)


def format_size(b):
    if b < 1024: return f"{b} B"
    if b < 1024 * 1024: return f"{b // 1024} KB"
    return f"{b / (1024 * 1024):.1f} MB"


def _format_bytes(b):
    if b < 1024: return f"{b} B"
    if b < 1024 * 1024: return f"{b // 1024} KB"
    if b < 1024 * 1024 * 1024: return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def _connected_device_label(device) -> str:
    name = (device.name or "").strip()
    return name if name else f"Device {device.short_id}"


def _create_device_picker(config, *, title: str, subtitle: str = ""):
    from .devices import ConnectedDeviceRegistry

    registry = ConnectedDeviceRegistry(config)
    devices = registry.list_devices()
    active_device = registry.get_active_device()
    device_labels = [_connected_device_label(device) for device in devices]
    selected_device = [None]

    if active_device is not None:
        active_id = active_device.device_id
        for index, device in enumerate(devices):
            if device.device_id == active_id:
                selected_device[0] = device
                selected_index = index
                break
        else:
            selected_index = 0
    else:
        selected_index = 0

    row = Adw.ComboRow(
        title=title,
        subtitle=subtitle,
        model=Gtk.StringList.new(device_labels or ["No paired devices"]),
    )
    row.set_sensitive(bool(devices))
    row.set_selected(selected_index)

    def on_device_changed(combo, _pspec):
        selected = combo.get_selected()
        if 0 <= selected < len(devices):
            selected_device[0] = devices[selected]
        else:
            selected_device[0] = None

    row.connect("notify::selected", on_device_changed)
    return row, selected_device, devices


def _setup_subprocess_logging(config_dir: Path) -> None:
    """Set up file logging for subprocess windows (mirrors main.py setup_logging)."""
    from .config import Config
    config = Config(config_dir)
    if not config.allow_logging:
        return
    import logging
    from logging.handlers import RotatingFileHandler
    log_dir = config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handler = RotatingFileHandler(
        log_dir / "desktop-connector.log",
        maxBytes=1_000_000, backupCount=1,
    )
    handler.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
