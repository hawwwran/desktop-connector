"""Pairing window (`python -m src.windows pairing`)."""

import base64
import json
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GdkPixbuf, GLib, Pango

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .windows_common import _make_app


def show_pairing(config_dir: Path):
    from .config import Config
    from .crypto import KeyManager
    from .connection import ConnectionManager
    from .api_client import ApiClient
    from .devices import (
        ConnectedDeviceRegistry,
        DeviceRegistryError,
        DuplicateDeviceNameError,
    )
    from .file_manager_integration import sync_file_manager_targets
    from .pairing import generate_qr_data, generate_qr_image
    from .pairing_key import (
        AlreadyPairedError,
        JoinRequestError,
        PairingHandshake,
        PairingKeyError,
        PairingKeyParseError,
        PairingKeySchemaError,
        RelayMismatchError,
        SelfPairError,
        begin_join,
        build_local_key,
        complete_join,
        decode as decode_pairing_key,
        default_filename,
        encode as encode_pairing_key,
        validate_for_join,
    )
    # windows.py runs as a GTK4 subprocess — Linux-scoped by construction,
    # so instantiate the Linux backend directly.
    from .backends.linux.dialog_backend import LinuxDialogBackend

    import io
    import logging
    import os as _os

    log = logging.getLogger("desktop-connector.pairing-key")

    config = Config(config_dir)
    # H.7: pass the same store Config picked so the private key
    # lands alongside auth_token + pairing symkeys instead of in a
    # separate PEM file. Insecure-store / no-keyring deployments
    # still get the legacy PEM path as fallback.
    crypto = KeyManager(config_dir, secret_store=config.secret_store)
    conn = ConnectionManager(config.server_url, config.device_id or "", config.auth_token or "")
    api = ApiClient(conn, crypto)
    dialogs = LinuxDialogBackend()

    qr_data = generate_qr_data(config, crypto)
    qr_pil = generate_qr_image(qr_data)
    server_url = json.loads(qr_data)["server"]
    device_id = crypto.get_device_id()

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(application=app, title="Pair with Device",
                                     default_width=460, default_height=640)

        toolbar_view = Adw.ToolbarView()
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(toolbar_view)
        win.set_content(toast_overlay)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(200)
        toolbar_view.set_content(stack)

        # ── Shared state across pages ───────────────────────────
        # role tracks which side of the pair we're on at the
        # naming step. "inviter" means a phone scanned our QR (or a
        # joiner sent us their pairing key); "joiner" means we
        # entered/imported someone else's pairing key.
        role = ["inviter"]
        device_info = [None]      # inviter side: dict from poll_pairing
        derived_key = [None]      # inviter side: bytes
        joiner_handshake: list = [None]  # joiner side: PairingHandshake

        # ── QR + verification page (phone pairing, default) ─────
        qr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=16, margin_bottom=24, margin_start=24, margin_end=24,
                         halign=Gtk.Align.CENTER)
        stack.add_named(qr_box, "qr")

        title = Gtk.Label(label="Scan this QR code with your device")
        title.add_css_class("title-3")
        qr_box.append(title)

        server_label = Gtk.Label(label=server_url)
        server_label.add_css_class("dim-label")
        server_label.add_css_class("caption")
        qr_box.append(server_label)

        id_label = Gtk.Label(label=f"Device ID: {device_id[:16]}...")
        id_label.add_css_class("dim-label")
        id_label.add_css_class("caption")
        qr_box.append(id_label)

        # QR code image
        buf = io.BytesIO()
        qr_pil.save(buf, format="PNG")
        buf.seek(0)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(buf.read())
        loader.close()
        pixbuf = loader.get_pixbuf()
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        qr_image = Gtk.Picture.new_for_paintable(texture)
        qr_image.set_size_request(260, 260)
        qr_image.set_content_fit(Gtk.ContentFit.CONTAIN)
        qr_box.append(qr_image)

        status_label = Gtk.Label(label="Waiting for device to scan...")
        status_label.add_css_class("body")
        qr_box.append(status_label)

        code_label = Gtk.Label(label="")
        code_label.add_css_class("title-1")
        qr_box.append(code_label)

        qr_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                             halign=Gtk.Align.CENTER)
        qr_box.append(qr_btn_box)

        qr_cancel_btn = Gtk.Button(label="Cancel")
        qr_cancel_btn.connect("clicked", lambda b: win.close())
        qr_btn_box.append(qr_cancel_btn)

        confirm_btn = Gtk.Button(label="Confirm Pairing")
        confirm_btn.add_css_class("suggested-action")
        confirm_btn.set_sensitive(False)
        qr_btn_box.append(confirm_btn)

        # Mode-switch link — opens the desktop-mode page.
        pair_desktop_btn = Gtk.Button(label="Pair desktop instead")
        pair_desktop_btn.add_css_class("flat")
        pair_desktop_btn.set_halign(Gtk.Align.CENTER)
        pair_desktop_btn.connect(
            "clicked", lambda _b: stack.set_visible_child_name("desktop"),
        )
        qr_box.append(pair_desktop_btn)

        # ── Desktop-mode hub ───────────────────────────────────
        desktop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                              margin_top=16, margin_bottom=16, margin_start=24, margin_end=24)
        stack.add_named(desktop_box, "desktop")

        desktop_top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=8, halign=Gtk.Align.START)
        desktop_box.append(desktop_top_row)

        pair_phone_btn = Gtk.Button(label="Pair phone instead")
        pair_phone_btn.add_css_class("flat")
        pair_phone_btn.connect(
            "clicked", lambda _b: stack.set_visible_child_name("qr"),
        )
        desktop_top_row.append(pair_phone_btn)

        desktop_title = Gtk.Label(label="Pair with another desktop", xalign=0)
        desktop_title.add_css_class("title-3")
        desktop_box.append(desktop_title)

        desktop_subtitle = Gtk.Label(
            label=(
                "Exchange a pairing key with the other desktop through any "
                "channel you trust — copy/paste through chat, or save to a "
                "file and transfer it. The verification code on both screens "
                "must match before either side confirms."
            ),
            xalign=0, wrap=True,
        )
        desktop_subtitle.add_css_class("dim-label")
        desktop_subtitle.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        desktop_box.append(desktop_subtitle)

        # Group 1 — Share your pairing key (inviter side)
        share_group = Adw.PreferencesGroup(title="Share your pairing key")
        desktop_box.append(share_group)

        show_key_row = Adw.ActionRow(
            title="Show pairing key",
            subtitle="Display the key as text to copy and send",
            activatable=True,
        )
        show_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        share_group.add(show_key_row)

        export_key_row = Adw.ActionRow(
            title="Export pairing key",
            subtitle=f"Save as a {default_filename(build_local_key(config, crypto))[-7:]} file",
            activatable=True,
        )
        export_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        share_group.add(export_key_row)

        # Group 2 — Use someone else's pairing key (joiner side)
        join_group = Adw.PreferencesGroup(
            title="Use someone else's pairing key",
        )
        desktop_box.append(join_group)

        enter_key_row = Adw.ActionRow(
            title="Enter pairing key",
            subtitle="Paste the key text from the other desktop",
            activatable=True,
        )
        enter_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        join_group.add(enter_key_row)

        import_key_row = Adw.ActionRow(
            title="Import pairing key",
            subtitle="Open a .dcpair file from the other desktop",
            activatable=True,
        )
        import_key_row.add_suffix(
            Gtk.Image.new_from_icon_name("go-next-symbolic"),
        )
        join_group.add(import_key_row)

        # Live status row at the bottom of the desktop hub. Updated
        # by the same poll loop that drives the QR page.
        desktop_status = Gtk.Label(
            label="Waiting for an incoming pair request…",
            xalign=0,
        )
        desktop_status.add_css_class("dim-label")
        desktop_box.append(desktop_status)

        # ── Joiner verification page ───────────────────────────
        join_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                           margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
                           halign=Gtk.Align.CENTER)
        stack.add_named(join_box, "join")

        join_title = Gtk.Label(label="Verify pairing")
        join_title.add_css_class("title-3")
        join_box.append(join_title)

        join_subtitle = Gtk.Label(
            label=(
                "Confirm this code matches the verification code shown on "
                "the other desktop's pairing window before clicking Confirm."
            ),
            xalign=0, wrap=True,
        )
        join_subtitle.add_css_class("dim-label")
        join_subtitle.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        join_box.append(join_subtitle)

        join_status = Gtk.Label(label="", xalign=0)
        join_status.add_css_class("body")
        join_box.append(join_status)

        join_code = Gtk.Label(label="")
        join_code.add_css_class("title-1")
        join_box.append(join_code)

        join_help = Gtk.Label(
            label=(
                "If the other desktop never confirms, the pair won't take "
                "effect — try opening its pairing window and trying again."
            ),
            xalign=0, wrap=True,
        )
        join_help.add_css_class("dim-label")
        join_help.add_css_class("caption")
        join_help.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        join_box.append(join_help)

        join_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                               halign=Gtk.Align.CENTER, margin_top=8)
        join_box.append(join_btn_box)

        join_cancel_btn = Gtk.Button(label="Cancel")
        join_cancel_btn.connect(
            "clicked",
            lambda _b: stack.set_visible_child_name("desktop"),
        )
        join_btn_box.append(join_cancel_btn)

        join_confirm_btn = Gtk.Button(label="Confirm Pairing")
        join_confirm_btn.add_css_class("suggested-action")
        join_btn_box.append(join_confirm_btn)

        # ── Naming page (shared) ────────────────────────────────
        naming_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        stack.add_named(naming_box, "naming")

        naming_title = Gtk.Label(label="Name this device")
        naming_title.add_css_class("title-3")
        naming_title.set_xalign(0)
        naming_box.append(naming_title)

        naming_subtitle = Gtk.Label(
            label="Choose how this connected device appears in lists, history, and file-manager send targets."
        )
        naming_subtitle.add_css_class("dim-label")
        naming_subtitle.add_css_class("body")
        naming_subtitle.set_wrap(True)
        naming_subtitle.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        naming_subtitle.set_xalign(0)
        naming_box.append(naming_subtitle)

        name_group = Adw.PreferencesGroup()
        naming_box.append(name_group)

        name_row = Adw.EntryRow(title="Name")
        name_group.add(name_row)

        naming_error = Gtk.Label(label="")
        naming_error.add_css_class("error")
        naming_error.set_wrap(True)
        naming_error.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        naming_error.set_xalign(0)
        naming_error.set_visible(False)
        naming_box.append(naming_error)

        naming_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                                 halign=Gtk.Align.END, margin_top=8)
        naming_box.append(naming_btn_box)

        naming_cancel_btn = Gtk.Button(label="Cancel")
        naming_cancel_btn.connect("clicked", lambda b: win.close())
        naming_btn_box.append(naming_cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        naming_btn_box.append(save_btn)

        stack.set_visible_child_name("qr")

        # ── Inviter side: Confirm Pairing on the QR page ───────
        def on_confirm_inviter(_b):
            if not device_info[0]:
                return
            role[0] = "inviter"
            registry = ConnectedDeviceRegistry(config)
            name_row.set_text(registry.next_default_name())
            naming_error.set_visible(False)
            stack.set_visible_child_name("naming")
            name_row.grab_focus()

        confirm_btn.connect("clicked", on_confirm_inviter)

        # ── Joiner side: Confirm Pairing on the join page ──────
        def on_confirm_joiner(_b):
            if not joiner_handshake[0]:
                return
            role[0] = "joiner"
            registry = ConnectedDeviceRegistry(config)
            name_row.set_text(
                joiner_handshake[0].key.name
                or registry.next_default_name(),
            )
            naming_error.set_visible(False)
            stack.set_visible_child_name("naming")
            name_row.grab_focus()

        join_confirm_btn.connect("clicked", on_confirm_joiner)

        # ── Naming page save handler (branches on role) ─────────
        def show_naming_error(message: str):
            naming_error.set_label(message)
            naming_error.set_visible(True)

        def on_save(_b):
            registry = ConnectedDeviceRegistry(config)
            try:
                normalized = registry.validate_unique_name(name_row.get_text())
            except DuplicateDeviceNameError:
                show_naming_error("This name is already used by another device.")
                return
            except DeviceRegistryError:
                show_naming_error("Name cannot be empty.")
                return

            if role[0] == "joiner":
                handshake = joiner_handshake[0]
                if handshake is None:
                    show_naming_error("Pairing handshake is not ready.")
                    return
                try:
                    complete_join(
                        handshake,
                        config=config,
                        name=normalized,
                        on_synced=lambda: sync_file_manager_targets(config),
                    )
                except Exception as exc:
                    log.exception("pairing.key.complete_join_failed")
                    show_naming_error(f"Could not save pairing: {exc}")
                    return
            else:
                info = device_info[0]
                if info is None:
                    show_naming_error("Pairing request is no longer valid.")
                    return
                sym_key = derived_key[0]
                if sym_key is None:
                    sym_key = crypto.derive_shared_key(info["phone_pubkey"])
                config.add_paired_device(
                    device_id=info["phone_id"],
                    pubkey=info["phone_pubkey"],
                    symmetric_key_b64=base64.b64encode(sym_key).decode(),
                    name=normalized,
                )
                api.confirm_pairing(info["phone_id"])
                try:
                    registry.mark_active(info["phone_id"], reason="paired")
                except DeviceRegistryError:
                    pass
                try:
                    sync_file_manager_targets(config)
                except Exception:
                    pass

            naming_title.set_label("Paired!")
            save_btn.set_sensitive(False)
            naming_cancel_btn.set_sensitive(False)
            GLib.timeout_add(800, win.close)

        save_btn.connect("clicked", on_save)

        # ── Inviter poll loop (also serves desktop hub) ─────────
        def poll_pairing():
            if not win.is_visible():
                return False
            requests_list = api.poll_pairing()
            paired_ids = set(config.paired_devices.keys()) if requests_list else set()
            for req in requests_list:
                if req["phone_id"] in paired_ids:
                    log.info(
                        "pairing.request.ignored_already_paired peer=%s",
                        req["phone_id"][:12],
                    )
                    continue
                device_info[0] = req
                sym_key = crypto.derive_shared_key(req["phone_pubkey"])
                derived_key[0] = sym_key
                code = KeyManager.get_verification_code(sym_key)
                msg = f"Device connected: {req['phone_id'][:12]}...  Verify code:"
                status_label.set_text(msg)
                code_label.set_text(code)
                confirm_btn.set_sensitive(True)
                desktop_status.set_text(
                    "Incoming pair request — review the verification code on the QR tab."
                )
                # Auto-switch to QR page so the user sees the verification.
                if stack.get_visible_child_name() == "desktop":
                    stack.set_visible_child_name("qr")
                return False
            return True

        GLib.timeout_add(2000, poll_pairing)

        # ── Show pairing key dialog (string surface) ───────────
        def on_show_pairing_key(_row):
            key = build_local_key(config, crypto)
            text = encode_pairing_key(key)
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="Your pairing key",
                body=(
                    "Send this key to the other desktop through a channel "
                    "you trust. Anyone with the key can request to pair "
                    "with this desktop while this window is open. The "
                    "verification code on both screens must match before "
                    "either side confirms."
                ),
            )
            extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            text_view = Gtk.TextView()
            text_view.set_editable(False)
            text_view.set_monospace(True)
            text_view.set_wrap_mode(Gtk.WrapMode.CHAR)
            text_view.get_buffer().set_text(text)
            scroller = Gtk.ScrolledWindow()
            scroller.set_min_content_height(120)
            scroller.set_hexpand(True)
            scroller.set_child(text_view)
            extra.append(scroller)
            dialog.set_extra_child(extra)
            dialog.add_response("close", "Done")
            dialog.add_response("copy", "Copy")
            dialog.set_response_appearance(
                "copy", Adw.ResponseAppearance.SUGGESTED,
            )
            dialog.set_default_response("copy")

            def on_response(dlg, response):
                if response == "copy":
                    clipboard = win.get_clipboard()
                    clipboard.set(text)
                    toast_overlay.add_toast(
                        Adw.Toast(title="Pairing key copied", timeout=2),
                    )
                dlg.close()

            dialog.connect("response", on_response)
            log.info("pairing.key.shown")
            dialog.present()

        show_key_row.connect("activated", on_show_pairing_key)

        # ── Export pairing key dialog (file surface) ───────────
        def on_export_pairing_key(_row):
            key = build_local_key(config, crypto)
            text = encode_pairing_key(key)
            chosen = dialogs.save_file(
                "Export pairing key",
                default_filename=default_filename(key),
                file_types=(("Pairing key", "*.dcpair"),),
            )
            if chosen is None:
                return
            try:
                # Write atomically with restrictive perms — same bucket
                # as identity material.
                tmp = chosen.with_suffix(chosen.suffix + ".tmp")
                tmp.write_text(text)
                try:
                    _os.chmod(tmp, 0o600)
                except OSError:
                    pass
                tmp.replace(chosen)
            except OSError as exc:
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=f"Could not write file: {exc}", timeout=4,
                    ),
                )
                log.warning(
                    "pairing.key.export_failed err=%s",
                    type(exc).__name__,
                )
                return
            log.info("pairing.key.exported path=%s", chosen)
            toast_overlay.add_toast(
                Adw.Toast(
                    title=f"Pairing key exported to {chosen.name}", timeout=3,
                ),
            )

        export_key_row.connect("activated", on_export_pairing_key)

        # ── Joiner: present the verification page after parse+validate ──
        def begin_joiner_session(text: str, *, surface: str) -> None:
            try:
                key = decode_pairing_key(text)
            except (PairingKeyParseError, PairingKeySchemaError) as exc:
                log.warning(
                    "pairing.key.import_parse_failed surface=%s err=%s",
                    surface, type(exc).__name__,
                )
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=f"Pairing key is malformed: {exc}", timeout=4,
                    ),
                )
                return
            try:
                validate_for_join(key, config=config, crypto=crypto)
            except SelfPairError:
                log.warning("pairing.key.import_self_pair_refused")
                toast_overlay.add_toast(
                    Adw.Toast(
                        title="This pairing key is from this same desktop.",
                        timeout=4,
                    ),
                )
                return
            except RelayMismatchError as exc:
                # Hostnames only — full URL may carry tokens we don't
                # want in logs.
                from urllib.parse import urlsplit
                local_host = urlsplit(exc.local).netloc
                remote_host = urlsplit(exc.remote).netloc
                log.warning(
                    "pairing.key.import_relay_mismatched local=%s remote=%s",
                    local_host, remote_host,
                )
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=(
                            f"Different relay servers ({remote_host}). Both "
                            f"desktops must be configured for the same relay."
                        ),
                        timeout=6,
                    ),
                )
                return
            except AlreadyPairedError as exc:
                log.warning(
                    "pairing.key.import_already_paired_refused peer=%s",
                    exc.device_id[:12],
                )
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=(
                            f"Already paired with \"{exc.name}\". Unpair "
                            f"first if you want to re-pair."
                        ),
                        timeout=6,
                    ),
                )
                return
            except PairingKeyError as exc:
                toast_overlay.add_toast(
                    Adw.Toast(title=str(exc), timeout=4),
                )
                return

            try:
                handshake = begin_join(
                    key, crypto=crypto,
                    send_pairing_request=api.send_pairing_request,
                )
            except JoinRequestError as exc:
                log.warning(
                    "pairing.key.import_request_failed peer=%s",
                    key.device_id[:12],
                )
                toast_overlay.add_toast(
                    Adw.Toast(title=str(exc), timeout=6),
                )
                return

            joiner_handshake[0] = handshake
            log.info(
                "pairing.request.sent_as_joiner target=%s",
                key.device_id[:12],
            )
            join_status.set_text(
                f"Pairing with {handshake.key.name} ({handshake.key.device_id[:12]}…)",
            )
            join_code.set_text(handshake.verification_code)
            stack.set_visible_child_name("join")

        # ── Enter pairing key dialog (string surface) ──────────
        def on_enter_pairing_key(_row):
            dialog = Adw.MessageDialog(
                transient_for=win,
                heading="Enter pairing key",
                body=(
                    "Paste the pairing key text the other desktop shared. "
                    "It typically starts with `dc-pair:`."
                ),
            )
            extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            text_view = Gtk.TextView()
            text_view.set_monospace(True)
            text_view.set_wrap_mode(Gtk.WrapMode.CHAR)
            text_view.set_accepts_tab(False)
            scroller = Gtk.ScrolledWindow()
            scroller.set_min_content_height(120)
            scroller.set_hexpand(True)
            scroller.set_child(text_view)
            extra.append(scroller)
            dialog.set_extra_child(extra)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("continue", "Continue")
            dialog.set_response_appearance(
                "continue", Adw.ResponseAppearance.SUGGESTED,
            )
            dialog.set_default_response("continue")

            def on_response(dlg, response):
                if response == "continue":
                    buf_obj = text_view.get_buffer()
                    start = buf_obj.get_start_iter()
                    end = buf_obj.get_end_iter()
                    text = buf_obj.get_text(start, end, False)
                    dlg.close()
                    begin_joiner_session(text, surface="text")
                else:
                    dlg.close()

            dialog.connect("response", on_response)
            dialog.present()
            text_view.grab_focus()

        enter_key_row.connect("activated", on_enter_pairing_key)

        # ── Import pairing key dialog (file surface) ───────────
        def on_import_pairing_key(_row):
            paths = dialogs.pick_files("Import pairing key")
            if not paths:
                return
            path = paths[0]
            try:
                text = path.read_text()
            except OSError as exc:
                toast_overlay.add_toast(
                    Adw.Toast(
                        title=f"Could not read file: {exc}", timeout=4,
                    ),
                )
                return
            begin_joiner_session(text, surface="file")

        import_key_row.connect("activated", on_import_pairing_key)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
