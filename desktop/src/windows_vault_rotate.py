"""Access-secret rotation wizard (§5.H3).

GTK4 subprocess invoked from Vault Settings → Recovery → "Update
recovery material…". Walks the operator through:

1. **Confirm.** Two checkboxes that must be ticked: "existing kits
   stop working", "I'll save the new kit before closing". Continue
   stays disabled until both are checked.
2. **Verify existing kit.** Operator picks their current kit file
   + types the recovery passphrase. We run :func:`verify_recovery_kit`
   to confirm the passphrase derives the master key, then parse the
   kit to extract ``recovery_secret`` + ``recovery_envelope_meta`` —
   we need these unchanged in the post-rotation kit.
3. **Progress.** Worker thread generates a fresh access secret via
   :func:`generate_new_secret`, POSTs the rotation, and atomically
   updates the local keyring grant. Old secret is invalid the
   instant the relay returns 200.
4. **Save the new kit.** The kit content is rendered + a path
   picker writes it via :func:`write_recovery_kit_file`. Close is
   blocked until the operator confirms they've saved.

§5.H3 builds on existing primitives — server endpoint shipped at
T13.6, the access-rotation library is ready, and the recovery-kit
file format already accepts a swap-in ``vault_access_secret``.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .windows_common import _make_app


log = logging.getLogger(__name__)


def show_vault_rotate(config_dir: Path) -> None:
    """Top-level entry for ``vault-rotate`` subprocess."""
    from .config import Config
    from .vault import (
        parse_recovery_kit_file,
        recovery_envelope_meta_from_json,
        recovery_envelope_meta_to_json,
        verify_recovery_kit,
        write_recovery_kit_file,
    )
    from .vault.binding.runtime import (
        _vault_device_seed_provider,
        create_vault_relay,
        open_local_vault_from_grant,
    )
    from .vault.error_messages import humanize
    from .vault.grant.access_rotation import generate_new_secret
    from .vault.grant.rotate_client import (
        RotationAuthError,
        RotationError,
        RotationNotFoundError,
        RotationRateLimitedError,
        rotate_access_secret,
    )
    from .vault.grant.store import VaultGrant, open_default_grant_store
    from .vault.ui.window_args import resolve_active_vault_id

    config = Config(config_dir)
    app = _make_app()

    state: dict = {
        "step": "confirm",
        "kit_path": None,
        "recovery_secret": None,     # bytes
        "envelope_meta": None,
        "new_secret": None,
        "rotated_at": None,
        "kit_saved": False,
        "kit_save_path": None,
    }

    vault_id_undashed = resolve_active_vault_id(config, None)

    def on_activate(_app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)

        win = Adw.ApplicationWindow(
            application=app,
            title="Rotate vault access secret",
            default_width=620,
            default_height=520,
        )
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        win.set_content(toolbar)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=14,
            margin_top=20, margin_bottom=20, margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        stack.set_hexpand(True)
        stack.set_vexpand(True)
        outer.append(stack)

        def go_to(name: str) -> None:
            state["step"] = name
            stack.set_visible_child_name(name)

        # ===== Page 1: Confirm ====================================
        confirm_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        confirm_page.append(Gtk.Label(
            label="Rotate vault access secret",
            xalign=0, css_classes=["title-2"],
        ))
        confirm_page.append(Gtk.Label(
            label=(
                "Rotation generates a new access secret that the relay "
                "uses to authenticate this vault. All existing recovery "
                "kits and device grants stop working — they reference "
                "the OLD secret. You'll get a fresh kit at the end of "
                "this wizard; existing paired devices must be re-granted "
                "via the QR-grant flow."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        cb_kits = Gtk.CheckButton(
            label="I understand existing recovery kits stop working.",
        )
        confirm_page.append(cb_kits)
        cb_save = Gtk.CheckButton(
            label="I'll save the new kit before closing this window.",
        )
        confirm_page.append(cb_save)

        confirm_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        confirm_cancel = Gtk.Button(label="Cancel", css_classes=["pill"])
        confirm_cancel.connect("clicked", lambda _b: win.close())
        confirm_actions.append(confirm_cancel)
        confirm_continue = Gtk.Button(
            label="Continue",
            css_classes=["pill", "suggested-action"],
        )
        confirm_continue.set_sensitive(False)
        confirm_actions.append(confirm_continue)
        confirm_page.append(confirm_actions)
        stack.add_named(confirm_page, "confirm")

        def _refresh_continue(_w=None) -> None:
            confirm_continue.set_sensitive(
                cb_kits.get_active() and cb_save.get_active(),
            )

        cb_kits.connect("toggled", _refresh_continue)
        cb_save.connect("toggled", _refresh_continue)

        # ===== Page 2: Verify existing kit ========================
        verify_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        verify_page.append(Gtk.Label(
            label="Verify your current recovery kit",
            xalign=0, css_classes=["title-2"],
        ))
        verify_page.append(Gtk.Label(
            label=(
                "Pick your current recovery kit + type the recovery "
                "passphrase. We need to verify them so the new kit "
                "carries the same passphrase-encrypted material."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        kit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        kit_entry = Gtk.Entry(
            placeholder_text="Recovery kit file", editable=False, hexpand=True,
        )
        kit_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Recovery kit file path"],
        )
        kit_row.append(kit_entry)
        browse_btn = Gtk.Button(label="Choose…", css_classes=["pill"])
        kit_row.append(browse_btn)
        verify_page.append(kit_row)

        passphrase_entry = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
        passphrase_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Recovery passphrase"],
        )
        verify_page.append(passphrase_entry)

        verify_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        verify_page.append(verify_status)

        verify_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        verify_cancel = Gtk.Button(label="Cancel", css_classes=["pill"])
        verify_cancel.connect("clicked", lambda _b: win.close())
        verify_actions.append(verify_cancel)
        verify_continue = Gtk.Button(
            label="Verify and continue",
            css_classes=["pill", "suggested-action"],
        )
        verify_actions.append(verify_continue)
        verify_page.append(verify_actions)
        stack.add_named(verify_page, "verify")

        # ===== Page 3: Progress ===================================
        progress_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        progress_page.append(Gtk.Label(
            label="Rotating access secret…",
            xalign=0, css_classes=["title-2"],
        ))
        progress_spinner = Gtk.Spinner()
        progress_spinner.set_size_request(48, 48)
        progress_spinner.set_halign(Gtk.Align.START)
        progress_page.append(progress_spinner)
        progress_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        progress_page.append(progress_status)
        stack.add_named(progress_page, "progress")

        # ===== Page 4: Save new kit ===============================
        save_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        save_page.append(Gtk.Label(
            label="Save your new recovery kit",
            xalign=0, css_classes=["title-2"],
        ))
        save_page.append(Gtk.Label(
            label=(
                "The rotation is committed on the relay. Save the new "
                "kit now — without it AND your passphrase, the vault is "
                "unrecoverable. Existing paired devices must be "
                "re-granted via QR-grant."
            ),
            xalign=0, wrap=True, css_classes=["warning"],
        ))
        save_path_label = Gtk.Label(xalign=0, wrap=True, css_classes=["monospace"])
        save_page.append(save_path_label)
        save_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        save_page.append(save_status)

        save_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        save_choose = Gtk.Button(
            label="Save new kit…",
            css_classes=["pill", "suggested-action"],
        )
        save_actions.append(save_choose)
        save_close = Gtk.Button(label="Close", css_classes=["pill"])
        save_close.set_sensitive(False)
        save_close.set_tooltip_text(
            "Save the new kit before closing — otherwise the vault is "
            "unrecoverable if you lose this device.",
        )
        save_close.connect("clicked", lambda _b: win.close())
        save_actions.append(save_close)
        save_page.append(save_actions)
        stack.add_named(save_page, "save_kit")

        # ===== Page 5: Error ======================================
        error_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        error_page.append(Gtk.Label(
            label="Rotation could not complete",
            xalign=0, css_classes=["title-2"],
        ))
        error_status = Gtk.Label(xalign=0, wrap=True, css_classes=["error"])
        error_page.append(error_status)
        err_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        err_close = Gtk.Button(label="Close", css_classes=["pill"])
        err_close.connect("clicked", lambda _b: win.close())
        err_actions.append(err_close)
        error_page.append(err_actions)
        stack.add_named(error_page, "error")

        # --- handlers -------------------------------------------------
        def _set_verify_status(text: str, kind: str = "neutral") -> None:
            verify_status.set_label(text)
            verify_status.remove_css_class("error")
            verify_status.remove_css_class("success")
            verify_status.remove_css_class("dim-label")
            if kind == "error":
                verify_status.add_css_class("error")
            elif kind == "success":
                verify_status.add_css_class("success")
            else:
                verify_status.add_css_class("dim-label")

        def on_continue_to_verify(_btn) -> None:
            if not vault_id_undashed:
                error_status.set_label(
                    "No vault is connected on this machine. Open Vault "
                    "Settings on the device that holds the vault first."
                )
                go_to("error")
                return
            go_to("verify")

        confirm_continue.connect("clicked", on_continue_to_verify)

        def on_browse(_btn) -> None:
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Choose current recovery kit")

            def on_chosen(_d, result):
                try:
                    gio_file = file_dialog.open_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path = gio_file.get_path()
                if not path:
                    return
                state["kit_path"] = path
                kit_entry.set_text(path)

            file_dialog.open(parent=win, callback=on_chosen)

        browse_btn.connect("clicked", on_browse)

        def on_verify(_btn) -> None:
            kit = state["kit_path"]
            passphrase = passphrase_entry.get_text()
            if not kit:
                _set_verify_status("Pick your current recovery kit first.", "error")
                return
            if not passphrase:
                _set_verify_status("Type your recovery passphrase.", "error")
                return

            try:
                meta = recovery_envelope_meta_from_json(
                    (config._data.get("vault") or {}).get("recovery_envelope_meta")
                )
            except Exception:  # noqa: BLE001
                meta = None
            if meta is None:
                _set_verify_status(
                    "Recovery envelope metadata is missing from this "
                    "device's config; rotation requires the originating "
                    "device. Use the admin device that created the vault.",
                    "error",
                )
                return

            verify_continue.set_sensitive(False)
            browse_btn.set_sensitive(False)
            _set_verify_status("Verifying kit + passphrase (Argon2id, 1–10 s)…")

            def worker() -> None:
                err: Exception | None = None
                ok = False
                msg = ""
                parsed: dict | None = None
                try:
                    ok, msg = verify_recovery_kit(
                        kit, passphrase=passphrase, envelope_meta=meta,
                    )
                    if ok:
                        parsed = parse_recovery_kit_file(kit)
                except Exception as exc:  # noqa: BLE001
                    err = exc

                def settle() -> bool:
                    verify_continue.set_sensitive(True)
                    browse_btn.set_sensitive(True)
                    if err is not None:
                        _set_verify_status(
                            f"Could not verify kit: {humanize(err)}", "error",
                        )
                        return False
                    if not ok:
                        _set_verify_status(msg or "Kit + passphrase did not match.", "error")
                        return False
                    state["recovery_secret"] = parsed["recovery_secret"]
                    state["envelope_meta"] = meta
                    _start_rotation()
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        verify_continue.connect("clicked", on_verify)

        def _start_rotation() -> None:
            go_to("progress")
            progress_spinner.start()
            progress_status.set_label("Generating new secret + posting to relay…")

            def worker() -> None:
                err: Exception | None = None
                rotated_at: str | None = None
                new_secret = generate_new_secret()
                state["new_secret"] = new_secret
                old_secret: str | None = None
                master_key: bytes | None = None
                try:
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(
                        config_dir, config, vault_id_undashed,
                    )
                    try:
                        master_key = bytes(vault.master_key) if vault.master_key else None
                        old_secret = vault.vault_access_secret
                    finally:
                        vault.close()
                    if not old_secret or master_key is None:
                        raise RuntimeError("local vault grant is closed / missing material")

                    response = rotate_access_secret(
                        relay, vault_id_undashed, old_secret, new_secret,
                    )
                    rotated_at = response.rotated_at
                    log.info(
                        "vault.rotate.server_committed vault=%s rotated_at=%s",
                        vault_id_undashed[:12], rotated_at,
                    )

                    # Atomically swap the local grant before the next
                    # vault op so cached state never points at a dead
                    # secret. Pre-rotation operations would 401 on the
                    # relay after this point.
                    new_grant = VaultGrant.from_bytes(
                        vault_id_undashed, master_key, new_secret,
                    )
                    try:
                        store = open_default_grant_store(
                            config_dir=Path(config_dir),
                            device_seed_provider=_vault_device_seed_provider(
                                Path(config_dir), config,
                            ),
                        )
                        store.save(new_grant)
                    finally:
                        new_grant.zero()
                except Exception as exc:  # noqa: BLE001
                    err = exc

                # Best-effort scrub of the master_key copy we held.
                if master_key is not None:
                    try:
                        ba = bytearray(master_key)
                        for i in range(len(ba)):
                            ba[i] = 0
                    except Exception:  # noqa: BLE001
                        pass

                def settle() -> bool:
                    progress_spinner.stop()
                    if err is not None:
                        msg = _humanize_rotation_error(err)
                        error_status.set_label(msg)
                        go_to("error")
                        return False
                    state["rotated_at"] = rotated_at
                    _open_save_kit_page()
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        def _open_save_kit_page() -> None:
            now_iso = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )
            save_path_label.set_label("No save location yet.")
            save_status.set_label(
                "Rotation committed at " +
                (state["rotated_at"] or now_iso) +
                ". Choose where to save the new kit."
            )
            save_close.set_sensitive(False)
            save_close.set_tooltip_text(
                "Save the new kit before closing — otherwise the vault "
                "is unrecoverable if you lose this device.",
            )
            go_to("save_kit")

        def on_save_choose(_btn) -> None:
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Save new recovery kit")
            file_dialog.set_initial_name(
                f"vault-recovery-kit-{vault_id_undashed[:8]}.txt"
            )

            def on_chosen(_d, result):
                try:
                    gio_file = file_dialog.save_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path = gio_file.get_path()
                if not path:
                    return
                _write_kit(Path(path))

            file_dialog.save(parent=win, callback=on_chosen)

        save_choose.connect("clicked", on_save_choose)

        def _write_kit(path: Path) -> None:
            try:
                write_recovery_kit_file(
                    path,
                    vault_id=vault_id_undashed,
                    recovery_secret=state["recovery_secret"],
                    vault_access_secret=state["new_secret"],
                    recovery_envelope_meta=state["envelope_meta"],
                )
                state["kit_saved"] = True
                state["kit_save_path"] = str(path)
                save_path_label.set_label(f"Saved to: {path}")
                save_status.set_label(
                    "New kit saved. You can close this window."
                )
                save_close.set_sensitive(True)
                save_close.set_tooltip_text("")
                save_choose.set_label("Save again")
                log.info(
                    "vault.rotate.kit_saved vault=%s",
                    vault_id_undashed[:12],
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "vault.rotate.kit_save_failed error=%s", exc,
                )
                save_status.set_label(
                    f"Could not save kit: {humanize(exc)}. "
                    "Try a different location."
                )
                save_status.remove_css_class("dim-label")
                save_status.add_css_class("error")

        def on_close(_w) -> bool:
            # If we're past rotation but the kit wasn't saved, surface
            # a confirmation. Otherwise allow close.
            if state.get("rotated_at") and not state["kit_saved"]:
                dlg = Adw.AlertDialog(
                    heading="Close without saving the kit?",
                    body=(
                        "The rotation is committed on the relay. If you "
                        "close now without saving the new kit, recovery "
                        "is lost — there is no second chance to download "
                        "this kit. Continue?"
                    ),
                )
                dlg.add_response("cancel", "Keep open")
                dlg.add_response("close", "Close anyway")
                dlg.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
                dlg.set_default_response("cancel")
                dlg.set_close_response("cancel")

                def on_resp(_d, response: str) -> None:
                    if response == "close":
                        log.warning(
                            "vault.rotate.kit_save_failed reason=user_force_close vault=%s",
                            vault_id_undashed[:12],
                        )
                        win.destroy()

                dlg.connect("response", on_resp)
                dlg.present(win)
                return True  # block close until user confirms
            return False

        win.connect("close-request", on_close)
        apply_pointer_cursors(win)

        log.info(
            "vault.rotate.started vault=%s",
            (vault_id_undashed or "?")[:12],
        )
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ----- helpers -------------------------------------------------------


def _humanize_rotation_error(exc: Exception) -> str:
    from .vault.error_messages import humanize
    from .vault.grant.rotate_client import (
        RotationAuthError,
        RotationNotFoundError,
        RotationRateLimitedError,
    )

    if isinstance(exc, RotationAuthError):
        return (
            "Admin role required to rotate the access secret. Use the "
            "admin device that created the vault."
        )
    if isinstance(exc, RotationRateLimitedError):
        return (
            "The relay refused rotation — too soon after the previous "
            "rotation. Try again later."
        )
    if isinstance(exc, RotationNotFoundError):
        return (
            "The relay reports this vault no longer exists. Reopen "
            "Vault Settings to refresh."
        )
    return f"Rotation aborted: {humanize(exc)}"


__all__ = ["show_vault_rotate"]
