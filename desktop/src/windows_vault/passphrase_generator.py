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
from gi.repository import Gtk, Adw

from ..brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..windows_common import _make_app


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
