"""Recovery tab + "Test recovery" dialog.

Extracted from ``windows_vault.py`` (lines ~183–549). Shape preserved:
the tab is a ``Gtk.Box`` with the emergency-recovery summary, the
export-reminder banner, and a "Test recovery now" button that opens
the in-vault recovery test dialog.
"""

import subprocess
import sys
import threading
from datetime import datetime, timezone

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ..vault import fresh_unlock
from ._main_context import MainContext


def build_recovery_tab(ctx: MainContext, win: "Adw.ApplicationWindow") -> "Gtk.Box":
    config = ctx.config
    config_dir = ctx.config_dir
    vault_id_undashed = ctx.vault_id_undashed
    log = ctx.log

    vault_meta = config._data.get("vault") if isinstance(config._data.get("vault"), dict) else {}
    recovery_status_text = (vault_meta or {}).get("recovery_status") or "Untested"
    recovery_last_tested = (vault_meta or {}).get("recovery_last_tested") or "—"

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
    # §5.H3: "Update recovery material" opens the rotation wizard
    # (vault-rotate subprocess) which rotates the access secret +
    # emits a fresh recovery kit. Disabled when no vault is loaded.
    update_recovery_btn = Gtk.Button(label="Update recovery material", css_classes=["pill"])
    update_recovery_btn.set_sensitive(bool(vault_id_undashed))
    if vault_id_undashed:
        update_recovery_btn.set_tooltip_text(
            "Rotate the vault's access secret. Generates a new recovery "
            "kit; existing kits and device grants stop working."
        )
    else:
        update_recovery_btn.set_tooltip_text(
            "Open a vault first — rotation requires a connected vault."
        )

    def on_update_recovery(_btn) -> None:
        subprocess.Popen(
            [
                sys.executable, "-m", "src.windows", "vault-rotate",
                f"--config-dir={config_dir}",
            ],
            close_fds=True,
        )

    update_recovery_btn.connect("clicked", on_update_recovery)
    actions.append(update_recovery_btn)

    # §6.H3: Export wizard launcher. Disabled when no vault is loaded
    # (no vault = nothing to export); the data layer's
    # ``write_export_bundle`` itself enforces the 8-char passphrase
    # floor, so this button is the safe entry point.
    export_bundle_btn = Gtk.Button(label="Export vault…", css_classes=["pill"])
    export_bundle_btn.set_sensitive(bool(vault_id_undashed))
    if vault_id_undashed:
        export_bundle_btn.set_tooltip_text(
            "Write a passphrase-encrypted .dcvault bundle to disk. "
            "Restorable via the Import wizard on any device with the "
            "matching vault_id."
        )
    else:
        export_bundle_btn.set_tooltip_text(
            "Open a vault first — export requires a connected vault."
        )

    def on_export_bundle(_btn) -> None:
        subprocess.Popen(
            [
                sys.executable, "-m", "src.windows", "vault-export",
                f"--config-dir={config_dir}",
            ],
            close_fds=True,
        )

    export_bundle_btn.connect("clicked", on_export_bundle)
    actions.append(export_bundle_btn)

    recovery_warning = Gtk.Label(
        label="Recovery has not been tested. Test it now to confirm you "
              "can actually restore the vault.",
        xalign=0,
        wrap=True,
    )
    recovery_warning.add_css_class("warning")
    # The "Recovery has not been tested" nag only makes sense when a
    # vault is actually loaded — without one there's nothing to
    # recover, so an orange CTA reads as an action the user can't
    # take. Empty state suppresses the warning; loaded state shows
    # it whenever recovery hasn't been verified for THIS vault.
    recovery_warning.set_visible(
        bool(vault_id_undashed)
        and recovery_status_text in ("Untested", "Stale")
    )
    recovery.append(recovery_warning)

    # F-501.5: Export reminder banner. Driven by
    # ``vault_export_reminder.should_show_export_reminder``; the
    # "Dismiss" button persists ``last_dismissed_at`` so the
    # cadence-based gate has somewhere to anchor.
    from ..vault.export.reminder import (
        normalize_cadence,
        should_show_export_reminder,
    )
    export_reminder_box = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        margin_top=12,
    )
    export_reminder_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
    export_reminder_label.add_css_class("warning")
    export_reminder_dismiss_btn = Gtk.Button(
        label="Dismiss", css_classes=["pill"],
    )
    export_reminder_box.append(export_reminder_label)
    export_reminder_box.append(export_reminder_dismiss_btn)
    export_reminder_box.set_visible(False)
    recovery.append(export_reminder_box)

    def _refresh_export_reminder() -> None:
        cadence = normalize_cadence(config.vault_export_reminder_cadence)
        if cadence == "off" or not vault_id_undashed:
            export_reminder_box.set_visible(False)
            return
        now_iso = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        # F-513: only the "due_again" state surfaces the banner.
        # ``first_run`` (never exported) is not a nag here — the
        # Recovery tab already shows the "Export vault now" CTA.
        should = should_show_export_reminder(
            last_export_at=config.vault_last_export_at,
            last_dismissed_at=config.vault_export_reminder_last_dismissed_at,
            cadence=cadence,
            now=now_iso,
        )
        if not should:
            export_reminder_box.set_visible(False)
            return
        last_export = config.vault_last_export_at or ""
        export_reminder_label.set_label(
            f"Vault hasn't been exported since {last_export[:10]}. "
            f"Export now to keep your recovery kit current "
            f"(cadence: {cadence})."
        )
        export_reminder_box.set_visible(True)

    def on_dismiss_export_reminder(_btn) -> None:
        now_iso = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        config.vault_export_reminder_last_dismissed_at = now_iso
        _refresh_export_reminder()

    export_reminder_dismiss_btn.connect(
        "clicked", on_dismiss_export_reminder,
    )
    _refresh_export_reminder()

    def refresh_recovery_summary(status: str, last_tested: str | None = None) -> None:
        recovery_value_labels["Status"].set_label(status)
        if last_tested is not None:
            recovery_value_labels["Last tested"].set_label(last_tested)
        recovery_warning.set_visible(
            bool(vault_id_undashed)
            and status in ("Untested", "Stale")
        )

    def open_recovery_test_dialog(_btn):
        log.info(
            "vault.recovery_test.clicked vault_id_present=%s config_meta_present=%s",
            bool(vault_id_undashed),
            isinstance((config._data.get("vault") or {}).get("recovery_envelope_meta"), dict),
        )
        try:
            from datetime import datetime, timezone
            from ..vault import recovery_envelope_meta_from_json, vault_id_dashed
            from ..vault.state.local_state import run_recovery_material_test

            # F-U17: ``Adw.Dialog`` (libadwaita 1.5+) replaces the
            # old ``Adw.ApplicationWindow`` shape so the recovery
            # tester:
            #   * floats over the parent vault-settings window with
            #     auto-handled transient ownership (no explicit
            #     ``set_transient_for`` / ``set_modal`` needed); and
            #   * auto-closes when the parent window closes, matching
            #     the lifecycle the rest of the vault settings tabs
            #     already give the user.
            # The pattern mirrors ``vault_connect_folder_dialog``.
            dialog = Adw.Dialog()
            dialog.set_title("Test recovery")
            dialog.set_content_width(560)
            dialog.set_content_height(420)

            extra = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=12,
                margin_top=16,
                margin_bottom=16,
                margin_start=16,
                margin_end=16,
            )
            dialog.set_child(extra)
            # Adw.Dialog draws its own title bar with the dialog's
            # ``title`` property; the inner title-2 label that used
            # to sit at the top of the body is now redundant. The
            # subhead below remains — it explains *what to do*,
            # which the title doesn't.
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
            # F-U10: placeholder text is *not* an accessible name. Bind
            # an explicit Gtk.Accessible label so AT-SPI / dogtail can
            # find these widgets by description rather than role only.
            kit_entry.update_property(
                [Gtk.AccessibleProperty.LABEL], ["Recovery kit file path"],
            )
            kit_row.append(kit_entry)
            browse_btn = Gtk.Button(label="Choose...", css_classes=["pill"])
            kit_row.append(browse_btn)
            extra.append(kit_row)

            vault_id_entry = Gtk.Entry(hexpand=True)
            vault_id_entry.set_text(vault_id_dashed(vault_id_undashed) if vault_id_undashed else "")
            vault_id_entry.set_placeholder_text("Vault ID")
            vault_id_entry.update_property(
                [Gtk.AccessibleProperty.LABEL], ["Vault ID"],
            )
            extra.append(vault_id_entry)

            extra.append(Gtk.Label(label="Recovery passphrase", xalign=0))
            passphrase_entry = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
            passphrase_entry.update_property(
                [Gtk.AccessibleProperty.LABEL], ["Recovery passphrase"],
            )
            extra.append(passphrase_entry)

            wipe_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            wipe_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            wipe_switch.update_property(
                [Gtk.AccessibleProperty.LABEL],
                ["Securely delete the recovery kit file after a successful test"],
            )
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
            # F-U09: prominent loss warning. The dim-label above
            # describes the action; this warning forces the user to
            # confront that a wipe is irreversible — without another
            # copy of the kit, the vault is unrecoverable. Per
            # `feedback_security_ux.md`, security-impacting toggles
            # must surface a visible loss warning, not bury it in
            # the description.
            wipe_warning = Gtk.Label(
                label=(
                    "⚠ Wipes the chosen file from disk after a successful test. "
                    "Make sure you have another copy of the kit (e.g. in a "
                    "password manager) — without it AND your passphrase, the "
                    "vault becomes permanently unrecoverable."
                ),
                xalign=0,
                wrap=True,
            )
            wipe_warning.add_css_class("warning")
            extra.append(wipe_warning)

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

                # F-U17: ``Gtk.FileDialog.open`` wants a real
                # ``Gtk.Window``; ``Adw.Dialog`` is a widget, not a
                # window, so reach through to the parent settings
                # window instead. Functionally identical — the file
                # picker still floats over the recovery dialog.
                file_dialog.open(parent=win, callback=on_file_chosen)

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
                # Review §6.C1: off-load the recovery verify (Argon2id
                # — 1-10s by spec) to a worker thread. A blocking call
                # here freezes the GTK main loop and reads as "the
                # app crashed", driving users to force-quit and lose
                # the recovery test. Mirror the worker shape used by
                # fresh_unlock_prompt.py:213-245 — settle on the main
                # thread via GLib.idle_add.
                set_status("Testing recovery...", "dim-label")
                test_btn.set_sensitive(False)
                close_btn.set_sensitive(False)
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

                kit = kit_path["path"]
                passphrase = passphrase_entry.get_text()
                vault_id_typed = vault_id_entry.get_text()
                wipe_after = wipe_switch.get_active()

                def worker() -> None:
                    try:
                        result = run_recovery_material_test(
                            kit,
                            passphrase=passphrase,
                            vault_id=vault_id_typed,
                            envelope_meta=meta,
                            wipe_after_success=wipe_after,
                        )
                        exc: Exception | None = None
                    except Exception as e:  # noqa: BLE001
                        result = None
                        exc = e
                        log.exception("vault.recovery_test.run.exception")

                    def settle() -> bool:
                        test_btn.set_sensitive(True)
                        close_btn.set_sensitive(True)
                        if exc is not None:
                            set_status(
                                "Recovery test failed unexpectedly. "
                                "Check the log for details.",
                                "error",
                            )
                            return False
                        log.info(
                            "vault.recovery_test.result ok=%s wiped=%s message=%s",
                            result.ok,
                            result.wiped,
                            result.message,
                        )
                        now = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%d %H:%M:%S UTC"
                        )
                        if (
                            "vault" not in config._data
                            or not isinstance(config._data.get("vault"), dict)
                        ):
                            config._data["vault"] = {}
                        config._data["vault"]["recovery_last_tested"] = now
                        if result.ok:
                            config._data["vault"]["recovery_status"] = "Verified"
                            config.save()
                            refresh_recovery_summary("Verified", now)
                            set_status(result.message, "success")
                            # F-LT11: a successful recovery test is the
                            # user typing the passphrase and Argon2id
                            # verifying it — same proof the mini-prompt
                            # asks for. Stamp so the next destructive
                            # op in this process picks up the active
                            # window.
                            fresh_unlock.stamp_fresh_unlock()
                            if result.wiped:
                                kit_path["path"] = None
                                kit_entry.set_text("")
                        else:
                            config._data["vault"]["recovery_status"] = "Failed"
                            config.save()
                            refresh_recovery_summary("Failed", now)
                            set_status(result.message, "error")
                        return False

                    GLib.idle_add(settle)

                threading.Thread(target=worker, daemon=True).start()

            test_btn.connect("clicked", on_test)
            # F-U17: ``Adw.Dialog.present(parent)`` ties the
            # dialog's lifecycle to the vault settings window.
            dialog.present(win)
            log.info("vault.recovery_test.dialog.presented")
        except Exception:
            log.exception("vault.recovery_test.dialog.exception")

    test_recovery_btn.connect("clicked", open_recovery_test_dialog)
    log.info("vault.recovery_test.button.connected vault_id_present=%s", bool(vault_id_undashed))
    return recovery
