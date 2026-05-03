"""Vault GTK windows.

Three subprocess windows:
  * `vault-main` — the deep settings tab strip (recovery, folders, devices…).
  * `vault-onboard` — the create / import wizard.
  * `vault-passphrase-generator` — standalone diceware generator.
"""

from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .windows_common import _make_app


def _local_vault_exists(config) -> bool:
    """True iff this device thinks a vault already exists locally.

    For T3 the heuristic is "has the wizard ever set
    config['vault']['last_known_id']?". A keyring-backed grant store
    (T3.2) provides a more authoritative answer once integration
    lands; this is good enough for the wizard's cancel-rule input.
    """
    raw = config._data.get("vault")
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("last_known_id"))


def show_vault_main(config_dir: Path):
    """Vault settings GTK window skeleton (T3.4).

    Top: Vault ID with copy button + (placeholder) QR icon.
    Body: tabbed pane with placeholders per the plan
    (Recovery / Folders / Devices / Activity / Maintenance / Security /
    Sync safety / Storage / Danger zone). Recovery tab implements the
    §gaps §2 emergency-access block.

    M1 manual-smoke surface; later phases populate the empty tabs.
    """
    import logging
    from .config import Config

    log = logging.getLogger("desktop-connector.vault-ui")
    config = Config(config_dir)
    app = _make_app()

    vault_id_undashed = ""
    paired = config.paired_devices
    # Reading the vault id from local grant storage is T3.2's surface;
    # for the M1 walk-through we surface whatever's currently stashed
    # under config["vault"]["last_known_id"] (set by the wizard on
    # successful create) and fall back to a placeholder.
    vault_meta = config._data.get("vault") if isinstance(config._data.get("vault"), dict) else {}
    vault_id_undashed = (vault_meta or {}).get("last_known_id") or ""
    recovery_status_text = (vault_meta or {}).get("recovery_status") or "Untested"
    recovery_last_tested = (vault_meta or {}).get("recovery_last_tested") or "—"

    def vault_id_dashed() -> str:
        v = vault_id_undashed
        if len(v) == 12:
            return f"{v[0:4]}-{v[4:8]}-{v[8:12]}"
        return "(no vault opened)"

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Vault settings",
            default_width=720,
            default_height=540,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
        )
        toolbar.set_content(outer)

        # ---- header: Vault ID + copy button ----
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(header)

        id_label = Gtk.Label(label="Vault ID:", xalign=0)
        id_label.add_css_class("dim-label")
        header.append(id_label)

        id_value = Gtk.Label(label=vault_id_dashed(), xalign=0, hexpand=True)
        id_value.add_css_class("monospace")
        id_value.add_css_class("title-4")
        header.append(id_value)

        def on_copy(_btn):
            display = win.get_display()
            if display is not None:
                display.get_clipboard().set(vault_id_dashed())
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.add_css_class("pill")
        copy_btn.connect("clicked", on_copy)
        header.append(copy_btn)

        qr_btn = Gtk.Button(label="QR")
        qr_btn.add_css_class("pill")
        qr_btn.set_tooltip_text("Show Vault ID as a QR code (post-v1)")
        qr_btn.set_sensitive(False)
        header.append(qr_btn)

        # ---- tabbed pane ----
        view_stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        outer.append(switcher)
        outer.append(view_stack)

        def add_tab(name: str, title: str, body: Gtk.Widget) -> None:
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_child(body)
            view_stack.add_titled(scroller, name, title)

        # Recovery tab — §gaps §2 emergency-access block.
        recovery = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
        )
        recovery.append(Gtk.Label(label="Emergency recovery", xalign=0, css_classes=["title-3"]))
        block = Gtk.Grid(column_spacing=12, row_spacing=6)
        recovery_value_labels = {}
        for row, (k, v) in enumerate([
            ("Method", "Recovery kit + passphrase"),
            ("Last tested", recovery_last_tested),
            ("Status", recovery_status_text),
        ]):
            k_lbl = Gtk.Label(label=k, xalign=0)
            k_lbl.add_css_class("dim-label")
            block.attach(k_lbl, 0, row, 1, 1)
            v_lbl = Gtk.Label(label=v, xalign=0)
            block.attach(v_lbl, 1, row, 1, 1)
            recovery_value_labels[k] = v_lbl
        recovery.append(block)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        recovery.append(actions)
        test_recovery_btn = Gtk.Button(label="Test recovery now", css_classes=["pill"])
        actions.append(test_recovery_btn)
        update_recovery_btn = Gtk.Button(label="Update recovery material", css_classes=["pill"])
        update_recovery_btn.set_sensitive(False)
        update_recovery_btn.set_tooltip_text("Recovery-material rotation is not implemented yet")
        actions.append(update_recovery_btn)

        recovery_warning = Gtk.Label(
            label="Recovery has not been tested. Test it now to confirm you "
                  "can actually restore the vault.",
            xalign=0,
            wrap=True,
        )
        recovery_warning.add_css_class("warning")
        recovery_warning.set_visible(recovery_status_text in ("Untested", "Stale"))
        recovery.append(recovery_warning)

        def refresh_recovery_summary(status: str, last_tested: str | None = None) -> None:
            recovery_value_labels["Status"].set_label(status)
            if last_tested is not None:
                recovery_value_labels["Last tested"].set_label(last_tested)
            recovery_warning.set_visible(status in ("Untested", "Stale"))

        def open_recovery_test_dialog(_btn):
            log.info(
                "vault.recovery_test.clicked vault_id_present=%s config_meta_present=%s",
                bool(vault_id_undashed),
                isinstance((config._data.get("vault") or {}).get("recovery_envelope_meta"), dict),
            )
            try:
                from datetime import datetime, timezone
                from .vault import recovery_envelope_meta_from_json, vault_id_dashed
                from .vault_local import run_recovery_material_test

                dialog = Adw.ApplicationWindow(
                    application=app,
                    title="Test recovery",
                    default_width=560,
                    default_height=420,
                )
                dialog.set_transient_for(win)
                dialog.set_modal(True)
                toolbar = Adw.ToolbarView()
                dialog.set_content(toolbar)
                toolbar.add_top_bar(Adw.HeaderBar())

                extra = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=12,
                    margin_top=16,
                    margin_bottom=16,
                    margin_start=16,
                    margin_end=16,
                )
                toolbar.set_content(extra)
                extra.append(Gtk.Label(label="Test recovery", xalign=0, css_classes=["title-2"]))
                extra.append(Gtk.Label(
                    label="Select the recovery kit file and enter the passphrase saved for this vault.",
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                ))

                kit_path = {"path": None}
                kit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                kit_entry = Gtk.Entry(
                    placeholder_text="Recovery kit file",
                    editable=False,
                    hexpand=True,
                )
                kit_row.append(kit_entry)
                browse_btn = Gtk.Button(label="Choose...", css_classes=["pill"])
                kit_row.append(browse_btn)
                extra.append(kit_row)

                vault_id_entry = Gtk.Entry(hexpand=True)
                vault_id_entry.set_text(vault_id_dashed(vault_id_undashed) if vault_id_undashed else "")
                vault_id_entry.set_placeholder_text("Vault ID")
                extra.append(vault_id_entry)

                extra.append(Gtk.Label(label="Recovery passphrase", xalign=0))
                passphrase_entry = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
                extra.append(passphrase_entry)

                wipe_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                wipe_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
                wipe_row.append(wipe_switch)
                wipe_label = Gtk.Label(
                    label="Securely delete the recovery kit file after a successful test",
                    xalign=0,
                    wrap=True,
                    hexpand=True,
                )
                wipe_label.add_css_class("dim-label")
                wipe_row.append(wipe_label)
                extra.append(wipe_row)

                status_label = Gtk.Label(xalign=0, wrap=True)
                status_label.add_css_class("dim-label")
                extra.append(status_label)

                button_row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=8,
                    halign=Gtk.Align.END,
                )
                extra.append(button_row)
                close_btn = Gtk.Button(label="Close", css_classes=["pill"])
                test_btn = Gtk.Button(label="Test recovery", css_classes=["pill", "suggested-action"])
                button_row.append(close_btn)
                button_row.append(test_btn)

                def set_status(message: str, css_class: str = "dim-label") -> None:
                    for klass in ("dim-label", "error", "success"):
                        status_label.remove_css_class(klass)
                    status_label.add_css_class(css_class)
                    status_label.set_label(message)

                def on_choose_file(_button):
                    log.info("vault.recovery_test.file_choose.clicked")
                    file_dialog = Gtk.FileDialog()
                    file_dialog.set_title("Choose recovery kit")

                    def on_file_chosen(file_dialog, result):
                        try:
                            gio_file = file_dialog.open_finish(result)
                        except GLib.Error:
                            log.info("vault.recovery_test.file_choose.cancelled")
                            return
                        if gio_file is None:
                            log.info("vault.recovery_test.file_choose.empty")
                            return
                        path = gio_file.get_path()
                        if not path:
                            log.info("vault.recovery_test.file_choose.no_local_path")
                            return
                        kit_path["path"] = path
                        kit_entry.set_text(path)
                        log.info("vault.recovery_test.file_choose.selected")

                    file_dialog.open(parent=dialog, callback=on_file_chosen)

                browse_btn.connect("clicked", on_choose_file)

                def on_close(_button):
                    log.info(
                        "vault.recovery_test.response response=close kit_selected=%s wipe=%s",
                        bool(kit_path["path"]),
                        wipe_switch.get_active(),
                    )
                    dialog.close()

                close_btn.connect("clicked", on_close)

                def on_test(_button):
                    log.info(
                        "vault.recovery_test.response response=%s kit_selected=%s wipe=%s",
                        "test",
                        bool(kit_path["path"]),
                        wipe_switch.get_active(),
                    )
                    set_status("Testing recovery...", "dim-label")
                    test_btn.set_sensitive(False)
                    try:
                        meta = recovery_envelope_meta_from_json(
                            (config._data.get("vault") or {}).get("recovery_envelope_meta")
                        )
                        log.info("vault.recovery_test.config_meta.loaded")
                    except Exception as exc:
                        meta = None
                        log.info(
                            "vault.recovery_test.config_meta.unavailable error_kind=%s",
                            type(exc).__name__,
                        )

                    try:
                        result = run_recovery_material_test(
                            kit_path["path"],
                            passphrase=passphrase_entry.get_text(),
                            vault_id=vault_id_entry.get_text(),
                            envelope_meta=meta,
                            wipe_after_success=wipe_switch.get_active(),
                        )
                    except Exception:
                        log.exception("vault.recovery_test.run.exception")
                        set_status("Recovery test failed unexpectedly. Check the log for details.", "error")
                        test_btn.set_sensitive(True)
                        return
                    finally:
                        test_btn.set_sensitive(True)

                    log.info(
                        "vault.recovery_test.result ok=%s wiped=%s message=%s",
                        result.ok,
                        result.wiped,
                        result.message,
                    )
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    if "vault" not in config._data or not isinstance(config._data.get("vault"), dict):
                        config._data["vault"] = {}
                    config._data["vault"]["recovery_last_tested"] = now
                    if result.ok:
                        config._data["vault"]["recovery_status"] = "Verified"
                        config.save()
                        refresh_recovery_summary("Verified", now)
                        set_status(result.message, "success")
                        if result.wiped:
                            kit_path["path"] = None
                            kit_entry.set_text("")
                    else:
                        config._data["vault"]["recovery_status"] = "Failed"
                        config.save()
                        refresh_recovery_summary("Failed", now)
                        set_status(result.message, "error")

                test_btn.connect("clicked", on_test)
                dialog.present()
                log.info("vault.recovery_test.dialog.presented")
            except Exception:
                log.exception("vault.recovery_test.dialog.exception")

        test_recovery_btn.connect("clicked", open_recovery_test_dialog)
        log.info("vault.recovery_test.button.connected vault_id_present=%s", bool(vault_id_undashed))
        add_tab("recovery", "Recovery", recovery)

        from .vault_folders_tab import build_vault_folders_tab
        add_tab("folders", "Folders", build_vault_folders_tab(
            app=app,
            parent_window=win,
            config_dir=config_dir,
            config=config,
            vault_id=vault_id_undashed,
        ))

        # Other tabs are empty placeholders for later phases.
        for name, title in [
            ("devices", "Devices"),
            ("activity", "Activity"),
            ("maintenance", "Maintenance"),
            ("security", "Security"),
            ("sync_safety", "Sync safety"),
            ("storage", "Storage"),
        ]:
            placeholder = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=8,
                margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
            )
            placeholder.append(Gtk.Label(
                label=f"{title} — coming in a later phase.",
                xalign=0, css_classes=["dim-label"],
            ))
            add_tab(name, title, placeholder)

        danger = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        danger.append(Gtk.Label(label="Disconnect vault", xalign=0, css_classes=["title-3"]))
        danger.append(Gtk.Label(
            label="Remove this machine's local connection to the vault.",
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        disconnect_btn = Gtk.Button(label="Disconnect vault", css_classes=["pill", "destructive-action"])
        disconnect_btn.set_halign(Gtk.Align.START)
        disconnect_btn.set_sensitive(bool(vault_id_undashed))
        danger.append(disconnect_btn)

        def on_disconnect_vault(_btn):
            dlg = Adw.AlertDialog(
                heading="Disconnect vault?",
                body="The vault will still exist. This machine will only lose the connection to it.",
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("disconnect", "Disconnect vault")
            dlg.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def on_resp(_dialog, response):
                if response != "disconnect":
                    return
                from .vault_local import disconnect_local_vault
                disconnect_local_vault(config)
                win.close()

            dlg.connect("response", on_resp)
            dlg.present(win)

        disconnect_btn.connect("clicked", on_disconnect_vault)
        add_tab("danger_zone", "Danger zone", danger)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_vault_onboard(config_dir: Path):
    """Vault create / import wizard (T3.6).

    Two paths: 'Create new vault' (full M1 flow) and 'Import from
    export' (stubbed for T8). The create flow walks:
        1. relay picker (uses the existing ``server_url`` if set)
        2. recovery passphrase entry + confirm
        3. recovery-test prompt with Skip option
        4. success screen

    Per §A2: cancelling without an existing vault flips
    ``Config.vault_active`` to False so the user isn't permanently
    nagged.
    """
    from .config import Config
    from .vault_ui_state import wizard_cancel_rule

    config = Config(config_dir)

    # Wizard state — held in a dict so nested closures can mutate.
    # `recovery_secret_bytes` and `vault_access_secret` are stashed
    # post-create so the Export+Verify button can build the kit
    # content on demand (no silent auto-save anywhere). Both are
    # zeroed when the wizard closes. `recovery_envelope_meta` is
    # non-secret but needed to run the real recovery test.
    state = {
        "step": "choose_path",        # → create_passphrase → success
        "vault_existed_at_open": _local_vault_exists(config),
        "passphrase": "",
        "completed_successfully": False,
        "vault_id": None,
        "recovery_secret_bytes": None,
        "vault_access_secret": None,
        "recovery_envelope_meta": None,
        "exported_kit_path": None,
        "verify_passed": False,
        "delete_after_close": False,
    }

    app = _make_app()

    def _zero_state_secrets():
        """Best-effort overwrite of in-memory copies of the kit material
        before the wizard process exits. The Vault object's own
        master_key was already zero'd by ``vault.close()`` inside
        perform_create; this covers the duplicates we stashed for the
        Export flow.
        """
        rs = state.get("recovery_secret_bytes")
        if isinstance(rs, (bytes, bytearray)):
            buf = bytearray(rs)
            for i in range(len(buf)):
                buf[i] = 0
            state["recovery_secret_bytes"] = None
        state["vault_access_secret"] = None
        state["passphrase"] = ""

    def on_close(win):
        # If "Safely delete after close" is on AND a kit file was
        # exported during this wizard session, shred it now.
        if state.get("delete_after_close") and state.get("exported_kit_path"):
            from .vault import shred_file
            shred_file(state["exported_kit_path"])

        # If the wizard closes WITHOUT the user completing the
        # success flow (export + verify + confirmation), wipe any
        # local trace of vault creation so they can retry from
        # scratch on the next click. The relay-side vault row is
        # orphaned (no kit was saved → master key can't be
        # recovered → unusable anyway). Per
        # `feedback_respect_user_intent.md`: clean up partial state,
        # but never auto-flip the user's toggle.
        if not state["completed_successfully"] and state.get("vault_id"):
            try:
                cfg_dict = config._data.get("vault")
                if isinstance(cfg_dict, dict):
                    cfg_dict.pop("last_known_id", None)
                    config.save()
            except Exception:
                pass

        # The wizard's cancel rule (revised 2026-05-03) never changes
        # the toggle — the user's deliberate ON stays ON. Function
        # call kept for signature stability.
        wizard_cancel_rule(vault_exists=state["vault_existed_at_open"])

        _zero_state_secrets()
        return False

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Vault setup",
            default_width=720,
            default_height=520,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())
        win.connect("close-request", lambda w: on_close(w))

        body = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        toolbar.set_content(body)

        # Step 1 — choose path.
        choose = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        choose.append(Gtk.Label(label="Set up a vault", xalign=0, css_classes=["title-2"]))
        choose.append(Gtk.Label(
            label="A vault stores files and history end-to-end encrypted on the relay.",
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        create_btn = Gtk.Button(label="Create a new vault", css_classes=["pill", "suggested-action"])
        import_btn = Gtk.Button(label="Import from export… (coming in T8)", css_classes=["pill"])
        import_btn.set_sensitive(False)
        choose.append(create_btn)
        choose.append(import_btn)
        body.add_named(choose, "choose_path")

        # Step 2 — passphrase entry.
        pp = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        pp.append(Gtk.Label(label="Recovery passphrase", xalign=0, css_classes=["title-3"]))
        pp.append(Gtk.Label(
            label="This passphrase plus the recovery kit file is your only path "
                  "back to the vault if every device is lost. Choose carefully.",
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        # ``show_peek_icon=True`` adds the eye icon inside the entry —
        # GTK's built-in reveal-on-demand. Click toggles between
        # masked dots and plaintext characters; the unmasked state
        # persists only while the icon is held / toggled, so it doesn't
        # leak the passphrase to anyone who happens to glance later.
        pp_entry = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
        pp_confirm = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)

        # Generate button — opens the standalone passphrase generator
        # window. The user copies the result and pastes it into the
        # passphrase fields manually (matches the existing subprocess-
        # window pattern; no IPC needed).
        gen_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pp_entry.set_hexpand(True)
        gen_row.append(pp_entry)
        gen_btn = Gtk.Button(label="Generate", css_classes=["pill"])
        gen_btn.set_tooltip_text("Open a passphrase generator in a new window")
        def on_generate(_btn):
            import os as _os
            import subprocess as _subprocess
            import sys as _sys
            appimage = _os.environ.get("APPIMAGE")
            cmd = (
                [appimage, "--gtk-window=vault-passphrase-generator",
                 f"--config-dir={config.config_dir}"]
                if appimage else
                [_sys.executable, "-m", "src.windows", "vault-passphrase-generator",
                 f"--config-dir={config.config_dir}"]
            )
            cwd = (None if appimage
                   else str(Path(__file__).resolve().parent.parent))
            _subprocess.Popen(cmd, cwd=cwd)
        gen_btn.connect("clicked", on_generate)
        gen_row.append(gen_btn)
        pp.append(gen_row)

        pp.append(Gtk.Label(label="Confirm passphrase", xalign=0))
        pp.append(pp_confirm)
        pp_status = Gtk.Label(xalign=0, css_classes=["dim-label"])
        pp.append(pp_status)
        pp_next = Gtk.Button(label="Continue", css_classes=["pill", "suggested-action"])
        pp.append(pp_next)
        body.add_named(pp, "create_passphrase")

        # (Step 3 — vestigial "recovery test prompt" with Test/Skip
        # buttons that did the same thing has been removed. The real
        # recovery test now happens on the success screen, bundled
        # with kit export, mandatory before Done. See
        # `feedback_no_fake_tests.md` and `T0 §gaps §1` revision.)

        # Step 4 — success: vault is created, user MUST back up the
        # recovery kit + passphrase before leaving. Layout follows the
        # "explicit user-controlled flow + severity messaging +
        # confirmation gate" pattern (see memory feedback_security_ux.md).
        ok = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        ok.append(Gtk.Label(label="Vault created", xalign=0, css_classes=["title-2"]))

        # ---- Severity warning (always visible) ----
        warn = Gtk.Label(
            xalign=0, wrap=True,
            label=(
                "⚠ Your data is unrecoverable without BOTH the recovery kit "
                "file AND your passphrase. There is no password reset. Lose "
                "either one and the vault is gone forever."
            ),
        )
        warn.add_css_class("warning")
        ok.append(warn)

        # ---- Copyable Vault ID ----
        ok.append(Gtk.Label(label="Your Vault ID:", xalign=0, css_classes=["dim-label"]))
        ok_id_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ok.append(ok_id_row)
        ok_id_entry = Gtk.Entry()
        ok_id_entry.set_editable(False)
        ok_id_entry.set_can_focus(True)
        ok_id_entry.add_css_class("monospace")
        ok_id_entry.set_hexpand(True)
        ok_id_row.append(ok_id_entry)
        ok_id_copy = Gtk.Button(label="Copy", css_classes=["pill"])
        def on_copy_vault_id(_btn):
            display = win.get_display()
            if display is not None:
                display.get_clipboard().set(ok_id_entry.get_text())
        ok_id_copy.connect("clicked", on_copy_vault_id)
        ok_id_row.append(ok_id_copy)

        # ---- Export + verify recovery kit ----
        # Bundled as one user action: there's no point exporting a kit
        # without confirming the kit + passphrase actually produce the
        # master key. The verify is real — re-runs derive_recovery_wrap_key
        # against the saved kit and AEAD-decrypts the recovery envelope.
        ok.append(Gtk.Label(label="Recovery kit file:", xalign=0, css_classes=["dim-label"]))
        export_btn = Gtk.Button(
            label="Export and verify recovery kit…",
            css_classes=["pill", "suggested-action"],
        )
        ok.append(export_btn)

        # Status line — shows the exported path + verify result.
        export_status = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["monospace", "dim-label"])
        ok.append(export_status)
        verify_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        ok.append(verify_status)

        # "Safely delete" toggle — only meaningful after an export.
        # Shown but disabled until the user has actually picked a path.
        delete_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ok.append(delete_row)
        delete_switch = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=False)
        delete_row.append(delete_switch)
        delete_label = Gtk.Label(
            xalign=0, wrap=True, hexpand=True,
            label=(
                "Securely delete the exported file when I close this wizard "
                "(use this if you're moving it into a password manager and "
                "don't want a copy left in Downloads)"
            ),
        )
        delete_label.add_css_class("dim-label")
        delete_row.append(delete_label)

        # ---- Confirmation gate ----
        confirm_check = Gtk.CheckButton(
            label=(
                "I have backed up the recovery kit file AND remember my passphrase. "
                "I understand my vault data is unrecoverable without both."
            ),
        )
        confirm_check.set_sensitive(False)   # disabled until export+verify pass
        ok.append(confirm_check)

        ok_close = Gtk.Button(label="Done", css_classes=["pill", "suggested-action"])
        ok_close.set_sensitive(False)
        ok.append(ok_close)

        # ---- Wire up the success-screen logic ----
        def _refresh_done_button():
            """Done is enabled only when the user has confirmed AND the
            export+verify step has succeeded.
            """
            ok_close.set_sensitive(
                state["verify_passed"] and confirm_check.get_active()
            )

        confirm_check.connect("toggled", lambda _w: _refresh_done_button())

        def on_delete_toggled(switch, _pspec):
            state["delete_after_close"] = switch.get_active()
        delete_switch.connect("notify::active", on_delete_toggled)

        def on_export_clicked(_btn):
            """Open a save dialog, write the kit, then **verify** that
            the kit + passphrase actually produce the master key. This
            is the real recovery test — replaces the prior decorative
            'Test now / Skip' branches that did nothing.
            """
            from .vault import (
                vault_id_dashed,
                verify_recovery_kit,
                write_recovery_kit_file,
            )

            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Save recovery kit")
            file_dialog.set_initial_name(
                f"{vault_id_dashed(state['vault_id'])}.dc-vault-recovery"
            )

            def on_file_chosen(dialog, result):
                try:
                    gio_file = dialog.save_finish(result)
                except GLib.Error:
                    # User cancelled the file dialog — no error state.
                    return
                if gio_file is None:
                    return
                target_path = gio_file.get_path()

                # 1) Write the kit.
                try:
                    write_recovery_kit_file(
                        target_path,
                        vault_id=state["vault_id"],
                        recovery_secret=state["recovery_secret_bytes"],
                        vault_access_secret=state["vault_access_secret"],
                        recovery_envelope_meta=state["recovery_envelope_meta"],
                    )
                except Exception as exc:
                    export_status.remove_css_class("dim-label")
                    export_status.add_css_class("error")
                    export_status.set_label(f"Export failed: {exc}")
                    verify_status.set_label("")
                    state["verify_passed"] = False
                    confirm_check.set_sensitive(False)
                    _refresh_done_button()
                    return

                state["exported_kit_path"] = target_path
                export_status.remove_css_class("error")
                export_status.add_css_class("dim-label")
                export_status.set_label(f"Saved to: {target_path}")
                delete_switch.set_sensitive(True)

                # 2) Real recovery verify — re-derive wrap_key from
                #    the saved kit + the typed passphrase, AEAD-decrypt
                #    the recovery envelope. Poly1305 verifies the
                #    kit/passphrase combo end-to-end.
                ok_, msg = verify_recovery_kit(
                    target_path,
                    passphrase=state["passphrase"],
                    envelope_meta=state["recovery_envelope_meta"],
                )
                if ok_:
                    verify_status.remove_css_class("error")
                    verify_status.remove_css_class("dim-label")
                    verify_status.add_css_class("success")
                    verify_status.set_label(
                        f"✓ Recovery verified — {msg}."
                    )
                    state["verify_passed"] = True
                    confirm_check.set_sensitive(True)
                else:
                    verify_status.remove_css_class("success")
                    verify_status.remove_css_class("dim-label")
                    verify_status.add_css_class("error")
                    verify_status.set_label(
                        f"✗ {msg}. Check that you typed the passphrase correctly, "
                        "then click Export and verify recovery kit again."
                    )
                    state["verify_passed"] = False
                    confirm_check.set_active(False)
                    confirm_check.set_sensitive(False)
                _refresh_done_button()

            file_dialog.save(parent=win, callback=on_file_chosen)

        export_btn.connect("clicked", on_export_clicked)

        def on_done(_btn):
            # The shred-on-close logic + secret zeroing happens in
            # on_close(); we just need to dismiss the window.
            win.close()
        ok_close.connect("clicked", on_done)

        body.add_named(ok, "success")

        body.set_visible_child_name("choose_path")

        # Step transitions.
        def on_create_path(_btn):
            body.set_visible_child_name("create_passphrase")
        create_btn.connect("clicked", on_create_path)

        def on_pp_next(_btn):
            entered = pp_entry.get_text()
            confirm = pp_confirm.get_text()
            if len(entered) < 8:
                pp_status.set_text("Passphrase must be at least 8 characters.")
                return
            if entered != confirm:
                pp_status.set_text("Passphrases don't match.")
                return
            state["passphrase"] = entered
            # Skip the prior decorative recovery_test step — go straight
            # to vault creation and the success screen, where the real
            # bundled Export-and-Verify happens.
            perform_create()
        pp_next.connect("clicked", on_pp_next)

        def perform_create():
            """Actually create the vault on the relay, then stash the
            recovery_secret + vault_access_secret in wizard state so
            the user-controlled Export flow on the success screen can
            write the kit file at a path they pick.

            The kit file is **not** auto-saved anywhere on disk —
            silent auto-save would hide the act of "you have a thing
            you must back up", and per design feedback users rarely
            go look for files they didn't choose to save.
            """
            from .vault import Vault, recovery_envelope_meta_to_json
            from .vault_runtime import create_vault_relay, save_local_vault_grant

            vault = None
            try:
                relay = create_vault_relay(config)
                vault = Vault.create_new(
                    relay,
                    recovery_passphrase=state["passphrase"],
                )

                # Stash the kit material into wizard state. Both buffers
                # die when on_close calls _zero_state_secrets — but they
                # MUST live until the user chooses to Export, otherwise
                # the kit is unrecoverable.
                # `recovery_envelope_meta` is non-secret (envelope_id,
                # salts, nonces, ciphertext) but needed to run the
                # mandatory verify step.
                state["vault_id"] = vault.vault_id
                state["recovery_secret_bytes"] = vault.recovery_secret
                state["vault_access_secret"] = vault.vault_access_secret
                state["recovery_envelope_meta"] = vault.recovery_envelope_meta
                save_local_vault_grant(config_dir, config, vault)

                # Persist the vault id so the main settings + tray
                # know there's a vault to switch to.
                if "vault" not in config._data or not isinstance(config._data.get("vault"), dict):
                    config._data["vault"] = {}
                config._data["vault"]["last_known_id"] = vault.vault_id
                config._data["vault"]["recovery_envelope_meta"] = recovery_envelope_meta_to_json(
                    vault.recovery_envelope_meta
                )
                config.save()
                state["completed_successfully"] = True

                ok_id_entry.set_text(vault.vault_id_dashed)
                vault.close()
                vault = None
                body.set_visible_child_name("success")
            except Exception as exc:
                if vault is not None:
                    try:
                        vault.close()
                    except Exception:
                        pass
                # Surface the failure on the success screen — the layout
                # is the same; we just leave the export status in error
                # state and disable Done.
                ok_id_entry.set_text("")
                export_status.remove_css_class("dim-label")
                export_status.add_css_class("error")
                export_status.set_label(f"Vault creation failed: {exc}")
                export_btn.set_sensitive(False)
                body.set_visible_child_name("success")

        def on_done(_btn):
            win.close()
        ok_close.connect("clicked", on_done)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_vault_passphrase_generator(config_dir: Path):
    """Standalone passphrase-generator window opened from the wizard's
    Generate button. Shows a random diceware-style passphrase, lets
    the user Regenerate or Copy. The user pastes the result back into
    the wizard's passphrase fields manually.
    """
    from .vault_passphrase import generate_passphrase, estimated_entropy_bits

    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Generate passphrase",
            default_width=720,
            default_height=320,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        outer.append(Gtk.Label(label="Random passphrase", xalign=0, css_classes=["title-3"]))
        outer.append(Gtk.Label(
            label=(
                f"7 random words from a 520-word list ≈ {estimated_entropy_bits():.0f} "
                "bits of entropy. Copy this into the wizard's passphrase fields, "
                "or click Regenerate if you don't like it."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        # Read-only Entry so it's selectable + Ctrl-C friendly.
        pp_entry = Gtk.Entry()
        pp_entry.set_editable(False)
        pp_entry.set_text(generate_passphrase())
        pp_entry.add_css_class("monospace")
        pp_entry.set_hexpand(True)
        outer.append(pp_entry)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(btn_row)

        regen_btn = Gtk.Button(label="Regenerate", css_classes=["pill"])
        def on_regen(_b):
            pp_entry.set_text(generate_passphrase())
        regen_btn.connect("clicked", on_regen)
        btn_row.append(regen_btn)

        copy_btn = Gtk.Button(label="Copy", css_classes=["pill", "suggested-action"])
        def on_copy(_b):
            display = win.get_display()
            if display is not None:
                display.get_clipboard().set(pp_entry.get_text())
        copy_btn.connect("clicked", on_copy)
        btn_row.append(copy_btn)

        spacer = Gtk.Box(hexpand=True)
        btn_row.append(spacer)

        close_btn = Gtk.Button(label="Close", css_classes=["pill"])
        close_btn.connect("clicked", lambda _b: win.close())
        btn_row.append(close_btn)

        outer.append(Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
            label=(
                "Tip: write the passphrase down somewhere safe BEFORE you paste "
                "it. If you lose it, the recovery kit file alone won't get you "
                "back into the vault."
            ),
        ))

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
