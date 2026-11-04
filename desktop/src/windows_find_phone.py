"""Find my Device + locate-alert windows.

`python -m src.windows find-phone` opens the locator UI on this desktop
to ring/locate a paired phone. `python -m src.windows locate-alert`
opens a modal stop-button window on this desktop when another device
locates it.
"""

import base64
import json
import threading
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
from .windows_common import _create_device_picker, _make_app


def show_find_phone(config_dir: Path):
    import logging
    log = logging.getLogger("desktop-connector.find-phone")

    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .devices import ConnectedDeviceRegistry, DeviceRegistryError
    from .messaging import FasttrackAdapter, MessageType

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)

    def decode_target_find_device_update(raw: dict, target_id: str, symmetric_key: bytes):
        if (raw.get("sender_id") or "") != target_id:
            return None
        mid = raw.get("id")
        enc_data = raw.get("encrypted_data", "")
        try:
            enc_bytes = base64.b64decode(enc_data)
            plain = crypto.decrypt_blob(enc_bytes, symmetric_key)
            resp = json.loads(plain)
        except Exception as exc:
            log.error("Decrypt failed: %s", exc)
            return None
        if not isinstance(resp, dict):
            return None
        msg = FasttrackAdapter.to_device_message(resp)
        if not msg or msg.type != MessageType.FIND_PHONE_LOCATION_UPDATE:
            return None
        return mid, resp

    # Check WebKit availability
    has_webkit = False
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit
        has_webkit = True
    except (ValueError, ImportError):
        pass

    MAP_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9/dist/leaflet.js"></script>
