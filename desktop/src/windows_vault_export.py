"""Vault export bundle wizard (§6.H3).

GTK4 subprocess (`vault-export`) that wraps
:func:`vault.export.bundle.write_export_bundle` in a 4-page flow:

1. **Setup.** ``Gtk.FileDialog`` for the destination path (default
   filename ``vault-export-<YYYY-MM-DD>.dcvault``), passphrase + confirm
   fields with an inline strength hint. Continue stays disabled until
   both fields agree, both pass the 8-char floor (the data-layer
   enforces this with :data:`EXPORT_PASSPHRASE_MIN_LEN`), and the
   path is set.
2. **Progress.** Worker thread runs the Argon2id-derive + bundle-write
   sequence with a callback wired to two progress bars.
3. **Verify.** Default-on — we re-read the bundle we just wrote
   (:func:`read_export_bundle`) to catch a corrupt write before the
   operator is told it's safe to delete the original. Surfacing any
   mismatch is the v1 protection against the rare "bundle written
   without a final fsync that gets the bits wrong" failure class.
4. **Success.** SHA-256 of the bundle bytes, path, and an opt-in
   "Shred bundle" action that runs :func:`shred_file` after a
   confirmation dialog (per ``feedback_security_ux.md``: destructive
   actions surface a visible loss warning).

Cancel + close at any pre-write stage is safe — the writer uses an
atomic-rename pattern, so a killed mid-derivation run leaves no
``vault-export-…dcvault`` file. The temporary ``.dc-temp-<rand>``
file may linger but is reaped on the next run.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


EXPORT_PASSPHRASE_RECOMMENDED_LEN = 16


def show_vault_export(config_dir: Path) -> None:
    """Top-level entry for the ``vault-export`` subprocess."""
    from .config import Config
    from .vault import shred_file
    from .vault.binding.runtime import (
        create_vault_relay,
        open_local_vault_from_grant,
    )
    from .vault.error_messages import humanize
    from .vault.export.bundle import (
        EXPORT_PASSPHRASE_MIN_LEN,
        ExportError,
        ExportProgress,
        read_export_bundle,
        write_export_bundle,
    )
    from .vault.ui.window_args import resolve_active_vault_id

    config = Config(config_dir)
    app = _make_app()

    vault_id_undashed = resolve_active_vault_id(config, None)

    state: dict[str, Any] = {
        "step": "setup",
        "output_path": None,                  # Path
        "passphrase": None,                   # str
        "result_path": None,                  # Path
        "result_sha256": None,                # str
        "verify_ok": None,                    # bool | None
        "verify_message": "",
        "cancel": threading.Event(),
        "verify_default_on": True,
    }

    def on_activate(_app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)

        win = Adw.ApplicationWindow(
            application=app,
            title="Export vault bundle",
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

        # ===== Page 1: Setup ======================================
        setup_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        setup_page.append(Gtk.Label(
            label="Export vault bundle",
            xalign=0, css_classes=["title-2"],
        ))
        setup_page.append(Gtk.Label(
            label=(
                "Writes every encrypted chunk + manifest revision to a "
                "single ``.dcvault`` file you can move offline (USB, "
                "encrypted backup, separate machine). Decrypting the "
                "bundle needs the passphrase you choose here AND the "
                "matching vault_id — relay credentials are not used."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        # Destination row.
        dest_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dest_entry = Gtk.Entry(
            placeholder_text="Bundle destination", editable=False, hexpand=True,
        )
        dest_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Bundle destination path"],
        )
        dest_row.append(dest_entry)
        dest_choose = Gtk.Button(label="Choose…", css_classes=["pill"])
        dest_row.append(dest_choose)
        setup_page.append(dest_row)

        # Passphrase entries.
        pp_entry = Gtk.PasswordEntry(
            show_peek_icon=True, hexpand=True,
        )
        pp_entry.set_placeholder_text("Export passphrase")
        pp_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Export passphrase"],
        )
        setup_page.append(pp_entry)

        pp_confirm = Gtk.PasswordEntry(
            show_peek_icon=True, hexpand=True,
        )
        pp_confirm.set_placeholder_text("Confirm passphrase")
        pp_confirm.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Confirm export passphrase"],
        )
        setup_page.append(pp_confirm)

        strength_label = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
        )
        setup_page.append(strength_label)

        # Verify-after-write toggle.
        verify_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        verify_switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=True)
        verify_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Verify bundle after writing"],
        )
        verify_row.append(verify_switch)
        verify_row.append(Gtk.Label(
            label=(
                "Verify the bundle by reading it back after writing. "
                "Catches the rare case where a write completes but the "
                "bytes on disk don't match what was meant. Default on."
            ),
            xalign=0, wrap=True, hexpand=True, css_classes=["dim-label"],
        ))
        setup_page.append(verify_row)

        setup_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        setup_cancel = Gtk.Button(label="Cancel", css_classes=["pill"])
        setup_cancel.connect("clicked", lambda _b: win.close())
        setup_actions.append(setup_cancel)
        setup_continue = Gtk.Button(
            label="Export", css_classes=["pill", "suggested-action"],
        )
        setup_continue.set_sensitive(False)
        setup_actions.append(setup_continue)
        setup_page.append(setup_actions)
        stack.add_named(setup_page, "setup")

        # ===== Page 2: Progress ===================================
        progress_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        progress_page.append(Gtk.Label(
            label="Writing export bundle",
            xalign=0, css_classes=["title-2"],
        ))
        derive_label = Gtk.Label(
            label="Deriving key (Argon2id)…",
            xalign=0, css_classes=["heading"],
        )
        progress_page.append(derive_label)
        derive_bar = Gtk.ProgressBar()
        derive_bar.set_pulse_step(0.1)
        progress_page.append(derive_bar)

        write_label = Gtk.Label(
            label="Writing records…",
            xalign=0, css_classes=["heading"],
        )
        progress_page.append(write_label)
        write_bar = Gtk.ProgressBar()
        write_bar.set_show_text(True)
        progress_page.append(write_bar)

        progress_status = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
        )
        progress_page.append(progress_status)
        stack.add_named(progress_page, "progress")

        # ===== Page 3: Done =======================================
        done_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        done_page.append(Gtk.Label(
            label="Export complete",
            xalign=0, css_classes=["title-2"],
        ))
        done_summary = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
        )
        done_page.append(done_summary)
        done_sha = Gtk.Label(xalign=0, css_classes=["monospace"])
        done_page.append(done_sha)

        verify_status_label = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
        )
        done_page.append(verify_status_label)

        shred_warning = Gtk.Label(
            label=(
                "⚠ Shred permanently deletes the bundle from this disk. "
                "Do this only after you've copied the bundle to its "
                "target (USB, password-manager attachment, etc.). "
                "Without the bundle AND the passphrase, the vault is "
                "unrecoverable."
            ),
            xalign=0, wrap=True, css_classes=["warning"],
        )
        shred_warning.set_visible(False)
        done_page.append(shred_warning)

        done_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        done_shred = Gtk.Button(
            label="Shred bundle…",
            css_classes=["pill", "destructive-action"],
        )
        done_actions.append(done_shred)
        done_close = Gtk.Button(
            label="Close", css_classes=["pill", "suggested-action"],
        )
        done_close.connect("clicked", lambda _b: win.close())
        done_actions.append(done_close)
        done_page.append(done_actions)
        stack.add_named(done_page, "done")

        # ===== Page 4: Error ======================================
        error_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        error_page.append(Gtk.Label(
            label="Export could not complete",
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

        # --- helpers -------------------------------------------------
        def _wipe_passphrase() -> None:
            """C2 review fix: drop in-process passphrase references.

            Mirrors the import wizard's ``_wipe_passphrase`` pattern.
            Python ``str`` is immutable so the bytes can't be zeroed
            in place — best we can do is release the references so
            the string isn't pinned by the wizard's ``state`` dict
            for the rest of the window's lifetime. The two
            ``Gtk.PasswordEntry`` buffers are cleared so the
            visible field doesn't carry the text past the operation
            either.
            """
            state["passphrase"] = ""
            state.pop("passphrase", None)
            try:
                pp_entry.set_text("")
                pp_confirm.set_text("")
            except Exception:  # noqa: BLE001
                pass

        def _refresh_continue(*_args) -> None:
            pp = pp_entry.get_text()
            cf = pp_confirm.get_text()
            path = state["output_path"]
            strength = _strength_hint(pp)
            strength_label.set_label(strength)
            strength_label.remove_css_class("error")
            strength_label.remove_css_class("success")
            if len(pp) < EXPORT_PASSPHRASE_MIN_LEN:
                strength_label.add_css_class("error")
            elif len(pp) >= EXPORT_PASSPHRASE_RECOMMENDED_LEN:
                strength_label.add_css_class("success")
            ok = bool(
                path is not None
                and len(pp) >= EXPORT_PASSPHRASE_MIN_LEN
                and pp == cf
            )
            setup_continue.set_sensitive(ok)

        pp_entry.connect("changed", _refresh_continue)
        pp_confirm.connect("changed", _refresh_continue)
        _refresh_continue()

        def on_choose_dest(_btn) -> None:
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Choose export bundle destination")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            file_dialog.set_initial_name(f"vault-export-{today}.dcvault")

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
                state["output_path"] = Path(path)
                dest_entry.set_text(path)
                _refresh_continue()

            file_dialog.save(parent=win, callback=on_chosen)

        dest_choose.connect("clicked", on_choose_dest)

        # --- main action --------------------------------------------
        def on_export(_btn) -> None:
            pp = pp_entry.get_text()
            path = state["output_path"]
            if not path or len(pp) < EXPORT_PASSPHRASE_MIN_LEN or pp != pp_confirm.get_text():
                return
            state["passphrase"] = pp
            state["verify_default_on"] = verify_switch.get_active()
            go_to("progress")
            derive_bar.set_fraction(0.0)
            write_bar.set_fraction(0.0)
            write_bar.set_text("0 chunks")
            progress_status.set_label("Opening local vault…")
            log.info(
                "vault.export.started vault=%s",
                (vault_id_undashed or "?")[:12],
            )
            _run_export()

        setup_continue.connect("clicked", on_export)

        def _on_progress(prog: ExportProgress) -> None:
            # N2 review: ``update`` closes over ``prog`` — Python
            # binds the closure to the parameter cell, which is set
            # ONCE per ``_on_progress`` invocation. Each
            # write_export_bundle callback creates a fresh closure
            # over a fresh ``prog`` snapshot, so the GLib.idle_add
            # marshalling sees the right value at execution time
            # even when the worker has moved on to the next phase.
            def update() -> bool:
                if prog.phase == "derive":
                    derive_bar.pulse()
                    progress_status.set_label(
                        f"Argon2id derivation in progress… "
                        f"({prog.records_written} records prepared)"
                    )
                elif prog.phase in ("header", "manifest", "footer"):
                    derive_bar.set_fraction(1.0)
                    write_bar.pulse()
                    write_label.set_label(
                        {
                            "header": "Writing header…",
                            "manifest": "Writing manifest envelope…",
                            "footer": "Finalising bundle…",
                        }[prog.phase]
                    )
                elif prog.phase == "chunk":
                    derive_bar.set_fraction(1.0)
                    write_bar.set_fraction(min(1.0, prog.records_written / 1000.0))
                    write_bar.set_text(f"{prog.records_written} records")
                    write_label.set_label("Writing chunks…")
                return False

            GLib.idle_add(update)

        def _run_export() -> None:
            output_path = state["output_path"]
            passphrase = state["passphrase"]

            def worker() -> None:
                err: Exception | None = None
                result = None
                contents = None
                manifest_envelope: bytes = b""
                manifest_plaintext: dict = {}
                try:
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(
                        config_dir, config, vault_id_undashed,
                    )
                    try:
                        manifest_plaintext = vault.fetch_unified_manifest(relay)
                        # Use the relay's root envelope verbatim as the
                        # bundle's manifest_envelope; import-side
                        # ``decrypt_bundle_manifest_envelope`` handles
                        # the root-shape envelope and falls back to
                        # legacy when present.
                        root_payload = relay.get_root(
                            vault.vault_id, vault.vault_access_secret,
                        )
                        root_envelope = root_payload["root_ciphertext"]
                        if isinstance(root_envelope, (bytearray, memoryview)):
                            root_envelope = bytes(root_envelope)
                        manifest_envelope = root_envelope

                        result = write_export_bundle(
                            vault=vault,
                            relay=relay,
                            manifest_envelope=manifest_envelope,
                            manifest_plaintext=manifest_plaintext,
                            output_path=output_path,
                            passphrase=passphrase,
                            progress=_on_progress,
                        )
                    finally:
                        vault.close()

                    if state["verify_default_on"]:
                        contents = read_export_bundle(
                            bundle_path=result.bundle_path,
                            passphrase=passphrase,
                            vault_id=vault_id_undashed,
                        )
                except Exception as exc:  # noqa: BLE001
                    err = exc

                def settle() -> bool:
                    if err is not None:
                        msg = _humanize_export_error(err)
                        error_status.set_label(msg)
                        # Atomic-rename pattern: a failed run leaves
                        # the destination file un-renamed; reassure
                        # the user no partial bundle was created.
                        log.warning(
                            "vault.export.failed vault=%s error=%s",
                            (vault_id_undashed or "?")[:12],
                            type(err).__name__,
                        )
                        go_to("error")
                        return False
                    assert result is not None
                    state["result_path"] = result.bundle_path
                    state["result_sha256"] = _sha256_of_file(result.bundle_path)
                    if state["verify_default_on"]:
                        if contents is not None:
                            state["verify_ok"] = True
                            state["verify_message"] = (
                                f"Verify passed: {contents.record_count} records, "
                                f"{len(contents.chunks)} chunks. Hash chain "
                                f"matched."
                            )
                        else:
                            state["verify_ok"] = False
                            state["verify_message"] = (
                                "Verify did not run (unexpected) — "
                                "treat the bundle as suspect until "
                                "verified manually."
                            )
                    log.info(
                        "vault.export.completed vault=%s bytes=%d",
                        (vault_id_undashed or "?")[:12],
                        result.bytes_written,
                    )
                    if state["verify_default_on"]:
                        log.info(
                            "vault.export.verified vault=%s ok=%s",
                            (vault_id_undashed or "?")[:12],
                            state["verify_ok"],
                        )
                    _render_done()
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        def _render_done() -> None:
            path = state["result_path"]
            sha = state["result_sha256"] or "?"
            done_summary.set_label(
                f"Bundle written to:\n{path}\n\n"
                "Keep both this file AND your export passphrase safe. "
                "Either alone is useless — both together restore the "
                "vault on a fresh device via the Import wizard."
            )
            done_sha.set_label(f"SHA-256: {sha}")
            if state["verify_default_on"]:
                verify_status_label.set_label(state["verify_message"])
                verify_status_label.remove_css_class("error")
                verify_status_label.remove_css_class("success")
                verify_status_label.add_css_class(
                    "success" if state["verify_ok"] else "error",
                )
            else:
                verify_status_label.set_label(
                    "Verify-after-write was turned off. Run the Import "
                    "wizard on a fresh device to confirm round-trip."
                )
            shred_warning.set_visible(True)
            go_to("done")
            # C2: drop the passphrase references once the export
            # bundle has been written + verified — they're not
            # needed past this point and pinning them in
            # ``state["passphrase"]`` keeps the string alive until
            # window close.
            _wipe_passphrase()

        def on_shred_clicked(_btn) -> None:
            path = state["result_path"]
            if path is None:
                return
            dlg = Adw.AlertDialog(
                heading="Shred bundle from this disk?",
                body=(
                    f"This permanently deletes {path} from this disk. "
                    "Have you confirmed the bundle is at its target "
                    "(USB drive, password manager, encrypted backup, "
                    "etc.)? Without it AND your passphrase, the vault "
                    "is unrecoverable."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("shred", "Shred bundle")
            dlg.set_response_appearance(
                "shred", Adw.ResponseAppearance.DESTRUCTIVE,
            )
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def on_resp(_d, response: str) -> None:
                if response != "shred":
                    return
                shredded = shred_file(path)
                if shredded:
                    log.info(
                        "vault.export.shredded vault=%s",
                        (vault_id_undashed or "?")[:12],
                    )
                    done_summary.set_label(
                        f"Bundle was at {path} — shredded.\n\n"
                        "Keep the passphrase + the copy at your "
                        "target location safe."
                    )
                    done_shred.set_sensitive(False)
                else:
                    verify_status_label.set_label(
                        f"Shred reported nothing to delete at {path}. "
                        "Manual cleanup may be needed.",
                    )
                    verify_status_label.remove_css_class("dim-label")
                    verify_status_label.add_css_class("error")

            dlg.connect("response", on_resp)
            dlg.present(win)

        done_shred.connect("clicked", on_shred_clicked)

        # C2: drop the passphrase on window close so a stuck-in-state
        # passphrase doesn't sit pinned in memory after the user
        # cancels.
        def on_close(_w) -> bool:
            _wipe_passphrase()
            return False

        win.connect("close-request", on_close)

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ----- helpers -------------------------------------------------------


def _strength_hint(passphrase: str) -> str:
    """Length-based strength hint shown beneath the passphrase fields.

    Intentionally simple: the data layer enforces an 8-char floor; the
    UI nudges towards 16+ for genuine offline-attack resistance. No
    external strength library — bypassing zxcvbn keeps the wizard's
    deps slim and avoids per-character allocation spikes that would
    chunk the GTK main loop.
    """
    n = len(passphrase or "")
    if n == 0:
        return "Pick a passphrase you don't use elsewhere."
    if n < 8:
        return f"{n} characters — too short (minimum 8)."
    if n < 16:
        return f"{n} characters — works, but 16+ is recommended for offline-attack resistance."
    return f"{n} characters — good length."


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _humanize_export_error(exc: Exception) -> str:
    from .vault.error_messages import humanize
    from .vault.export.bundle import ExportError

    if isinstance(exc, ExportError):
        if exc.code == "vault_export_passphrase_too_short":
            return (
                "Passphrase too short — minimum 8 characters. (The wizard "
                "should have caught this before submission; please file a "
                "bug if you reached this branch.)"
            )
        return f"Bundle write failed ({exc.code}): {exc.args[0] if exc.args else ''}"
    return f"Export aborted: {humanize(exc)}"


__all__ = ["show_vault_export"]
