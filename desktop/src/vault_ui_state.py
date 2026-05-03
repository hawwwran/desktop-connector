"""Pure state-decision functions for the Vault UI surface.

T3.3 / T3.5 / T3.6 all need to map a small set of inputs (toggle on/off,
vault present/absent, etc.) to a UI state (button enabled, submenu
contents, wizard step). Centralizing those decisions here lets the
GTK-heavy modules ship as thin renderers — every decision rule has an
automated test.

Sources of truth: T0 §D16 (toggle + button + tray submenu) and §A2
(wizard cancel rule). When this module disagrees with T0, T0 wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Action codes for the "Open Vault settings…" button in main Settings.
# The GTK side dispatches on these.
ButtonAction = Literal["disabled", "launch_wizard", "launch_settings"]

# Tray submenu modes per §D16.
SubmenuMode = Literal["hidden", "wizard_only", "operating"]

# Wizard cancel rule outcomes per §A2.
CancelRule = Literal["flip_toggle_off", "no_change"]


@dataclass(frozen=True)
class VaultSettingsButtonState:
    """What the 'Open Vault settings…' button in main Settings should
    do given the current toggle + vault state. Per §D16 wizard-routing.
    """

    action: ButtonAction
    enabled: bool

    @property
    def is_disabled(self) -> bool:
        return self.action == "disabled"


def vault_settings_button_state(
    *,
    toggle_active: bool,
    vault_exists: bool,
) -> VaultSettingsButtonState:
    """Map toggle + vault-existence to button state per T0 §D16.

    Three cells:
        OFF → greyed out (disabled).
        ON + no vault → enabled, launches the create/import wizard.
        ON + vault    → enabled, launches the Vault settings window.

    Note: §D16's earlier draft mentioned a ``Hidden`` state for "no
    vault configured at all", but the locked wizard-routing table
    consolidates that into the ON+no-vault branch (button stays
    visible, just opens the wizard instead). The button is **never**
    hidden in v1.
    """
    if not toggle_active:
        return VaultSettingsButtonState(action="disabled", enabled=False)
    if not vault_exists:
        return VaultSettingsButtonState(action="launch_wizard", enabled=True)
    return VaultSettingsButtonState(action="launch_settings", enabled=True)


def should_show_vault_submenu(toggle_active: bool) -> bool:
    """T3.5 — the tray's Vault submenu is visible iff the toggle is ON.

    Independent of whether a vault exists: an ON toggle on a fresh
    install still shows a submenu, just with Create / Import entries
    instead of the operating menu (see :func:`vault_submenu_entries`).
    """
    return toggle_active


def vault_submenu_entries(
    *,
    toggle_active: bool,
    vault_exists: bool,
) -> list[str]:
    """T3.5 — what the tray's Vault submenu lists, in display order.

    Returns ``[]`` when the submenu is hidden (toggle OFF). The actual
    GTK / pystray code maps these tokens to menu-item objects.

    Tokens:
        "create_vault"   → "Create vault…" — launches the wizard
        "import_vault"   → "Import vault…" — launches the wizard
        "open_vault"     → "Open Vault…"
        "sync_now"       → "Sync now"  (stub in T3)
        "export"         → "Export…"  (stub in T3)
        "import"         → "Import…"  (stub in T3)
        "settings"       → "Settings"
    """
    if not toggle_active:
        return []
    if not vault_exists:
        return ["create_vault", "import_vault"]
    return ["open_vault", "sync_now", "export", "import", "settings"]


def wizard_cancel_rule(*, vault_exists: bool) -> CancelRule:
    """What to do when the user cancels the create/import wizard.

    Always ``"no_change"``: the wizard wipes its own partial state
    (in-memory secrets zeroed, optionally exported kit shredded), but
    the Vault-active toggle stays exactly where the user put it.

    **Deviation from T0 §A2.** The original spec auto-flipped the
    toggle OFF on cancel-without-vault, on the theory that a user who
    cancels probably "doesn't actually want a vault yet". User
    feedback (2026-05-03) showed this is paternalistic — a user who
    deliberately turned the toggle ON has stated their intent, and
    canceling a wizard step is not a signal to reverse that. They
    might be reconsidering a passphrase, refreshing the generator,
    or just clicked too soon; auto-disabling the feature and hiding
    the entry point is confusing.

    Argument signature kept for backward-compat with callers that
    still pass ``vault_exists``; ignored.
    """
    _ = vault_exists  # intentionally unused
    return "no_change"
