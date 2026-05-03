"""Settings window (`python -m src.windows settings`)."""

import base64
import json
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .windows_common import _format_bytes, _make_app


def show_settings(config_dir: Path):
    import os as _os
    from .config import (
        Config,
        DEFAULT_RECEIVE_ACTION_LIMITS,
        RECEIVE_ACTION_COPY,
        RECEIVE_ACTION_KEY_DOCUMENT_OPEN,
        RECEIVE_ACTION_KEY_IMAGE_OPEN,
        RECEIVE_ACTION_KEY_TEXT_COPY,
        RECEIVE_ACTION_KEY_URL_COPY,
        RECEIVE_ACTION_KEY_URL_OPEN,
        RECEIVE_ACTION_KEY_VIDEO_OPEN,
        RECEIVE_ACTION_LIMIT_BATCH,
        RECEIVE_ACTION_LIMIT_MAX,
        RECEIVE_ACTION_LIMIT_MINUTE,
        RECEIVE_ACTION_NONE,
        RECEIVE_ACTION_OPEN,
        RECEIVE_KIND_DOCUMENT,
        RECEIVE_KIND_IMAGE,
        RECEIVE_KIND_TEXT,
        RECEIVE_KIND_URL,
        RECEIVE_KIND_VIDEO,
    )
    from .crypto import KeyManager
    from .connection import ConnectionManager, ConnectionState
    from .api_client import ApiClient
    from .bootstrap.app_version import get_app_version

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")

    # Fetch stats from server
    from .devices import (
        ConnectedDeviceRegistry,
        DeviceRegistryError,
        DuplicateDeviceNameError,
    )
    from .file_manager_integration import sync_file_manager_targets

    stats = None
    settings_registry = ConnectedDeviceRegistry(config)
    settings_active_device = settings_registry.get_active_device()
    try:
        api = ApiClient(conn, crypto)
        stats = api.get_stats(
            paired_with=settings_active_device.device_id
            if settings_active_device else None,
        )
    except Exception:
        pass

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Settings", default_width=630, default_height=624)
        win.set_resizable(True)

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

        # Connection
        conn_group = Adw.PreferencesGroup(title="Connection")
        content.append(conn_group)

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

        # Long poll status (auto-refreshes every 3s)
        poll_status_file = config_dir / "poll_status.json"
        lp_labels = {"active": "Active", "unavailable": "Not available", "testing": "Testing (may take up to 30s)...", "offline": "Offline", "unknown": "Unknown"}
        lp_row = Adw.ActionRow(title="Long polling", subtitle="...")
        retry_btn = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
        retry_btn.add_css_class("suggested-action")
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

        def on_save(btn):
            new_url = server_row.get_text().strip()
            if new_url:
                config.server_url = new_url
                btn.set_label("✓ Saved")
                btn.set_sensitive(False)
                GLib.timeout_add(2000, lambda: (btn.set_label("Save"), btn.set_sensitive(True), False)[-1])

        save_btn.connect("clicked", on_save)

        # Appearance
        appearance_group = Adw.PreferencesGroup(title="Appearance")
        content.append(appearance_group)

        theme_modes = (
            ("System", "system"),
            ("Light", "light"),
            ("Dark", "dark"),
        )
        theme_model = Gtk.StringList.new([label for label, _ in theme_modes])
        theme_row = Adw.ComboRow(
            title="Theme",
            subtitle="Match desktop, or force light / dark mode.",
            model=theme_model,
        )
        current_mode = config.theme_mode
        for idx, (_, value) in enumerate(theme_modes):
            if value == current_mode:
                theme_row.set_selected(idx)
                break

        def on_theme_changed(combo, _pspec, modes=theme_modes):
            i = combo.get_selected()
            if 0 <= i < len(modes):
                new_mode = modes[i][1]
                if new_mode != config.theme_mode:
                    config.theme_mode = new_mode
                    # Live-apply to this window so the change is visible
                    # without a restart. Other open subprocesses pick it
                    # up on their next reload (config.json is the source
                    # of truth).
                    from .brand import apply_theme_mode
                    apply_theme_mode(new_mode)

        theme_row.connect("notify::selected", on_theme_changed)
        appearance_group.add(theme_row)

        # ---- Vault section (T3.3) ----
        # T0 §D16: a small "Vault" group with the active toggle and an
        # "Open Vault settings…" button. Toggle is ON by default on
        # fresh install; OFF hides the tray submenu and pauses sync
        # without destroying any data.
        from .vault_ui_state import vault_settings_button_state

        vault_group = Adw.PreferencesGroup(title="Vault")
        content.append(vault_group)

        vault_active_row = Adw.SwitchRow(
            title="Vault active",
            subtitle="Show Vault in tray menu and run sync. OFF is reversible — keys, manifests, and downloaded data are preserved.",
            active=config.vault_active,
        )
        vault_group.add(vault_active_row)

        open_vault_row = Adw.ActionRow(
            title="Open Vault settings…",
            subtitle="Opens the deep-config window. Disabled when Vault is inactive.",
        )
        vault_group.add(open_vault_row)
        open_vault_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_vault_btn.add_css_class("pill")
        open_vault_row.add_suffix(open_vault_btn)

        def vault_exists_locally() -> bool:
            raw = config._data.get("vault")
            return isinstance(raw, dict) and bool(raw.get("last_known_id"))

        def refresh_vault_button():
            state = vault_settings_button_state(
                toggle_active=config.vault_active,
                vault_exists=vault_exists_locally(),
            )
            open_vault_btn.set_sensitive(state.enabled)

        def on_vault_toggled(switch, _pspec):
            new_value = switch.get_active()
            if new_value != config.vault_active:
                config.vault_active = new_value
            refresh_vault_button()

        def on_open_vault_clicked(_btn):
            state = vault_settings_button_state(
                toggle_active=config.vault_active,
                vault_exists=vault_exists_locally(),
            )
            target = None
            if state.action == "launch_wizard":
                target = "vault-onboard"
            elif state.action == "launch_settings":
                target = "vault-main"
            if target is None:
                return
            import os as _os
            import subprocess as _subprocess
            import sys as _sys
            appimage = _os.environ.get("APPIMAGE")
            cmd = (
                [appimage, f"--gtk-window={target}",
                 f"--config-dir={config.config_dir}"]
                if appimage else
                [_sys.executable, "-m", "src.windows", target,
                 f"--config-dir={config.config_dir}"]
            )
            cwd = (None if appimage
                   else str(Path(__file__).resolve().parent.parent))
            _subprocess.Popen(cmd, cwd=cwd)

        vault_active_row.connect("notify::active", on_vault_toggled)
        open_vault_btn.connect("clicked", on_open_vault_clicked)
        refresh_vault_button()

        # Receive actions
        receive_group = Adw.PreferencesGroup(title="Receive Actions")
        content.append(receive_group)

        receive_action_rows = (
            (
                RECEIVE_KIND_URL,
                "URL",
                "What to do when received text is detected as a URL.",
                (
                    ("Open in default browser", RECEIVE_ACTION_OPEN),
                    ("Copy to clipboard", RECEIVE_ACTION_COPY),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_TEXT,
                "Text",
                "What to do after receiving text that is not only a URL.",
                (
                    ("Copy to clipboard", RECEIVE_ACTION_COPY),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_IMAGE,
                "Image",
                "What to do after receiving an image file.",
                (
                    ("Open in default image viewer", RECEIVE_ACTION_OPEN),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_VIDEO,
                "Video",
                "What to do after receiving a video file.",
                (
                    ("Open in default video viewer", RECEIVE_ACTION_OPEN),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
            (
                RECEIVE_KIND_DOCUMENT,
                "Document",
                "What to do after receiving a document file.",
                (
                    ("Open in default document viewer", RECEIVE_ACTION_OPEN),
                    ("No action", RECEIVE_ACTION_NONE),
                ),
            ),
        )

        for kind, title, subtitle, options in receive_action_rows:
            labels = [label for label, _action in options]
            actions = [action for _label, action in options]
            row = Adw.ComboRow(
                title=title,
                subtitle=subtitle,
                model=Gtk.StringList.new(labels),
            )
            current_action = config.get_receive_action(kind)
            try:
                row.set_selected(actions.index(current_action))
            except ValueError:
                row.set_selected(0)

            def on_receive_action_changed(combo, _pspec, k=kind, acts=actions):
                selected = combo.get_selected()
                if 0 <= selected < len(acts):
                    config.set_receive_action(k, acts[selected])

            row.connect("notify::selected", on_receive_action_changed)
            receive_group.add(row)

        # Receive action flood protection
        flood_group = Adw.PreferencesGroup(title="Receive Action Flood Protection")
        content.append(flood_group)

        reset_row = Adw.ActionRow(
            title="Flood limits",
            subtitle="0 means unlimited",
        )
        reset_btn = Gtk.Button(label="Reset to defaults", valign=Gtk.Align.CENTER)
        reset_row.add_suffix(reset_btn)
        flood_group.add(reset_row)

        limit_spinbuttons: dict[tuple[str, str], Gtk.SpinButton] = {}

        def make_limit_spin(action_key: str, limit_name: str) -> Gtk.SpinButton:
            value = config.get_receive_action_limits(action_key)[limit_name]
            adjustment = Gtk.Adjustment(
                value=float(value),
                lower=0.0,
                upper=float(RECEIVE_ACTION_LIMIT_MAX),
                step_increment=1.0,
                page_increment=10.0,
            )
            spin = Gtk.SpinButton(
                adjustment=adjustment,
                climb_rate=1.0,
                digits=0,
                numeric=True,
                valign=Gtk.Align.CENTER,
            )
            spin.set_width_chars(3)

            def on_limit_changed(widget, _action_key=action_key, _limit_name=limit_name):
                config.set_receive_action_limit(
                    _action_key,
                    _limit_name,
                    int(widget.get_value()),
                )

            spin.connect("value-changed", on_limit_changed)
            limit_spinbuttons[(action_key, limit_name)] = spin
            return spin

        receive_action_limit_rows = (
            (RECEIVE_ACTION_KEY_URL_OPEN, "Open URL"),
            (RECEIVE_ACTION_KEY_URL_COPY, "Copy URL to clipboard"),
            (RECEIVE_ACTION_KEY_TEXT_COPY, "Copy text to clipboard"),
            (RECEIVE_ACTION_KEY_IMAGE_OPEN, "Open image"),
            (RECEIVE_ACTION_KEY_VIDEO_OPEN, "Open video"),
            (RECEIVE_ACTION_KEY_DOCUMENT_OPEN, "Open document"),
        )

        limits_table = Gtk.Grid(
            column_spacing=16,
            row_spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        flood_group.add(limits_table)

        table_headers = ("Action type", "Max per batch", "Max per minute")
        for column, header in enumerate(table_headers):
            label = Gtk.Label(label=header, xalign=0.0)
            label.add_css_class("dim-label")
            label.add_css_class("caption-heading")
            limits_table.attach(label, column, 0, 1, 1)

        for row_index, (action_key, title) in enumerate(receive_action_limit_rows, start=1):
            action_label = Gtk.Label(
                label=title,
                xalign=0.0,
                valign=Gtk.Align.CENTER,
                hexpand=True,
            )
            limits_table.attach(action_label, 0, row_index, 1, 1)
            limits_table.attach(
                make_limit_spin(action_key, RECEIVE_ACTION_LIMIT_BATCH),
                1,
                row_index,
                1,
                1,
            )
            limits_table.attach(
                make_limit_spin(action_key, RECEIVE_ACTION_LIMIT_MINUTE),
                2,
                row_index,
                1,
                1,
            )

        def on_reset_limits(btn):
            config.reset_receive_action_limits()
            for action_key, limits in DEFAULT_RECEIVE_ACTION_LIMITS.items():
                for limit_name, value in limits.items():
                    spin = limit_spinbuttons.get((action_key, limit_name))
                    if spin is not None:
                        spin.set_value(float(value))
            btn.set_label("✓ Reset")
            btn.set_sensitive(False)

            def restore_reset_label() -> bool:
                btn.set_label("Reset to defaults")
                btn.set_sensitive(True)
                return False

            GLib.timeout_add(2000, restore_reset_label)

        reset_btn.connect("clicked", on_reset_limits)

        def add_logs_group():
            logs_group = Adw.PreferencesGroup(title="Logs")
            content.append(logs_group)

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

        # This device
        device_group = Adw.PreferencesGroup(title="This Device")
        content.append(device_group)
        device_group.add(Adw.ActionRow(title="Name", subtitle=config.device_name))
        device_group.add(Adw.ActionRow(title="Device ID", subtitle=crypto.get_device_id()[:24] + "..."))

        # Connected devices list
        pair_group = Adw.PreferencesGroup(title="Connected Devices")
        content.append(pair_group)

        paired_devices = settings_registry.list_devices()
        active_device_id = (
            settings_active_device.device_id if settings_active_device else None
        )

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
                   else str(Path(__file__).resolve().parent.parent))
            _subprocess.Popen(cmd, cwd=cwd)
            win.close()

        add_pair_row.connect("activated", on_add_pair)
        pair_group.add(add_pair_row)

        # Connection statistics (only when at least one pair + stats fetched)
        if stats and paired_devices:
            stats_group = Adw.PreferencesGroup(title="Connection Statistics")
            content.append(stats_group)

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

        # Security: verify secret storage (H.6). Surfaces the active
        # backend's status and gives the user a way to re-scrub
        # plaintext after manual config.json edits or a partial-failure
        # boot. Settings open already triggers Config init's automatic
        # migration, so on first display the row almost always reads
        # "Already clean".
        sec_group = Adw.PreferencesGroup(title="Security")
        content.append(sec_group)

        _secure_now = config.is_secret_storage_secure()
        verify_row = Adw.ActionRow(
            title="Secret storage",
            subtitle=(
                "Identity, auth token + pairing keys in OS keyring "
                "(libsecret / KWallet)"
                if _secure_now else
                "Identity, auth token + pairing keys in plaintext "
                "~/.config/desktop-connector/ (no keyring backend "
                "reachable)"
            ),
        )
        sec_group.add(verify_row)

        # Info icon: tooltip on hover, click opens an explainer dialog.
        info_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        info_btn.set_child(Gtk.Image.new_from_icon_name(
            "dialog-information-symbolic",
        ))
        info_btn.add_css_class("flat")
        info_btn.add_css_class("circular")
        info_btn.set_tooltip_text(
            "Verify scans config.json for plaintext secrets and "
            "migrates them into the OS keyring. Click for details."
        )

        def _on_secret_info(_btn):
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="About verify secret storage",
                body=(
                    "Verify scans for plaintext secret material and "
                    "moves anything it finds into the OS keyring. "
                    "Three locations are checked:\n\n"
                    "  • config.json → auth_token field\n"
                    "  • config.json → paired_devices[*].symmetric_key_b64\n"
                    "  • keys/private_key.pem → long-term device "
                    "identity key\n\n"
                    "Useful when:\n"
                    "  • You've manually edited config.json or "
                    "restored a private_key.pem from backup\n"
                    "  • A previous launch's automatic migration "
                    "failed (e.g. the keyring was locked at startup)\n"
                    "  • You want to confirm secrets are stored "
                    "correctly\n\n"
                    "Verify does NOT change cryptographic identity, "
                    "doesn't re-pair, and doesn't rotate keys. It "
                    "only ensures plaintext doesn't accumulate "
                    "outside the keyring."
                ),
            )
            dialog.add_response("close", "Close")
            dialog.set_default_response("close")
            dialog.present()

        info_btn.connect("clicked", _on_secret_info)
        verify_row.add_suffix(info_btn)

        verify_btn = Gtk.Button(label="Verify", valign=Gtk.Align.CENTER)
        verify_btn.add_css_class("pill")

        def _on_verify_secret_storage(_btn):
            result = config.scrub_secrets()
            # H.7 also covers the private-key PEM. crypto already
            # ran a migration check in __init__ (window-spawn time);
            # re-running here picks up anything that arrived since.
            pem_scrubbed = (
                crypto.scrub_private_key() or crypto.was_pem_migrated
            )

            if not result.secure:
                verify_row.set_subtitle(
                    "Plaintext fallback active — no scrub possible. "
                    "Install gnome-keyring or kwallet and re-launch."
                )
                return
            if result.failed > 0:
                verify_row.set_subtitle(
                    f"Scrubbed {result.scrubbed}, {result.failed} "
                    "field(s) remain (keyring transient — re-try)"
                )
                return

            items: list[str] = []
            if result.scrubbed > 0:
                items.append(f"{result.scrubbed} field(s)")
            if pem_scrubbed:
                items.append("device private key")
            if items:
                verify_row.set_subtitle(
                    "✓ Scrubbed " + " + ".join(items) +
                    " into the keyring"
                )
            else:
                verify_row.set_subtitle(
                    "✓ Already clean — identity, auth token + "
                    "pairing keys all in keyring"
                )

        verify_btn.connect("clicked", _on_verify_secret_storage)
        verify_row.add_suffix(verify_btn)

        add_logs_group()

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
