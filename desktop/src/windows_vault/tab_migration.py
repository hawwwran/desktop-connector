"""Migration tab — launch wizard + switch-back surface.

The full multi-page wizard lives in :mod:`windows_vault_migration`;
this tab is the entry point + the post-commit switch-back UI."""

import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ._kv_row import _kv_row
from ._main_context import MainContext


def build_migration_tab(ctx: MainContext, win) -> "Gtk.Box":
    config = ctx.config
    config_dir = ctx.config_dir

    from ..vault.migration.propagation import can_switch_back

    migration_tab = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=12,
        margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
    )
    migration_tab.append(Gtk.Label(
        label="Relay migration",
        xalign=0, css_classes=["title-3"],
    ))
    migration_tab.append(Gtk.Label(
        label=(
            "Move this vault to a different relay. The migration wizard "
            "walks through preflight, copy, verify, and commit; within "
            "7 days of a commit you can switch back to the previous "
            "relay from this tab."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))

    current_relay_label = Gtk.Label(xalign=0)
    current_relay_label.add_css_class("monospace")
    migration_tab.append(_kv_row("Current relay", current_relay_label))

    previous_relay_label = Gtk.Label(xalign=0)
    previous_relay_label.add_css_class("monospace")
    migration_tab.append(_kv_row("Previous relay", previous_relay_label))

    previous_expires_label = Gtk.Label(xalign=0, css_classes=["dim-label"])
    migration_tab.append(_kv_row("Switch-back available until", previous_expires_label))

    switch_back_btn = Gtk.Button(
        label="Switch back to previous relay",
        css_classes=["pill"],
    )
    switch_back_btn.set_halign(Gtk.Align.START)
    migrate_btn = Gtk.Button(
        label="Migrate to another relay…",
        css_classes=["pill", "suggested-action"],
    )
    migrate_btn.set_halign(Gtk.Align.START)
    migrate_btn.set_tooltip_text(
        "Open the migration wizard: target URL → preflight → copy → "
        "verify → commit."
    )

    def on_migrate_clicked(_b) -> None:
        # The wizard is its own subprocess so the long-running engine
        # work doesn't block the Settings GTK loop. Same shape as
        # ``vault-onboard`` / ``vault-import``.
        subprocess.Popen(
            [
                sys.executable, "-m", "src.windows", "vault-migration",
                f"--config-dir={config_dir}",
            ],
            close_fds=True,
        )

    migrate_btn.connect("clicked", on_migrate_clicked)
    migration_tab.append(switch_back_btn)
    migration_tab.append(migrate_btn)

    def refresh_migration_tab() -> None:
        config.reload()
        current = str(getattr(config, "server_url", "") or "(not set)")
        current_relay_label.set_label(current)
        prev_url = config.vault_previous_relay_url
        prev_exp = config.vault_previous_relay_expires_at
        available = can_switch_back(
            previous_relay_url=prev_url,
            previous_relay_expires_at=prev_exp,
        )
        previous_relay_label.set_label(prev_url or "(none)")
        previous_expires_label.set_label(prev_exp or "—")
        switch_back_btn.set_sensitive(available)
        switch_back_btn.set_tooltip_text(
            "Roll the active relay back to the source. The 7-day grace "
            "window starts from the moment this device learned of the "
            "migration."
            if available else
            "No previous relay on file, or the 7-day grace window has "
            "elapsed."
        )

    def on_switch_back(_btn) -> None:
        prev_url = config.vault_previous_relay_url
        if not prev_url:
            return
        dlg = Adw.AlertDialog(
            heading="Switch back to previous relay?",
            body=(
                f"This device will start using {prev_url} again. "
                "The migration on the source relay is not undone — "
                "the source is still read-only on the relay side."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("switch", "Switch back")
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("switch", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_resp(_dialog, response: str) -> None:
            if response != "switch":
                return
            config.reload()
            config.server_url = prev_url
            config.vault_previous_relay_url = None
            config.vault_previous_relay_expires_at = None
            refresh_migration_tab()

        dlg.connect("response", on_resp)
        dlg.present(win)

    switch_back_btn.connect("clicked", on_switch_back)
    refresh_migration_tab()
    return migration_tab
