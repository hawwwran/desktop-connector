"""Shared state for the v2 Vault browser.

The v1 module kept everything in a flat ``state`` dict captured by 50+
nested closures. v2 lifts these into a dataclass so the structural
refactor preserves the same fields verbatim — only the access shape
changes (``self.state.path`` instead of ``state["path"]``), which
keeps the diff readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrowserState:
    """Mutable browser state mirrored from v1's ``state`` dict.

    ``manifest`` is the decrypted manifest dict (or ``None`` until the
    first refresh succeeds). ``path`` is the current location inside
    the vault (``""`` is the root, otherwise something like
    ``"Documents/draft"``). ``back`` / ``forward`` are the navigation
    history stacks; ``selected_file`` is the currently-highlighted
    file row; ``show_deleted`` mirrors the toggle state.
    """

    manifest: dict | None = None
    path: str = ""
    back: list[str] = field(default_factory=list)
    forward: list[str] = field(default_factory=list)
    selected_file: dict | None = None
    show_deleted: bool = False
