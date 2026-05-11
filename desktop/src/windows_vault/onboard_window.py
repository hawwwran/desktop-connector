"""Vault create / import wizard (T3.6).

Extracted from ``windows_vault.py`` (lines ~1589–2380). Behaviour is
preserved exactly: relay picker → recovery passphrase → deriving spinner
→ success screen with mandatory export+verify of the recovery kit.
"""

import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ..brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..windows_common import _make_app


def show_vault_onboard(config_dir: Path):
    """Vault create / import wizard (T3.6).

    Two paths: 'Create new vault' (full M1 flow) and 'Import from
    export' (the import wizard launches via :mod:`windows_vault_import`
    and lives in its own subprocess). The create flow walks:
        1. relay picker (uses the existing ``server_url`` if set)
        2. recovery passphrase entry + confirm
        3. recovery-test prompt with Skip option
        4. success screen

    Cancel behaviour (revised 2026-05-03 vs T0 §A2): cancelling never
    flips ``Config.vault_active``. The toggle stays where the user put
    it. The wizard does, however, scrub its own partial state — see
    :func:`vault_ui_state.wizard_cancel_rule` for the rationale and
    :func:`on_close` below for the per-phase cleanup
    (grant_saved-but-not-published artifacts get reaped; in-memory
    secrets are zeroed; the optional "delete kit after close" toggle
    runs the shredder).
    """
    from ..config import Config
    from ..vault_ui_state import wizard_cancel_rule

    config = Config(config_dir)

    # Wizard state — held in a dict so nested closures can mutate.
    # `recovery_secret_bytes` and `vault_access_secret` are stashed
    # post-create so the Export+Verify button can build the kit
    # content on demand (no silent auto-save anywhere). Both are
    # zeroed when the wizard closes. `recovery_envelope_meta` is
    # non-secret but needed to run the real recovery test.
    state = {
        "step": "choose_path",        # → create_passphrase → success
        "passphrase": "",
        "completed_successfully": False,
        "vault_id": None,
        "recovery_secret_bytes": None,
        "vault_access_secret": None,
        "recovery_envelope_meta": None,
        "exported_kit_path": None,
        "verify_passed": False,
        "delete_after_close": False,
        # T8-pre safety net: defer the relay POST until the local grant
        # is durable. The wizard transitions through three flags so the
        # cancel cleanup knows what to undo on a partial run:
        #   grant_saved → save_local_vault_grant returned without raising
        #   published   → publish_initial returned without raising
        # Both must be true (plus completed_successfully) for a real vault.
        "vault": None,                # prepared Vault held until publish
        "grant_saved": False,
        "published": False,
        # F-LT03: set by on_close so any in-flight perform_create worker
        # checks it between phases and stops before touching the
        # keyring or the relay. The on_close cleanup paths still run as
        # before — the flag just shortens the worker so it doesn't
        # *start* new side-effects after the user has closed the
        # window.
        "wizard_cancelled": threading.Event(),
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
        # F-LT03: short any in-flight perform_create worker before it
        # advances to the next phase. The cleanup branches below still
        # run for whatever already landed (saved grant, published row).
        cancel_event = state.get("wizard_cancelled")
        if cancel_event is not None:
            cancel_event.set()

        # If "Safely delete after close" is on AND a kit file was
        # exported during this wizard session, shred it now.
        if state.get("delete_after_close") and state.get("exported_kit_path"):
            from ..vault import shred_file
            shred_file(state["exported_kit_path"])

        # Partial-state cleanup, by phase. Per
        # `feedback_respect_user_intent.md`: clean up partial state on
        # cancel, but never auto-flip the user's toggle.
        #
        # The new prepare → save_grant → publish → save_config order
        # means:
        #   * grant_saved and NOT published → the local grant is for a
        #     vault that does NOT exist on the relay. Drop it so the
        #     keyring/file backend doesn't accumulate dead entries.
        #   * published and NOT completed_successfully → an extremely
        #     rare case where publish succeeded but the config write
        #     immediately after failed. Both the relay vault and the
        #     grant are real; recovery script can wire up
        #     last_known_id later. We don't auto-clean here because the
        #     vault is usable.
        if state.get("grant_saved") and not state.get("published") and state.get("vault_id"):
            try:
                from ..vault.grant.grant import delete_local_grant_artifacts
                delete_local_grant_artifacts(Path(config.config_dir), state["vault_id"])
            except Exception:
                pass

        # Close the in-memory Vault to zero its master_key. Idempotent
        # if perform_create already closed it.
        prepared = state.get("vault")
        if prepared is not None:
            try:
                prepared.close()
            except Exception:
                pass
            state["vault"] = None

        # The wizard's cancel rule (revised 2026-05-03) never changes
        # the toggle — the user's deliberate ON stays ON. Function
        # call kept for signature stability.
        wizard_cancel_rule()

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
        # Accessible names so screen readers (and AT-SPI test drivers)
        # can disambiguate the two PasswordEntries — they're otherwise
        # both reported as anonymous "password text" widgets.
        pp_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Recovery passphrase"],
        )
        pp_confirm.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Confirm passphrase"],
        )

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
                   else str(Path(__file__).resolve().parent.parent.parent))
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

        # Step 2.5 — "deriving key" panel shown while Argon2id stretches
        # the passphrase. Argon2id is intentionally memory-hard (~1–10 s
        # depending on the host); on the GTK main thread it would block
        # repaints and look like a crash, so the worker runs it off-thread
        # and this panel stays visible until phase 3 hands us either the
        # success screen or a phase-1/2 failure that throws us back to
        # the passphrase step.
        deriving = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        deriving.append(Gtk.Label(
            label="Deriving key…", xalign=0, css_classes=["title-2"],
        ))
        deriving_spinner_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
        )
        deriving_spinner = Gtk.Spinner()
        deriving_spinner_row.append(deriving_spinner)
        deriving_spinner_row.append(Gtk.Label(
            label="Stretching your passphrase with Argon2id.",
            xalign=0,
        ))
        deriving.append(deriving_spinner_row)
        deriving.append(Gtk.Label(
            label=(
                "This is intentional — it's what stops attackers from "
                "brute-forcing your vault. Hold on for a few seconds."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        body.add_named(deriving, "deriving_key")

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

        # Publish-failure retry button. Hidden on the happy path; shown
        # only when prepare + grant-save succeeded but publish_initial
        # raised. Reusing the same Vault instance makes the retry POST
        # byte-identical, so a relay flake doesn't fork the local grant
        # against a different vault_id.
        retry_publish_btn = Gtk.Button(
            label="Retry publish",
            css_classes=["pill", "suggested-action"],
        )
        retry_publish_btn.set_visible(False)
        ok.append(retry_publish_btn)

        # "Safely delete" toggle — only meaningful after an export.
        # Shown but disabled until the user has actually picked a path.
        delete_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ok.append(delete_row)
        delete_switch = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=False)
        # F-U10: AT-SPI accessible name. The success screen's top warning
        # already covers the user-loss case, so no separate F-U09-style
        # warning here — but the switch still needs a label string so
        # AT-SPI tools find it by description, not just by role.
        delete_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Securely delete the exported recovery kit file when this wizard closes"],
        )
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
            from ..vault import (
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
            # F-U23: enforce a stricter minimum + nudge typed input
            # toward the Generate button for short / non-mixed inputs.
            if len(entered) < 12:
                pp_status.set_text(
                    "Passphrase must be at least 12 characters. The "
                    "Generate button produces a stronger 7-word phrase."
                )
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

        def _set_export_status_error(message: str) -> None:
            export_status.remove_css_class("dim-label")
            export_status.add_css_class("error")
            export_status.set_label(message)

        def _commit_after_publish(vault) -> None:
            """Final step once the relay has accepted the bundle:
            persist last_known_id + envelope meta to config.json, mark
            the wizard completed, swap to the success screen.
            """
            from ..vault import recovery_envelope_meta_to_json

            if "vault" not in config._data or not isinstance(config._data.get("vault"), dict):
                config._data["vault"] = {}
            config._data["vault"]["last_known_id"] = vault.vault_id
            config._data["vault"]["recovery_envelope_meta"] = recovery_envelope_meta_to_json(
                vault.recovery_envelope_meta
            )
            config.save()
            state["completed_successfully"] = True

            ok_id_entry.set_text(vault.vault_id_dashed)
            export_status.remove_css_class("error")
            export_status.add_css_class("dim-label")
            export_status.set_label("")
            retry_publish_btn.set_visible(False)
            export_btn.set_sensitive(True)

            # Master key + recovery secret aren't needed beyond this
            # point (the export flow reads them from wizard state, not
            # from the live Vault instance).
            try:
                vault.close()
            except Exception:
                pass
            state["vault"] = None

        def perform_create():
            """Defer-the-relay-create wizard transition (T8-pre).

            Order:
              1. ``Vault.prepare_new`` — pure crypto, no relay POST.
              2. ``save_local_vault_grant`` — durable local unlock.
              3. ``Vault.publish_initial`` — first relay write.
              4. ``config.save`` — last_known_id + envelope meta.

            If step 1 or 2 fails, no vault row exists on the relay and
            no orphan accumulates. If step 3 fails, the user sees a
            "Retry publish" button on the success screen that re-runs
            POST against the same prepared bundle (so a relay flake
            doesn't fork the local grant against a different vault_id).

            The kit file itself is **not** auto-saved anywhere — silent
            auto-save would hide the act of "you have a thing you must
            back up", and per design feedback users rarely go look for
            files they didn't choose to save.

            F-LT01: phases 1–3 run in a worker thread so the GTK main
            loop keeps repainting during Argon2id derivation and the
            relay POST. The "deriving_key" panel stays visible until
            we either swap to success (phase 3 ok) or back to the
            passphrase step (phase 1/2 failure).

            F-LT03 (orphan-row protection): phase 3 + the config commit
            run in the same worker call so the window between "vault on
            relay" and "vault recorded in config.json" closes
            sub-millisecond. If the user shuts the wizard during
            derivation, ``state["wizard_cancelled"]`` shorts the worker
            before it touches the keyring or the relay; the existing
            on_close cleanup reaps any partial state that already
            landed.
            """
            from ..vault import Vault
            from ..vault.binding.runtime import create_vault_relay, save_local_vault_grant

            body.set_visible_child_name("deriving_key")
            deriving_spinner.start()
            pp_next.set_sensitive(False)
            passphrase = state["passphrase"]
            cancelled: threading.Event = state["wizard_cancelled"]

            def back_to_passphrase(message: str) -> bool:
                deriving_spinner.stop()
                pp_next.set_sensitive(True)
                pp_status.set_text(message)
                body.set_visible_child_name("create_passphrase")
                return False

            def handle_phase3_failure(vault, exc: Exception) -> bool:
                deriving_spinner.stop()
                body.set_visible_child_name("success")
                ok_id_entry.set_text(vault.vault_id_dashed)
                _set_export_status_error(
                    f"Vault prepared locally but the relay rejected the "
                    f"first publish: {exc}. Click Retry publish to try "
                    "again with the same vault material."
                )
                export_btn.set_sensitive(False)
                retry_publish_btn.set_visible(True)
                return False

            def handle_success_ui(vault) -> bool:
                deriving_spinner.stop()
                body.set_visible_child_name("success")
                ok_id_entry.set_text(vault.vault_id_dashed)
                export_status.remove_css_class("error")
                export_status.add_css_class("dim-label")
                export_status.set_label("")
                retry_publish_btn.set_visible(False)
                export_btn.set_sensitive(True)
                # Master key + recovery secret aren't needed beyond this
                # point (the export flow reads them from wizard state, not
                # from the live Vault instance).
                try:
                    vault.close()
                except Exception:
                    pass
                state["vault"] = None
                return False

            def handle_commit_failure(exc: Exception) -> bool:
                deriving_spinner.stop()
                body.set_visible_child_name("success")
                _set_export_status_error(
                    f"Vault published, but config.json could not be "
                    f"updated: {exc}. Restart the app — the vault is "
                    "real on the relay; the recovery script can re-link "
                    "it locally."
                )
                export_btn.set_sensitive(False)
                return False

            def worker() -> None:
                from ..vault import recovery_envelope_meta_to_json

                if cancelled.is_set():
                    return

                # Phase 1 — prepare in memory only (Argon2id-heavy).
                try:
                    vault = Vault.prepare_new(recovery_passphrase=passphrase)
                except Exception as exc:
                    GLib.idle_add(
                        back_to_passphrase, f"Could not prepare vault: {exc}",
                    )
                    return

                if cancelled.is_set():
                    try:
                        vault.close()
                    except Exception:
                        pass
                    return

                state["vault"] = vault
                state["vault_id"] = vault.vault_id
                state["recovery_secret_bytes"] = vault.recovery_secret
                state["vault_access_secret"] = vault.vault_access_secret
                state["recovery_envelope_meta"] = vault.recovery_envelope_meta

                # Phase 2 — local grant, before any relay write. A failure
                # here means the keyring/file fallback couldn't store the
                # unlock material on this machine; retrying the wizard is
                # safe because no relay vault exists yet.
                try:
                    save_local_vault_grant(config_dir, config, vault)
                    state["grant_saved"] = True
                except Exception as exc:
                    try:
                        vault.close()
                    except Exception:
                        pass
                    state["vault"] = None
                    GLib.idle_add(
                        back_to_passphrase,
                        f"Could not save the local unlock material: {exc}. "
                        "Install a Secret Service backend (gnome-keyring / "
                        "kwallet) or re-launch and try again.",
                    )
                    return

                if cancelled.is_set():
                    # The grant is on disk; on_close's existing
                    # grant_saved-and-not-published branch will reap it.
                    return

                # Phases 3 + 4 — relay POST then config commit, both in
                # the worker so the orphan window between them is
                # whatever ``config.save()`` takes (microseconds on a
                # local disk). If cancellation lands between these two
                # we still complete the commit: a published vault that
                # is NOT in config.json is the orphan we're trying to
                # avoid.
                try:
                    relay = create_vault_relay(config)
                    vault.publish_initial(relay)
                    state["published"] = True
                except Exception as exc:
                    GLib.idle_add(handle_phase3_failure, vault, exc)
                    return

                try:
                    if "vault" not in config._data or not isinstance(
                        config._data.get("vault"), dict
                    ):
                        config._data["vault"] = {}
                    config._data["vault"]["last_known_id"] = vault.vault_id
                    config._data["vault"]["recovery_envelope_meta"] = (
                        recovery_envelope_meta_to_json(vault.recovery_envelope_meta)
                    )
                    config.save()
                    state["completed_successfully"] = True
                except Exception as exc:
                    GLib.idle_add(handle_commit_failure, exc)
                    return

                GLib.idle_add(handle_success_ui, vault)

            threading.Thread(target=worker, daemon=True).start()

        def on_retry_publish(_btn) -> None:
            """User-triggered retry after a phase-3 failure.

            Reuses the in-memory ``Vault`` so the publish payload is
            byte-identical. If the relay accepts on retry, the wizard
            transitions to the normal post-publish state.
            """
            from ..vault.binding.runtime import create_vault_relay

            vault = state.get("vault")
            if vault is None or not vault.has_pending_publish:
                _set_export_status_error(
                    "No pending publish to retry. Close this window and "
                    "re-open the wizard."
                )
                retry_publish_btn.set_visible(False)
                return

            retry_publish_btn.set_sensitive(False)
            export_status.remove_css_class("error")
            export_status.add_css_class("dim-label")
            export_status.set_label("Retrying publish…")

            try:
                relay = create_vault_relay(config)
                vault.publish_initial(relay)
                state["published"] = True
            except Exception as exc:
                _set_export_status_error(
                    f"Retry failed: {exc}. The local unlock material is "
                    "still saved; you can close this window and try again "
                    "later from the Vault setup wizard."
                )
                retry_publish_btn.set_sensitive(True)
                return

            try:
                _commit_after_publish(vault)
            except Exception as exc:
                _set_export_status_error(
                    f"Vault published, but config.json could not be "
                    f"updated: {exc}."
                )

        retry_publish_btn.connect("clicked", on_retry_publish)

        def on_done(_btn):
            win.close()
        ok_close.connect("clicked", on_done)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
