"""v2 Vault browser package.

Built incrementally as a parallel implementation of
``windows_vault_browser.py``. Exposes ``show_vault_browser_v2`` which
the tray's "Open Vault NEW" menu item and ``windows.py``
``vault-browser-v2`` dispatch route through. The v1 module stays in
place until v2 reaches parity and the user signs off.
"""

from .app import VaultBrowser, show_vault_browser_v2

__all__ = ["VaultBrowser", "show_vault_browser_v2"]
