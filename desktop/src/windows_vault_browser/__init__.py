"""Vault browser package.

Structural refactor of the original ``windows_vault_browser.py`` —
v1's 1837-line ``on_activate`` closure body is split into a
``VaultBrowser`` class with discrete methods. The ``v2`` directory
name was a transition marker; the package was renamed to
``windows_vault_browser/`` once parity was confirmed.
"""

from .app import VaultBrowser, show_vault_browser

__all__ = ["VaultBrowser", "show_vault_browser"]
