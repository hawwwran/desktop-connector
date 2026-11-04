"""This Device + Connected Devices + add-pair + Connection Statistics.

Pre-split this lived inline in ``on_activate`` across three adjacent
``Adw.PreferencesGroup`` blocks:

1. ``device_group = Adw.PreferencesGroup(title="This Device")``
2. ``pair_group = Adw.PreferencesGroup(title="Connected Devices")``
3. ``stats_group = Adw.PreferencesGroup(title="Connection Statistics")``
   (only when ``stats and paired_devices``)

with nested ``open_rename_dialog``, ``open_unpair_dialog`` and
``on_add_pair`` closures. The unpair flow especially is load-bearing —
it sends a ``.fn.unpair`` message to the remote first (best-effort, with
proper logging on failure) and only THEN removes the local pairing
through the registry. The ``settings_registry.unpair`` call already
clears ``active_device_id`` when it was pointing at the unpaired device.

The per-row button ``lambda _b, tid=target_id, …`` default-arg
captures are preserved verbatim — they pin the loop variables.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Pango  # noqa: E402

from ..api_client import ApiClient
from ..connection import ConnectionManager
from ..devices import (
    DeviceRegistryError,
    DuplicateDeviceNameError,
)
from ..file_manager_integration import sync_file_manager_targets
from ..windows_common import _format_bytes
from .context import SettingsContext


def build(ctx: SettingsContext) -> None:
    """Append the This-Device + Connected-Devices + Statistics groups."""
    config = ctx.config
    crypto = ctx.crypto
    win = ctx.win
    settings_registry = ctx.settings_registry
    settings_active_device = ctx.settings_active_device
    stats = ctx.stats

    # This device
    device_group = Adw.PreferencesGroup(title="This Device")
    ctx.content.append(device_group)
    device_group.add(Adw.ActionRow(title="Name", subtitle=config.device_name))
    device_group.add(Adw.ActionRow(title="Device ID", subtitle=crypto.get_device_id()[:24] + "..."))

    # Connected devices list
    pair_group = Adw.PreferencesGroup(title="Connected Devices")
    ctx.content.append(pair_group)

    paired_devices = settings_registry.list_devices()
    active_device_id = (
        settings_active_device.device_id if settings_active_device else None
    )
    ctx.paired_devices = paired_devices
    ctx.active_device_id = active_device_id

    def open_rename_dialog(target_id: str, current_name: str):
        dialog = Adw.MessageDialog(
            transient_for=win,
            heading="Rename connected device",
            body="Choose a unique name for this connected device.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        entry_group = Adw.PreferencesGroup()
        name_entry = Adw.EntryRow(title="Name")
        name_entry.set_text(current_name)
        entry_group.add(name_entry)

        error_label = Gtk.Label(label="")
        error_label.add_css_class("error")
        error_label.set_wrap(True)
        error_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        error_label.set_xalign(0)
        error_label.set_visible(False)
        error_label.set_margin_top(8)

        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        extra.append(entry_group)
        extra.append(error_label)
        dialog.set_extra_child(extra)

        def on_response(dlg, response):
            if response != "save":
                dlg.close()
                return
            try:
                settings_registry.rename(target_id, name_entry.get_text())
            except DuplicateDeviceNameError:
                error_label.set_label(
                    "This name is already used by another device."
                )
                error_label.set_visible(True)
                return
            except DeviceRegistryError:
                error_label.set_label("Name cannot be empty.")
                error_label.set_visible(True)
                return
            try:
                sync_file_manager_targets(config)
            except Exception:
                pass
            dlg.close()
            win.close()

        dialog.connect("response", on_response)
        # Block default close-on-response so validation can keep the
        # dialog open when the user enters a duplicate or empty name.
        dialog.set_close_response("cancel")
        dialog.present()

    def open_unpair_dialog(target_id: str, target_name: str, target_info: dict):
        dialog = Adw.MessageDialog(
            transient_for=win,
            heading="Unpair?",
            body=f"Disconnect from \"{target_name}\"?\nYou will need to pair again.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("unpair", "Unpair")
        dialog.set_response_appearance("unpair", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dlg, response):
            if response != "unpair":
                return
            # Notify only this specific device. The remote side
            # mirrors the unpair via .fn.unpair so both sides stay
            # in sync; if the notify hops fail (offline, auth lost,
            # quota), log loudly — the local pairing is still
            # removed below, leaving the remote believing it's
            # paired until they hit a 403 of their own.
            import logging
            import os as _os
            import tempfile
            log = logging.getLogger("desktop-connector.settings")
            tmp_path: Path | None = None
            try:
                sym_key = base64.b64decode(target_info["symmetric_key_b64"])
                fd, tmp_name = tempfile.mkstemp(suffix="_.fn.unpair")
                tmp_path = Path(tmp_name)
                with _os.fdopen(fd, "wb") as fh:
                    fh.write(b"unpair")
                conn_tmp = ConnectionManager(
                    config.server_url,
                    config.device_id or "",
                    config.auth_token or "",
                )
                api_tmp = ApiClient(conn_tmp, crypto)
                api_tmp.send_file(
                    tmp_path, target_id, sym_key, filename_override=".fn.unpair"
                )
            except Exception:
                log.warning(
                    "pairing.unpair.notify_failed peer=%s",
                    target_id[:12],
                    exc_info=True,
                )
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        log.debug(
                            "pairing.unpair.tmp_cleanup_failed",
                            exc_info=True,
                        )
            # Remove only this pairing through Config so the keyring
            # entry is cleaned up alongside JSON metadata. The
            # registry helper also clears active_device_id when it
            # was pointing at the unpaired device.
            settings_registry.unpair(target_id)
            try:
                sync_file_manager_targets(config)
            except Exception:
                pass
            win.close()

        dialog.connect("response", on_response)
        dialog.present()

    if paired_devices:
        for device in paired_devices:
            target_id = device.device_id
            target_name = device.name or "Unknown"
            if target_id == active_device_id:
                subtitle = f"{target_id[:24]}…  ·  Active"
            else:
                subtitle = f"{target_id[:24]}…"
            row = Adw.ActionRow(title=target_name, subtitle=subtitle)

            rename_btn = Gtk.Button(
                label="Rename", valign=Gtk.Align.CENTER,
            )
            rename_btn.add_css_class("flat")
            rename_btn.connect(
                "clicked",
                lambda _b, tid=target_id, nm=target_name: open_rename_dialog(tid, nm),
            )
            row.add_suffix(rename_btn)

            target_info = config.paired_devices.get(target_id, {})
            unpair_btn = Gtk.Button(
                label="Unpair", valign=Gtk.Align.CENTER,
            )
            unpair_btn.add_css_class("destructive-action")
            unpair_btn.connect(
                "clicked",
                lambda _b, tid=target_id, nm=target_name, info=target_info:
                    open_unpair_dialog(tid, nm, info),
            )
            row.add_suffix(unpair_btn)

            pair_group.add(row)
    else:
        pair_group.add(Adw.ActionRow(
            title="No connected devices",
            subtitle="Use Pair… from the tray menu",
        ))

    # Add-pairing entry-point. Mirrors Android's "Pair with another
    # desktop" row in PairingsCard. Spawning the pairing window
    # from here works whether or not the user already has a pair.
    add_pair_row = Adw.ActionRow(
        title="Pair another device",
        subtitle="Open the QR code window to add a new connected device",
        activatable=True,
    )
    add_pair_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
    add_pair_row.add_prefix(add_pair_icon)
    add_pair_row.add_suffix(
        Gtk.Image.new_from_icon_name("go-next-symbolic"),
    )

    def on_add_pair(_row):
        import os as _os
        import subprocess as _subprocess
        import sys as _sys
        appimage = _os.environ.get("APPIMAGE")
        cmd = (
            [appimage, "--gtk-window=pairing",
             f"--config-dir={config.config_dir}"]
            if appimage else
            [_sys.executable, "-m", "src.windows", "pairing",
             f"--config-dir={config.config_dir}"]
        )
        cwd = (None if appimage
               else str(Path(__file__).resolve().parent.parent.parent))
        _subprocess.Popen(cmd, cwd=cwd)
        win.close()

    add_pair_row.connect("activated", on_add_pair)
    pair_group.add(add_pair_row)

    # Connection statistics (only when at least one pair + stats fetched)
    if stats and paired_devices:
        stats_group = Adw.PreferencesGroup(title="Connection Statistics")
        ctx.content.append(stats_group)

        paired_devs_stats = stats.get("paired_devices", [])
        stats_by_id = {
            d.get("device_id"): d for d in paired_devs_stats if d.get("device_id")
        }
        for device in paired_devices:
            pd = stats_by_id.get(device.device_id)
            if not pd:
                continue
            online = pd.get("online", False)
            last_seen = pd.get("last_seen", 0)
            if online:
                status_str = "Online"
            elif last_seen:
                ago = int(time.time()) - last_seen
                if ago < 60:
                    status_str = "Last seen just now"
                elif ago < 3600:
                    status_str = f"Last seen {ago // 60} min ago"
                elif ago < 86400:
                    status_str = f"Last seen {ago // 3600}h ago"
                else:
                    status_str = f"Last seen {time.strftime('%b %d, %H:%M', time.localtime(last_seen))}"
            else:
                status_str = "Offline"
            pair_label = device.name or device.device_id[:12]
            stats_group.add(Adw.ActionRow(
                title=f"{pair_label} — status",
                subtitle=status_str,
            ))
            stats_group.add(Adw.ActionRow(
                title=f"{pair_label} — transfers",
                subtitle=str(pd.get("transfers", 0)),
            ))
            stats_group.add(Adw.ActionRow(
                title=f"{pair_label} — data",
                subtitle=_format_bytes(pd.get("bytes_transferred", 0)),
            ))
            paired_since = pd.get("paired_since", 0)
            if paired_since:
                stats_group.add(Adw.ActionRow(
                    title=f"{pair_label} — paired since",
                    subtitle=time.strftime("%b %d, %Y", time.localtime(paired_since)),
                ))

        stats_group.add(Adw.ActionRow(
            title="Pending incoming",
            subtitle=str(stats.get("pending_incoming", 0)),
        ))
        stats_group.add(Adw.ActionRow(
            title="Pending outgoing",
            subtitle=str(stats.get("pending_outgoing", 0)),
        ))
