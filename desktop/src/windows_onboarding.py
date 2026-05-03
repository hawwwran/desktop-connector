"""Onboarding + secret-storage warning windows.

`python -m src.windows onboarding` opens the first-launch relay-URL
prompt. `python -m src.windows secret-storage-warning` explains the
plaintext-fallback state and how to fix it.
"""

from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .brand import apply_brand_css, apply_theme_mode_from_config_dir
from .windows_common import _make_app


def show_onboarding(config_dir: Path):
    """First-launch onboarding dialog (P.4a).

    Asks for the relay server URL (with /api/health probe button) and an
    autostart toggle. Persists answers via the same Config object the
    parent uses; the parent detects Save vs Cancel by re-reading
    config.server_url.

    Runs as a subprocess (spawned by appimage_onboarding) for the same
    reason all other GTK4 windows do — pystray's appindicator backend
    loads GTK3 in the parent process at dep-check time, locking GTK to
    3.0 there.
    """
    from .config import Config
    from .bootstrap.appimage_onboarding import (
        commit_onboarding_settings,
        probe_server,
    )

    config = Config(config_dir)
    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Welcome to Desktop Connector",
            default_width=480,
            default_height=420,
        )

        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        title = Gtk.Label(label="Welcome to Desktop Connector", xalign=0)
        title.add_css_class("title-2")
        outer.append(title)

        subtitle = Gtk.Label(
            label="Connect to your relay server to pair with your devices.",
            xalign=0, wrap=True,
        )
        subtitle.add_css_class("dim-label")
        outer.append(subtitle)

        url_label = Gtk.Label(label="Relay server URL", xalign=0)
        url_label.add_css_class("heading")
        outer.append(url_label)

        url_entry = Gtk.Entry(
            placeholder_text="https://example.com/SERVICES/desktop-connector",
        )
        outer.append(url_entry)

        probe_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(probe_row)
        probe_btn = Gtk.Button(label="Test connection")
        probe_row.append(probe_btn)
        probe_status = Gtk.Label(xalign=0, hexpand=True)
        probe_status.add_css_class("dim-label")
        probe_row.append(probe_status)

        def run_probe(url):
            if probe_server(url):
                probe_status.set_text("✓ Server reachable")
                probe_status.add_css_class("success")
                return True
            probe_status.remove_css_class("success")
            probe_status.set_text("✗ Could not reach server")
            return False

        def on_probe(_btn):
            url = url_entry.get_text().strip().rstrip("/")
            if not url:
                probe_status.set_text("Enter a URL first.")
                return
            probe_status.set_text("Checking…")
            GLib.idle_add(lambda: (run_probe(url), False)[1])

        probe_btn.connect("clicked", on_probe)

        autostart_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.append(autostart_row)
        autostart_label = Gtk.Label(
            label="Start automatically on login",
            xalign=0, hexpand=True,
        )
        autostart_row.append(autostart_label)
        autostart_switch = Gtk.Switch()
        autostart_switch.set_active(True)
        autostart_switch.set_valign(Gtk.Align.CENTER)
        autostart_row.append(autostart_switch)

        outer.append(Gtk.Box(vexpand=True))
        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.END)
        outer.append(button_row)

        cancel_btn = Gtk.Button(label="Cancel")
        button_row.append(cancel_btn)
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        button_row.append(save_btn)

        def commit(url):
            # Delegate to the free function in appimage_onboarding so
            # the persistence logic is unit-testable without GTK4.
            commit_onboarding_settings(
                config_dir,
                server_url=url,
                autostart_enabled=autostart_switch.get_active(),
            )

        cancel_btn.connect("clicked", lambda _b: win.close())

        def on_save(_btn):
            url = url_entry.get_text().strip().rstrip("/")
            if not url:
                probe_status.set_text("Enter a URL first.")
                return
            if probe_server(url):
                commit(url)
                win.close()
                return
            # Server unreachable — confirm "Save anyway?" (mirrors install.sh).
            dlg = Adw.AlertDialog(
                heading="Server did not respond",
                body=(
                    f"{url}/api/health did not return a healthy response. "
                    "Save anyway? You can update the URL in Settings later."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("save", "Save anyway")
            dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def on_resp(_d, response):
                if response == "save":
                    commit(url)
                    win.close()

            dlg.connect("response", on_resp)
            dlg.present(win)

        save_btn.connect("clicked", on_save)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def show_secret_storage_warning(config_dir: Path):
    """Explainer for H.5's plaintext-fallback warning.

    Opened when the user clicks the tray's "⚠ Secrets in plaintext"
    row. Three short sections — what's happening, why, how to fix —
    plus a Close button. No buttons that act on state; this window
    is informational. Fixing means installing a Secret Service
    backend (gnome-keyring on Zorin/Ubuntu/Mint, kwallet on KDE)
    and re-launching Desktop Connector.
    """
    app = _make_app()

    def on_activate(app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)
        win = Adw.ApplicationWindow(
            application=app,
            title="Secret storage warning",
            default_width=560,
            default_height=520,
        )

        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        title = Gtk.Label(label="Secrets are stored in plaintext", xalign=0)
        title.add_css_class("title-2")
        outer.append(title)

        subtitle = Gtk.Label(
            label=(
                "Desktop Connector couldn't reach a Secret Service "
                "backend (GNOME Keyring, KWallet, etc.). It's still "
                "working, but your long-term identity key, "
                "authentication token, and per-pairing encryption keys "
                "are sitting in plain text in your config directory."
            ),
            xalign=0, wrap=True, hexpand=True,
        )
        subtitle.add_css_class("dim-label")
        outer.append(subtitle)

        def _section(title_text: str, body_text: str) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            heading = Gtk.Label(label=title_text, xalign=0)
            heading.add_css_class("heading")
            box.append(heading)
            body = Gtk.Label(label=body_text, xalign=0, wrap=True, hexpand=True)
            body.set_selectable(True)
            box.append(body)
            return box

        outer.append(_section(
            "What's happening",
            f"Secrets are written to:\n  {config_dir / 'config.json'}\n"
            f"  {config_dir / 'keys' / 'private_key.pem'}\n"
            "with restrictive permissions (0o600), but anyone who can "
            "read your home directory (other accounts on this machine, "
            "anyone with a backup of ~/.config) sees the values in "
            "plain text. The private key is the most sensitive of the "
            "three — losing it leaks your long-term device identity "
            "and every pairing's encryption key.",
        ))

        outer.append(_section(
            "Why this is happening",
            "Either no Secret Service backend is installed / running, "
            "or it's locked and Desktop Connector couldn't unlock it. "
            "Desktop sessions normally start gnome-keyring (or kwallet "
            "on KDE) automatically, but headless / minimal installs "
            "may not.",
        ))

        outer.append(_section(
            "How to fix it",
            "On GNOME / Zorin / Ubuntu / Mint:\n"
            "  sudo apt install gnome-keyring libsecret-tools\n\n"
            "On KDE Plasma:\n"
            "  sudo apt install kwalletmanager kwallet-pam\n\n"
            "Then log out, log back in (so the keyring daemon "
            "registers with your session), and re-launch Desktop "
            "Connector. The next start will migrate your secrets out "
            "of config.json into the keyring automatically.",
        ))

        button_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
        )
        outer.append(button_row)

        close_btn = Gtk.Button(label="Close")
        close_btn.add_css_class("pill")
        close_btn.connect("clicked", lambda _: win.close())
        button_row.append(close_btn)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)
