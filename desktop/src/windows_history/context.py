"""Shared context threaded into the History window helpers.

Pre-split ``show_history`` was a single ~1000-line function with a
nested ``on_activate`` that further nested ``_selected_device_id``,
``_selected_device_name``, ``_empty_history_text``, ``_compute_status``,
``_row_key``, ``_create_row``, ``_update_row``, ``build_list``,
``refresh_tick``, ``_scrub_zombie_waiting``, ``show_toast``,
``_do_local_remove``, ``on_delete``, ``on_clear_all``,
``on_history_device_changed`` and ``on_item_click`` — every one of
them closing over a forest of locals (``config``, ``crypto``,
``history``, ``selected_device``, the row-state dicts, the widget
refs, etc.).

Splitting the window into sibling modules turns each of those
captures into a named attribute on this dataclass. Helpers receive a
single ``ctx: HistoryContext`` argument instead of being closures, and
the late-bound callables (``build_list``, ``refresh_tick``,
``_reset_history_view``, ``show_toast``, ``_do_local_remove``) are
populated by ``window.py`` before any signal that could fire them is
connected.

The state lists/dicts (``has_active``, ``row_widgets``,
``all_widgets``, ``structural_sig``, ``empty_label``, ``progress_sig``,
``selected_device``) are mutable and shared by reference, so the
closure-effect contract from the original (e.g. ``structural_sig[0]
= s_sig`` in ``build_list`` visible to ``_do_local_remove``) is
preserved automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402,F401

if TYPE_CHECKING:
    from ..config import Config
    from ..crypto import KeyManager
    from ..history import TransferHistory


@dataclass
class HistoryContext:
    """Bag of state + widget refs threaded through the History-window helpers."""

    # Constructor inputs.
    config_dir: Path
    config: "Config"
    crypto: "KeyManager"
    history: "TransferHistory"

    # Top-level Adw.Application + window built in window.py.
    app: Any = None  # Adw.Application
    win: Any = None  # Adw.ApplicationWindow

    # Device picker state — mutable list so picker callbacks see the
    # latest selection without re-binding.
    selected_device: List[Any] = field(default_factory=list)
    paired_devices: List[Any] = field(default_factory=list)
    device_picker: Any = None  # Adw.ComboRow
    clear_all_btn: Any = None  # Gtk.Button

    # List container + per-row state. Same dict identities the original
    # closures mutated, so cross-helper visibility is preserved.
    list_container: Any = None  # Gtk.Box
    has_active: List[bool] = field(default_factory=lambda: [False])
    # row_widgets[row_key] = (box_widget, row, progress_bar_or_None)
    row_widgets: dict = field(default_factory=dict)
    all_widgets: list = field(default_factory=list)  # ordered group children
    structural_sig: list = field(default_factory=lambda: [None])
    empty_label: list = field(default_factory=lambda: [None])
    progress_sig: list = field(default_factory=lambda: [None])

    # Late-bound callables — assigned in window.py before any signal
    # that could fire them is connected.
    build_list: Optional[Callable[[], bool]] = None
    refresh_tick: Optional[Callable[[], bool]] = None
    reset_history_view: Optional[Callable[[], None]] = None
    show_toast: Optional[Callable[[Any, str], None]] = None
