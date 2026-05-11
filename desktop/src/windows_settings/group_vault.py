"""Vault group: active toggle + "Open Vault settings…" launcher.

T0 §D16: a small "Vault" group with the active toggle and an
"Open Vault settings…" button. Toggle is ON by default on
fresh install; OFF hides the tray submenu and pauses sync
without destroying any data.

Pre-split this lived inline in ``on_activate`` as the
``vault_group = Adw.PreferencesGroup(title="Vault")`` block plus the
``vault_exists_locally``, ``refresh_vault_button``, ``on_vault_toggled``
and ``on_open_vault_clicked`` closures. The ``refresh_vault_button``
late-bound callable is published onto ``ctx`` so other group builders
(currently none) could reach it; ``on_vault_toggled`` calls it
directly, same as the original.
"""

from __future__ import annotations

from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from ..vault.ui.ui_state import vault_settings_button_state
from .context import SettingsContext


def build(ctx: SettingsContext) -> Adw.PreferencesGroup:
    config = ctx.config

    vault_group = Adw.PreferencesGroup(title="Vault")
    ctx.content.append(vault_group)

    vault_active_row = Adw.SwitchRow(
        title="Vault active",
        subtitle="Show Vault in tray menu and run sync. OFF is reversible — keys, manifests, and downloaded data are preserved.",
        active=config.vault_active,
    )
    vault_group.add(vault_active_row)
    ctx.vault_active_row = vault_active_row

    open_vault_row = Adw.ActionRow(
        title="Open Vault settings…",
        subtitle="Opens the deep-config window. Disabled when Vault is inactive.",
    )
    vault_group.add(open_vault_row)
    open_vault_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
    open_vault_btn.add_css_class("pill")
    open_vault_row.add_suffix(open_vault_btn)
    ctx.open_vault_btn = open_vault_btn

    def vault_exists_locally() -> bool:
        raw = config._data.get("vault")
        return isinstance(raw, dict) and bool(raw.get("last_known_id"))

    def refresh_vault_button():
        state = vault_settings_button_state(
            toggle_active=config.vault_active,
            vault_exists=vault_exists_locally(),
        )
        open_vault_btn.set_sensitive(state.enabled)

    def on_vault_toggled(switch, _pspec):
        new_value = switch.get_active()
        if new_value != config.vault_active:
            config.vault_active = new_value
        refresh_vault_button()

    def on_open_vault_clicked(_btn):
        state = vault_settings_button_state(
            toggle_active=config.vault_active,
            vault_exists=vault_exists_locally(),
        )
        target = None
        if state.action == "launch_wizard":
            target = "vault-onboard"
        elif state.action == "launch_settings":
            target = "vault-main"
        if target is None:
            return
        import os as _os
        import subprocess as _subprocess
        import sys as _sys
        appimage = _os.environ.get("APPIMAGE")
        cmd = (
            [appimage, f"--gtk-window={target}",
             f"--config-dir={config.config_dir}"]
            if appimage else
            [_sys.executable, "-m", "src.windows", target,
             f"--config-dir={config.config_dir}"]
        )
        cwd = (None if appimage
               else str(Path(__file__).resolve().parent.parent.parent))
        _subprocess.Popen(cmd, cwd=cwd)

    # Publish the refresher onto the context BEFORE wiring signals so
    # any signal that could fire it sees the bound callable.
    ctx.refresh_vault_button = refresh_vault_button

    vault_active_row.connect("notify::active", on_vault_toggled)
    open_vault_btn.connect("clicked", on_open_vault_clicked)
    refresh_vault_button()

    return vault_group