<style>body{margin:0;background:#1e1e1e}#map{width:100%;height:100vh}</style>
</head><body>
<div id="map"></div>
<script>
var map = L.map('map').setView([0,0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'OSM', maxZoom:19}).addTo(map);
var marker = null;
var circle = null;
function updatePos(lat,lng,acc) {
  if (!marker) {
    marker = L.marker([lat,lng]).addTo(map);
    map.setView([lat,lng], 16);
  } else {
    marker.setLatLng([lat,lng]);
    map.panTo([lat,lng]);
  }
  if (circle) map.removeLayer(circle);
  if (acc && acc > 0) {
    circle = L.circle([lat,lng], {radius:acc, color:'#3986FC',
      fillColor:'#3986FC', fillOpacity:0.15, weight:1}).addTo(map);
  }
}
</script>
</body></html>"""

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Find my Device",
                                     default_width=480, default_height=640)

        toolbar_view = Adw.ToolbarView()
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(toolbar_view)
        win.set_content(toast_overlay)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        toolbar_view.set_content(content)

        # Status
        status_label = Gtk.Label(label="Ready")
        status_label.add_css_class("title-3")
        content.append(status_label)

        # Connected-device picker
        device_picker, selected_device, paired_devices = _create_device_picker(
            config,
            title="Find my Device",
            subtitle="Connected device",
        )
        device_group = Adw.PreferencesGroup()
        device_group.add(device_picker)
        content.append(device_group)

        # Settings group
        settings_group = Adw.PreferencesGroup(title="Settings")
        content.append(settings_group)

        # Silent search toggle
        silent_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        silent_switch.set_active(False)
        silent_row = Adw.ActionRow(title="Silent search", subtitle="Track location without alarm (stolen device)")
        silent_row.add_suffix(silent_switch)
        silent_row.set_activatable_widget(silent_switch)
        settings_group.add(silent_row)

        # Volume slider
        volume_row = Adw.ActionRow(title="Volume")
        volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 100, 10)
        volume_scale.set_value(80)
        volume_scale.set_hexpand(True)
        volume_scale.set_valign(Gtk.Align.CENTER)
        volume_scale.set_draw_value(True)
        volume_scale.set_value_pos(Gtk.PositionType.RIGHT)
        volume_row.add_suffix(volume_scale)
        settings_group.add(volume_row)

        def on_silent_changed(sw, _):
            volume_scale.set_sensitive(not sw.get_active())
        silent_switch.connect("notify::active", on_silent_changed)

        # Action buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          halign=Gtk.Align.CENTER, margin_top=4)
        content.append(btn_box)

        start_btn = Gtk.Button(label="Start")
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.set_sensitive(selected_device[0] is not None)
        btn_box.append(start_btn)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("pill")
        stop_btn.set_visible(False)
        btn_box.append(stop_btn)

        def on_picker_changed(_combo, _pspec):
            # Idle states gate Start on a selection; while locating, the
            # picker is locked anyway so this stays a no-op.
            start_btn.set_sensitive(selected_device[0] is not None)
        device_picker.connect("notify::selected", on_picker_changed)

        # Map or fallback
        webview = [None]

        if has_webkit:
            from gi.repository import WebKit
            wv = WebKit.WebView()
            wv.set_vexpand(True)
            wv.set_hexpand(True)
            wv.set_size_request(-1, 250)
            wv.load_html(MAP_HTML, "about:blank")
            map_frame = Gtk.Frame()
            map_frame.set_child(wv)
            map_frame.set_overflow(Gtk.Overflow.HIDDEN)
            content.append(map_frame)
            webview[0] = wv
        else:
            map_placeholder = Gtk.Label(label="Map unavailable (install gir1.2-webkit-6.0)")
            map_placeholder.add_css_class("dim-label")
            map_placeholder.set_vexpand(True)
            content.append(map_placeholder)

        # Location info + open in browser
        loc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.append(loc_box)

        loc_label = Gtk.Label(label="", xalign=0, hexpand=True)
        loc_label.add_css_class("caption")
        loc_label.add_css_class("dim-label")
        loc_box.append(loc_label)

        open_map_btn = Gtk.Button(label="Open in Browser")
        open_map_btn.add_css_class("flat")
        open_map_btn.set_visible(False)
        loc_box.append(open_map_btn)

        # ── State ─────────────────────────────────────────────────
        # poll_generation: incremented on each Start. Old poll threads see mismatch and exit.
        # No shared mutable flags — eliminates all thread races.
        poll_generation = [0]
        last_lat = [None]
        last_lng = [None]
        is_silent = [False]
        shared_api = [None]
        shared_target = [None]
        shared_key = [None]

        LOST_COMMS_TIMEOUT = 20  # seconds with no heartbeat

        def set_ui(status_text, sliders_enabled, show_start, show_stop):
            status_label.set_text(status_text)
            volume_scale.set_sensitive(sliders_enabled and not silent_switch.get_active())
            silent_row.set_sensitive(sliders_enabled)
            # Picker locks while a session is in progress so the user
            # can't switch targets mid-locate. Re-enabling needs paired
            # devices to exist; otherwise the empty-list picker stays
            # insensitive.
            device_picker.set_sensitive(sliders_enabled and bool(paired_devices))
            start_btn.set_visible(show_start)
            start_btn.set_sensitive(
                show_start and not show_stop and selected_device[0] is not None
            )
            stop_btn.set_visible(show_stop)

        def update_location(lat, lng, accuracy):
            last_lat[0] = lat
            last_lng[0] = lng
            if lat is not None and lng is not None:
                acc_text = f"  |  ~{int(accuracy)}m" if accuracy else ""
                loc_label.set_text(f"{lat:.6f}, {lng:.6f}{acc_text}  |  {time.strftime('%H:%M:%S')}")
                open_map_btn.set_visible(True)
                if webview[0]:
                    acc_val = accuracy if accuracy else 0
                    webview[0].evaluate_javascript(
                        f"updatePos({lat},{lng},{acc_val})", -1, None, None, None, None, None)

        def on_open_map(btn):
            if last_lat[0] is not None:
                import subprocess
                url = f"https://www.openstreetmap.org/?mlat={last_lat[0]}&mlon={last_lng[0]}#map=16/{last_lat[0]}/{last_lng[0]}"
                subprocess.Popen(["xdg-open", url])
        open_map_btn.connect("clicked", on_open_map)

        def _send_stop(api, target_id, symmetric_key):
            payload = json.dumps({"fn": "find-phone", "action": "stop"}).encode()
            encrypted = crypto.encrypt_blob(payload, symmetric_key)
            encrypted_b64 = base64.b64encode(encrypted).decode()
            log.info("fasttrack.command.sent fn=find-phone action=stop recipient=%s", target_id[:12])
            api.fasttrack_send(target_id, encrypted_b64)

        def on_start(btn):
            target = selected_device[0]
            if target is None:
                toast_overlay.add_toast(Adw.Toast(title="No connected device selected", timeout=3))
                return
            if not target.symmetric_key_b64:
                toast_overlay.add_toast(Adw.Toast(
                    title="Cannot locate — pairing key missing for this device",
                    timeout=3,
                ))
                return

            target_id = target.device_id
            symmetric_key = base64.b64decode(target.symmetric_key_b64)
            volume = 0 if silent_switch.get_active() else int(volume_scale.get_value())
            is_silent[0] = silent_switch.get_active()

            # Advance generation — any old poll thread will see mismatch and exit
            poll_generation[0] += 1
            my_gen = poll_generation[0]

            set_ui("Sending command...", False, False, True)

            payload = json.dumps({
                "fn": "find-phone",
                "action": "start",
                "volume": volume,
                "timeout": 300,  # hardcoded 5 min, enforced on phone
            }).encode()
            encrypted = crypto.encrypt_blob(payload, symmetric_key)
            encrypted_b64 = base64.b64encode(encrypted).decode()

            def do_poll():
                conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")
                api = ApiClient(conn, crypto)
                shared_api[0] = api
                shared_target[0] = target_id
                shared_key[0] = symmetric_key

                # Flush only stale sender-side updates from this target.
                # Other pending fasttrack messages belong to the tray receiver.
                stale = api.fasttrack_pending()
                flushed_count = 0
                for m in stale:
                    decoded = decode_target_find_device_update(m, target_id, symmetric_key)
                    if decoded is None:
                        continue
                    mid, _resp = decoded
                    if mid:
                        api.fasttrack_ack(mid)
                        flushed_count += 1
                if flushed_count:
                    log.info("fasttrack.message.flushed_stale count=%d", flushed_count)

                log.info("fasttrack.command.sent fn=find-phone action=start volume=%d silent=%s recipient=%s",
                         volume, is_silent[0], target_id[:12])
                msg_id = api.fasttrack_send(target_id, encrypted_b64)
                if msg_id is None:
                    log.error("fasttrack.command.send_failed fn=find-phone")
                    GLib.idle_add(set_ui, "Failed to reach device", True, True, False)
                    return

                # D2: marking active happens only after a directed
                # device action is successfully queued.
                try:
                    ConnectedDeviceRegistry(config).mark_active(
                        target_id, reason="find_device_start",
                    )
                except DeviceRegistryError:
                    pass

                log.debug("fasttrack.command.polling message_id=%s", msg_id)
                last_heartbeat = time.time()
                comms_lost_shown = False

                while poll_generation[0] == my_gen:
                    time.sleep(3)
                    if poll_generation[0] != my_gen:
                        break

                    # Lost communication detection (fire UI update only once)
                    silence = time.time() - last_heartbeat
                    if silence > LOST_COMMS_TIMEOUT and not comms_lost_shown:
                        log.warning("No heartbeat for %.0fs", silence)
                        GLib.idle_add(set_ui, "Lost communication", False, True, True)
                        comms_lost_shown = True

                    try:
                        messages = api.fasttrack_pending()
                        for m in messages:
                            decoded = decode_target_find_device_update(m, target_id, symmetric_key)
                            if decoded is None:
                                continue
                            mid, resp = decoded
                            # Never log resp directly — it contains GPS coordinates for find-phone.
                            log.info("Response: fn=%s state=%s", resp.get("fn"), resp.get("state"))

                            resp_state = resp.get("state", "")
                            lat = resp.get("lat")
                            lng = resp.get("lng")
                            accuracy = resp.get("accuracy")

                            if resp_state == "ringing":
                                last_heartbeat = time.time()
                                comms_lost_shown = False
                                label = "Search in progress" if is_silent[0] else "Device is ringing!"
                                GLib.idle_add(set_ui, label, False, False, True)
                                if lat is not None:
                                    # Never log raw lat/lng — accuracy only.
                                    log.info("GPS fix received acc=%.1f", accuracy or 0)
                                    GLib.idle_add(update_location, lat, lng, accuracy)
                            elif resp_state == "stopped":
                                log.info("Device confirmed stopped")
                                GLib.idle_add(set_ui, "Alarm stopped", True, True, False)
                                if mid:
                                    api.fasttrack_ack(mid)
                                return  # clean exit
                            if mid:
                                api.fasttrack_ack(mid)
                    except Exception as e:
                        log.error("Poll failed: %s", e)

            threading.Thread(target=do_poll, daemon=True).start()

        def on_stop(btn):
            set_ui("Stopping...", False, False, False)
            poll_generation[0] += 1  # kill poll thread
            def do_stop():
                api, tid, key = shared_api[0], shared_target[0], shared_key[0]
                if api and tid and key:
                    _send_stop(api, tid, key)
                GLib.idle_add(set_ui, "Alarm stopped", True, True, False)
            threading.Thread(target=do_stop, daemon=True).start()

        start_btn.connect("clicked", on_start)
        stop_btn.connect("clicked", on_stop)

        def on_close(w):
            poll_generation[0] += 1  # kill poll thread
            api, tid, key = shared_api[0], shared_target[0], shared_key[0]
            if api and tid and key:
                threading.Thread(target=_send_stop, args=(api, tid, key), daemon=True).start()
            return False

        win.connect("close-request", on_close)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_locate_alert(config_dir: Path, *, sender_name: str):
    """Always-on-top modal shown when this desktop is being located (M.8).

    Spawned as a subprocess by ``GtkSubprocessAlert`` in the parent
    Poller process. The window has one job: display sender info + a
    Stop button. Clicking Stop (or closing the window) exits the
    process; the parent's watcher thread sees the exit and tears the
    rest of the locate session down.
    """
    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Being located",
            default_width=400,
            default_height=220,
        )
        win.set_modal(True)
        try:
            win.set_keep_above(True)
        except Exception:
            pass

        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        title = Gtk.Label(
            label="This device is being located",
            xalign=0,
        )
        title.add_css_class("title-2")
        outer.append(title)

        body = Gtk.Label(
            label=f"Locate request from {sender_name}.\n"
                  "Click Stop to silence this device.",
            xalign=0,
            wrap=True,
        )
        body.add_css_class("body")
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        outer.append(body)

        outer.append(Gtk.Box(vexpand=True))

        button_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        outer.append(button_row)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("pill")
        stop_btn.connect("clicked", lambda _b: win.close())
        button_row.append(stop_btn)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
