"""Standalone passphrase-generator window.

Opened from the wizard's Generate button. Shows a random diceware-style
passphrase, lets the user Regenerate or Copy. The user pastes the
result back into the wizard's passphrase fields manually.

Extracted from ``windows_vault.py`` (lines ~2383–2467).
"""

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


# Review §6.H4: clipboard auto-clear window. Most desktop clipboard
# managers (CopyQ, Klipper, GPaste) snapshot the X11 / Wayland
# selection at copy time — even if we overwrite the clipboard later
# the manager keeps the prior contents in its history. Surface this
# in the UI; 30 s is enough for a paste-into-wizard while keeping
# the plaintext-in-clipboard window short.
CLIPBOARD_AUTO_CLEAR_S = 30


def show_vault_passphrase_generator(config_dir: Path):
    """Standalone passphrase-generator window opened from the wizard's
    Generate button. Shows a random diceware-style passphrase, lets
    the user Regenerate or Copy. The user pastes the result back into
    the wizard's passphrase fields manually.
    """
    from ..vault.passphrase import generate_passphrase, estimated_entropy_bits

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
                "bits of entropy. Reveal with the eye icon, copy into the "
                "wizard's passphrase fields, or click Regenerate if you don't "
                "like it."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        # Review §6.H4: PasswordEntry obscures the passphrase by
        # default (peek icon reveals on click). Pre-fix a plain
        # ``Gtk.Entry`` showed the passphrase to anyone shoulder-
        # surfing or to a screen-reader running with announce-text.
        pp_entry = Gtk.PasswordEntry()
        pp_entry.set_show_peek_icon(True)
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

        # Clipboard auto-clear state. We keep the timeout source id
        # so a fresh Copy click resets the timer instead of stacking.
        copy_state = {"timeout_id": None, "remaining": 0}
        copy_status = Gtk.Label(xalign=0, css_classes=["dim-label"])

        def _clear_clipboard_if_match(snapshot: str) -> None:
            display = win.get_display()
            if display is None:
                return
            clip = display.get_clipboard()
            # Best-effort: only overwrite if the current content is
            # still our passphrase — don't clobber something the
            # user copied since.
            try:
                clip.read_text_async(None, _read_then_maybe_clear, snapshot)
            except Exception:  # noqa: BLE001
                pass

        def _read_then_maybe_clear(clip, result, snapshot) -> None:
            try:
                current = clip.read_text_finish(result)
            except Exception:  # noqa: BLE001
                current = None
            if current == snapshot:
                clip.set("")  # type: ignore[arg-type]

        def _tick_countdown() -> bool:
            copy_state["remaining"] -= 1
            if copy_state["remaining"] <= 0:
                copy_status.set_label(
                    "Clipboard cleared. Generate or Copy again to re-fill it."
                )
                copy_state["timeout_id"] = None
                return False  # stop the GLib timer
            copy_status.set_label(
                f"Copied — auto-clearing in {copy_state['remaining']} s."
            )
            return True

        def _cancel_pending_clear() -> None:
            tid = copy_state.get("timeout_id")
            if tid is not None:
                try:
                    GLib.source_remove(tid)
                except Exception:  # noqa: BLE001
                    pass
                copy_state["timeout_id"] = None

        def on_copy(_b):
            display = win.get_display()
            if display is None:
                return
            text = pp_entry.get_text()
            display.get_clipboard().set(text)
            _cancel_pending_clear()
            copy_state["remaining"] = CLIPBOARD_AUTO_CLEAR_S
            copy_status.set_label(
                f"Copied — auto-clearing in {CLIPBOARD_AUTO_CLEAR_S} s."
            )
            # 1-second countdown ticker, then a final clear on the
            # last tick.
            copy_state["timeout_id"] = GLib.timeout_add(
                1000, _tick_countdown,
            )
            # Schedule the actual overwrite at T+CLIPBOARD_AUTO_CLEAR_S
            # so the value disappears even if the user closes the
            # window before the visible countdown finishes.
            GLib.timeout_add(
                CLIPBOARD_AUTO_CLEAR_S * 1000,
                lambda snap=text: (
                    _clear_clipboard_if_match(snap) or False
                ),
            )
        copy_btn = Gtk.Button(label="Copy", css_classes=["pill", "suggested-action"])
        copy_btn.connect("clicked", on_copy)
        btn_row.append(copy_btn)

        spacer = Gtk.Box(hexpand=True)
        btn_row.append(spacer)

        close_btn = Gtk.Button(label="Close", css_classes=["pill"])
        close_btn.connect("clicked", lambda _b: win.close())
        btn_row.append(close_btn)

        outer.append(copy_status)
        outer.append(Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
            label=(
                "Tip: write the passphrase down somewhere safe BEFORE you paste "
                "it. If you lose it, the recovery kit file alone won't get you "
                "back into the vault. Note: a clipboard manager (CopyQ, "
                "Klipper, GPaste) may keep a history copy even after the "
                "auto-clear above runs — clear that history manually if you "
                "use one."
            ),
        ))

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
