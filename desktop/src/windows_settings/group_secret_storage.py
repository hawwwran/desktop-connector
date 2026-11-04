"""Security group: secret-storage status + Verify button (H.6 / H.7).

Surfaces the active backend's status and gives the user a way to
re-scrub plaintext after manual config.json edits or a partial-failure
boot. Settings open already triggers Config init's automatic migration,
so on first display the row almost always reads "Already clean".

Verify is a real round-trip per the project rule "No fake tests / verify
buttons" — it calls ``config.scrub_secrets()`` and
``crypto.scrub_private_key()`` and re-reads the result. Do not weaken
this path.

Pre-split this lived inline in ``on_activate`` as the
``sec_group = Adw.PreferencesGroup(title="Security")`` block plus the
nested ``_on_secret_info`` and ``_on_verify_secret_storage`` closures.
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from .context import SettingsContext


def build(ctx: SettingsContext) -> Adw.PreferencesGroup:
    config = ctx.config
    crypto = ctx.crypto
    win = ctx.win

    # Security: verify secret storage (H.6). Surfaces the active
    # backend's status and gives the user a way to re-scrub
    # plaintext after manual config.json edits or a partial-failure
    # boot. Settings open already triggers Config init's automatic
    # migration, so on first display the row almost always reads
    # "Already clean".
    sec_group = Adw.PreferencesGroup(title="Security")
    ctx.content.append(sec_group)

    _secure_now = config.is_secret_storage_secure()
    verify_row = Adw.ActionRow(
        title="Secret storage",
        subtitle=(
            "Identity, auth token + pairing keys in OS keyring "
            "(libsecret / KWallet)"
            if _secure_now else
            "Identity, auth token + pairing keys in plaintext "
            "~/.config/desktop-connector/ (no keyring backend "
            "reachable)"
        ),
    )
    sec_group.add(verify_row)
    ctx.verify_row = verify_row

    # Info icon: tooltip on hover, click opens an explainer dialog.
    info_btn = Gtk.Button(valign=Gtk.Align.CENTER)
    info_btn.set_child(Gtk.Image.new_from_icon_name(
        "dialog-information-symbolic",
    ))
    info_btn.add_css_class("flat")
    info_btn.add_css_class("circular")
    info_btn.set_tooltip_text(
        "Verify scans config.json for plaintext secrets and "
        "migrates them into the OS keyring. Click for details."
    )

    def _on_secret_info(_btn):
        dialog = Adw.MessageDialog(
            transient_for=win,
            heading="About verify secret storage",
            body=(
                "Verify scans for plaintext secret material and "
                "moves anything it finds into the OS keyring. "
                "Three locations are checked:\n\n"
                "  • config.json → auth_token field\n"
                "  • config.json → paired_devices[*].symmetric_key_b64\n"
                "  • keys/private_key.pem → long-term device "
                "identity key\n\n"
                "Useful when:\n"
                "  • You've manually edited config.json or "
                "restored a private_key.pem from backup\n"
                "  • A previous launch's automatic migration "
                "failed (e.g. the keyring was locked at startup)\n"
                "  • You want to confirm secrets are stored "
                "correctly\n\n"
                "Verify does NOT change cryptographic identity, "
                "doesn't re-pair, and doesn't rotate keys. It "
                "only ensures plaintext doesn't accumulate "
                "outside the keyring."
            ),
        )
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.present()

    info_btn.connect("clicked", _on_secret_info)
    verify_row.add_suffix(info_btn)

    verify_btn = Gtk.Button(label="Verify", valign=Gtk.Align.CENTER)
    verify_btn.add_css_class("pill")

    def _on_verify_secret_storage(_btn):
        result = config.scrub_secrets()
        # H.7 also covers the private-key PEM. crypto already
        # ran a migration check in __init__ (window-spawn time);
        # re-running here picks up anything that arrived since.
        pem_scrubbed = (
            crypto.scrub_private_key() or crypto.was_pem_migrated
        )

        if not result.secure:
            verify_row.set_subtitle(
                "Plaintext fallback active — no scrub possible. "
                "Install gnome-keyring or kwallet and re-launch."
            )
            return
        if result.failed > 0:
            verify_row.set_subtitle(
                f"Scrubbed {result.scrubbed}, {result.failed} "
                "field(s) remain (keyring transient — re-try)"
            )
            return

        items: list[str] = []
        if result.scrubbed > 0:
            items.append(f"{result.scrubbed} field(s)")
        if pem_scrubbed:
            items.append("device private key")
        if items:
            verify_row.set_subtitle(
                "✓ Scrubbed " + " + ".join(items) +
                " into the keyring"
            )
        else:
            verify_row.set_subtitle(
                "✓ Already clean — identity, auth token + "
                "pairing keys all in keyring"
            )

    verify_btn.connect("clicked", _on_verify_secret_storage)
    verify_row.add_suffix(verify_btn)

    return sec_group
