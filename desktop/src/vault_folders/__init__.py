"""Vault settings Folders-tab package.

Pre-split this lived as a single ~1300-line ``vault_folders_tab.py``
with ~50 nested closures inside ``build_vault_folders_tab``. The
package keeps the same public import surface (callers just need
``build_vault_folders_tab``); internals are split per-cohesion across
the sibling modules.
"""

from .tab import build_vault_folders_tab

__all__ = ["build_vault_folders_tab"]
