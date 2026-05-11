"""Shared context threaded into the Folders tab helpers.

Pre-split ``build_vault_folders_tab`` was a single ~1300-line function
with ~50 nested closures sharing a forest of locals: state dicts
(``usage_by_folder_state``, ``selection_state``, ``sync_in_flight``,
``action_in_flight``), the ``BindingCancellationRegistry``, the
``VaultRuntime`` + ``VaultLocalIndex`` handles, the constructor inputs
(``app``, ``parent_window``, ``config_dir``, ``config``, ``vault_id``),
and a stack of widget references plus a few ``refresh_*`` callables
that mutated the panes.

Splitting the tab into sibling modules turns each of those captures
into a named attribute on this dataclass. Helpers receive a single
``ctx: FoldersContext`` argument instead of being closures, and the
late-bound ``refresh_*`` callables are populated by ``tab.py`` before
any signal that could fire them is connected.

The state dicts are mutable and shared by reference, so the
closure-effect contract from the original (``sync_in_flight[bid] =
True`` in one helper visible to another) is preserved automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402,F401

if TYPE_CHECKING:
    from ..vault.binding.lifecycle import BindingCancellationRegistry
    from ..vault.folder.runtime import VaultRuntime
    from ..vault.state.local_index import VaultLocalIndex


@dataclass
class FoldersContext:
    """Bag of state + widget refs threaded through the Folders-tab helpers."""

    # Constructor inputs.
    app: "Adw.Application"
    parent_window: "Adw.ApplicationWindow"
    config_dir: Path
    config: Any
    vault_id: str

    # Composed services.
    local_index: "VaultLocalIndex"
    runtime: "VaultRuntime"
    cancellation_registry: "BindingCancellationRegistry"

    # Mutable shared state — same dict identities the original closures
    # mutated, so cross-helper visibility is preserved.
    usage_by_folder_state: dict = field(default_factory=lambda: {"value": {}})
    selection_state: dict = field(
        default_factory=lambda: {"folder_id": None}
    )
    sync_in_flight: dict = field(default_factory=dict)
    action_in_flight: dict = field(default_factory=dict)
    folder_rows_by_id: dict = field(default_factory=dict)
    suspend_selection_signal: dict = field(
        default_factory=lambda: {"value": False}
    )

    # Widget refs needed by helpers.
    sidebar_status: "Gtk.Label" = None  # type: ignore[assignment]
    content_status: "Gtk.Label" = None  # type: ignore[assignment]
    content_box: "Gtk.Box" = None  # type: ignore[assignment]
    folder_list: "Gtk.ListBox" = None  # type: ignore[assignment]
    split: "Adw.NavigationSplitView" = None  # type: ignore[assignment]
    content_page: "Adw.NavigationPage" = None  # type: ignore[assignment]

    # Late-bound callables — assigned in tab.py before any signal that
    # could fire them is connected.
    refresh_all: Callable[..., None] = None  # type: ignore[assignment]
    refresh_sidebar: Callable[[], None] = None  # type: ignore[assignment]
    render_detail: Callable[[], None] = None  # type: ignore[assignment]
    refresh_folders_usage_async: Callable[..., None] = None  # type: ignore[assignment]
    set_sidebar_status: Callable[..., None] = None  # type: ignore[assignment]
    set_content_status: Callable[..., None] = None  # type: ignore[assignment]
    open_browse_local: Callable[[str], None] = None  # type: ignore[assignment]
