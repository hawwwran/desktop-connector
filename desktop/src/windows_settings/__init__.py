"""Settings window (`python -m src.windows settings`).

Pre-split this lived as a single ~934-line ``windows_settings.py`` with
one giant ``show_settings`` function whose nested ``on_activate``
in turn nested ``on_retry_lp``, ``refresh_lp_status``, ``on_save``,
``on_theme_changed``, ``vault_exists_locally``, ``refresh_vault_button``,
``on_vault_toggled``, ``on_open_vault_clicked``,
``on_receive_action_changed``, ``make_limit_spin``, ``on_limit_changed``,
``on_reset_limits``, ``add_logs_group`` (with ``on_download_logs`` /
``on_clear_logs``), ``open_rename_dialog``, ``open_unpair_dialog``,
``on_add_pair``, ``_on_secret_info`` and ``_on_verify_secret_storage``,
each closing over a forest of locals (``config``, ``crypto``, ``conn``,
``settings_registry``, ``stats``, ``settings_active_device``, the row /
spinbutton / button widget refs, etc.).

The package keeps the same public import surface (callers just need
``show_settings``); internals are split per-cohesion across the sibling
``group_*.py`` modules.
"""

from .window import show_settings

__all__ = ["show_settings"]
