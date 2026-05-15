"""F-LT11 — inline "Unlock with recovery passphrase" mini-prompt.

Surfaced by the destructive-action gate sites (tab_danger.py and
windows_vault_import.py) when ``fresh_unlock.is_fresh_unlock_active()``
is false. Re-runs Argon2id against the on-disk recovery kit + the
typed passphrase via the existing ``verify_recovery_kit`` path; on
success stamps the per-process fresh-unlock window and invokes the
caller's continuation.

The prompt mirrors the ``Adw.Dialog`` shape used by the recovery
test in ``tab_recovery.py`` so it can stay open across a failed
verify + retry without dismissing the user's inputs.

In-memory state only — the prompt does not touch the cached
device grant, the keyring, or the manifest.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ..vault import fresh_unlock
from ..vault.recovery_kit import (
    recovery_envelope_meta_from_json,
    verify_recovery_kit,
)


log = logging.getLogger(__name__)


def require_fresh_unlock_or_prompt(
    parent_window,
    *,
    config,
    operation_label: str,
    on_success: Callable[[], None],
    on_cancel: Callable[[], None] | None = None,
) -> None:
    """Run ``on_success`` if the fresh-unlock window is active; else
    open the mini-prompt and run ``on_success`` once the user
    completes verification.

    On user cancel (or failure they choose not to retry) the
    optional ``on_cancel`` callback runs. ``on_success`` is invoked
    on the GTK main thread.
    """
    if fresh_unlock.is_fresh_unlock_active():
        on_success()
        return
    _present_prompt(
        parent_window,
        config=config,
        operation_label=operation_label,
        on_success=on_success,
        on_cancel=on_cancel,
    )


def _present_prompt(
    parent_window,
    *,
    config,
    operation_label: str,
    on_success: Callable[[], None],
    on_cancel: Callable[[], None] | None,
) -> None:
    try:
        envelope_meta = recovery_envelope_meta_from_json(
            (config._data.get("vault") or {}).get("recovery_envelope_meta")
        )
    except Exception:
        envelope_meta = None
    if envelope_meta is None:
        log.warning(
            "vault.fresh_unlock.prompt.envelope_meta_missing operation=%s",
            operation_label,
        )

    dialog = Adw.Dialog()
    dialog.set_title("Unlock with recovery passphrase")
    dialog.set_content_width(520)
    dialog.set_content_height(320)

    body = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
    )
    dialog.set_child(body)

    body.append(Gtk.Label(
        label=(
            "Sensitive vault operations require fresh proof of the "
            "recovery passphrase regardless of the unlock-timeout "
            f"setting. Choose your recovery kit and re-type the "
            f"passphrase to continue with {operation_label!s}."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))

    # Kit picker row.
    kit_state: dict[str, str | None] = {"path": None}
    kit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    kit_entry = Gtk.Entry(
        placeholder_text="Recovery kit file",
        editable=False, hexpand=True,
    )
    kit_entry.update_property(
        [Gtk.AccessibleProperty.LABEL], ["Recovery kit file path"],
    )
    kit_row.append(kit_entry)
    browse_btn = Gtk.Button(label="Choose…", css_classes=["pill"])
    kit_row.append(browse_btn)
    body.append(kit_row)

    # Passphrase entry.
    body.append(Gtk.Label(label="Recovery passphrase", xalign=0))
    passphrase_entry = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
    passphrase_entry.update_property(
        [Gtk.AccessibleProperty.LABEL], ["Recovery passphrase"],
    )
    body.append(passphrase_entry)

    status_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    body.append(status_label)
    if envelope_meta is None:
        status_label.set_label(
            "Recovery envelope metadata missing from config — finish "
            "onboarding before retrying."
        )
        status_label.remove_css_class("dim-label")
        status_label.add_css_class("error")

    button_row = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        halign=Gtk.Align.END,
    )
    body.append(button_row)
    cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
    unlock_btn = Gtk.Button(
        label="Unlock",
        css_classes=["pill", "suggested-action"],
    )
    unlock_btn.set_sensitive(False)
    button_row.append(cancel_btn)
    button_row.append(unlock_btn)

    def set_status(message: str, css_class: str = "dim-label") -> None:
        for klass in ("dim-label", "error", "success"):
            status_label.remove_css_class(klass)
        status_label.add_css_class(css_class)
        status_label.set_label(message)

    def update_unlock_sensitive() -> None:
        ok = (
            bool(kit_state["path"])
            and bool(passphrase_entry.get_text())
            and envelope_meta is not None
        )
        unlock_btn.set_sensitive(ok)

    def on_browse(_btn) -> None:
        picker = Gtk.FileDialog()
        picker.set_title("Choose recovery kit file")

        def on_file_chosen(file_dialog, result) -> None:
            try:
                gfile = file_dialog.open_finish(result)
            except Exception:
                return
            if gfile is None:
                return
            path = gfile.get_path()
            kit_state["path"] = path
            kit_entry.set_text(Path(path).name)
            kit_entry.set_tooltip_text(path)
            update_unlock_sensitive()

        picker.open(parent_window, None, on_file_chosen)

    browse_btn.connect("clicked", on_browse)
    passphrase_entry.connect("changed", lambda _e: update_unlock_sensitive())

    # Track verification outcome so close-by-Escape / close-by-X /
    # close-by-Cancel-button all funnel through the same on_close
    # handler and correctly invoke on_cancel when the user didn't
    # verify. Verified=True only after a successful Argon2id run.
    verified_state: dict[str, bool] = {"verified": False}

    def on_cancel_clicked(_btn) -> None:
        dialog.close()

    def on_unlock_clicked(_btn) -> None:
        kit_path = kit_state["path"]
        if not kit_path or envelope_meta is None:
            return
        passphrase = passphrase_entry.get_text()
        if not passphrase:
            return
        unlock_btn.set_sensitive(False)
        cancel_btn.set_sensitive(False)
        set_status("Verifying…", "dim-label")

        def worker() -> None:
            try:
                ok, msg = verify_recovery_kit(
                    kit_path,
                    passphrase=passphrase,
                    envelope_meta=envelope_meta,
                )
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"Verify failed: {type(exc).__name__}"

            def settle() -> bool:
                cancel_btn.set_sensitive(True)
                if ok:
                    fresh_unlock.stamp_fresh_unlock()
                    log.info(
                        "vault.fresh_unlock.verified operation=%s",
                        operation_label,
                    )
                    verified_state["verified"] = True
                    dialog.close()
                    on_success()
                else:
                    log.info(
                        "vault.fresh_unlock.verify_failed operation=%s reason=%s",
                        operation_label, msg,
                    )
                    set_status(msg or "Verification failed.", "error")
                    update_unlock_sensitive()
                return False

            GLib.idle_add(settle)

        threading.Thread(target=worker, daemon=True).start()

    cancel_btn.connect("clicked", on_cancel_clicked)
    unlock_btn.connect("clicked", on_unlock_clicked)

    def on_close(_dialog) -> None:
        if not verified_state["verified"] and on_cancel is not None:
            on_cancel()

    dialog.connect("closed", on_close)
    dialog.present(parent_window)
