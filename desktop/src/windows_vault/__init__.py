"""Vault GTK windows package.

Three subprocess windows:
  * ``vault-main`` — the deep settings tab strip (recovery, folders, devices…).
  * ``vault-onboard`` — the create / import wizard.
  * ``vault-passphrase-generator`` — standalone diceware generator.

Pre-split this lived as a single ~2500-line ``windows_vault.py``;
the package keeps the same import surface for callers (``windows.py``).
"""

from .main_window import show_vault_main
from .onboard_window import show_vault_onboard
from .passphrase_generator import show_vault_passphrase_generator

__all__ = [
    "show_vault_main",
    "show_vault_onboard",
    "show_vault_passphrase_generator",
]
