"""Shared context threaded into the Settings window helpers.

Pre-split ``show_settings`` was a single ~934-line function with a
nested ``on_activate`` that further nested ``on_retry_lp``,
``refresh_lp_status``, ``on_save``, ``on_theme_changed``,
``vault_exists_locally``, ``refresh_vault_button``,
``on_vault_toggled``, ``on_open_vault_clicked``,
``on_receive_action_changed``, ``make_limit_spin``,
``on_limit_changed``, ``on_reset_limits``, ``add_logs_group``,
``open_rename_dialog``, ``open_unpair_dialog``, ``on_add_pair``,
``_on_secret_info`` and ``_on_verify_secret_storage`` — every one of
them closing over the surrounding locals (``config``, ``crypto``,
``conn``, ``settings_registry``, ``settings_active_device``, ``stats``,
the relay-status widget refs, the spin-button dict, the verify row,
etc.).

Splitting the window into sibling modules turns each of those
captures into a named attribute on this dataclass. Helpers receive a
single ``ctx: SettingsContext`` argument instead of being closures, and
the late-bound callables (``refresh_vault_button``,
``refresh_lp_status``) are populated by ``window.py`` before any signal
that could fire them is connected.

The state containers (``limit_spinbuttons``) are mutable and shared by
reference, so the closure-effect contract from the original (the spin
button dict mutated by ``make_limit_spin`` and read by
``on_reset_limits``) is preserved automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402,F401

if TYPE_CHECKING:
    from ..config import Config
    from ..crypto import KeyManager
    from ..connection import ConnectionManager
    from ..devices import ConnectedDevice, ConnectedDeviceRegistry


@dataclass
class SettingsContext:
    """Bag of state + widget refs threaded through the Settings-window helpers."""

    # Constructor inputs.
    config_dir: Path
    config: "Config"
    crypto: "KeyManager"
    conn: "ConnectionManager"

    # Connected-devices registry + active-device snapshot, captured at
    # ``show_settings`` entry like the original.
    settings_registry: "ConnectedDeviceRegistry" = None  # type: ignore[assignment]
    settings_active_device: "Optional[ConnectedDevice]" = None
    stats: Optional[dict] = None

    # Top-level Adw.Application + window built in window.py.
    app: Any = None  # Adw.Application
    win: Any = None  # Adw.ApplicationWindow

    # Top-level content box that group builders append to.
    content: Any = None  # Gtk.Box

    # Relay group widget refs (long-poll status row + retry button live
    # under a 3 s GLib timeout that calls ``refresh_lp_status``).
    poll_status_file: Optional[Path] = None
    lp_row: Any = None  # Adw.ActionRow
    retry_btn: Any = None  # Gtk.Button
    server_row: Any = None  # Adw.EntryRow
    save_btn: Any = None  # Gtk.Button

    # Vault group widget refs + the late-bound ``refresh_vault_button``
    # callable, assigned in ``group_vault.build`` before any signal that
    # could fire it is connected.
    vault_active_row: Any = None  # Adw.SwitchRow
    open_vault_btn: Any = None  # Gtk.Button
    refresh_vault_button: Optional[Callable[[], None]] = None

    # Receive-action limit spin buttons keyed by ``(action_key,
    # limit_name)``. Same dict identity the original
    # ``make_limit_spin`` closure mutated, so ``on_reset_limits`` sees
    # every entry by reference.
    limit_spinbuttons: dict = field(default_factory=dict)

    # Pairings group: list of currently-paired devices fetched once at
    # window open (the original captured this in a local; on the context
    # it's reachable from both pairings + statistics builders).
    paired_devices: list = field(default_factory=list)
    active_device_id: Optional[str] = None

    # Security group: verify row widget ref, mutated by the verify-
    # button callback in ``group_secret_storage``.
    verify_row: Any = None  # Adw.ActionRow
