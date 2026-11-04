"""Appearance group: theme combo (System / Light / Dark).

Pre-split this lived inline in ``on_activate`` as the
``appearance_group = Adw.PreferencesGroup(title="Appearance")`` block
plus the ``on_theme_changed(combo, _pspec, modes=theme_modes)``
closure. The ``modes=theme_modes`` default-arg capture is preserved
verbatim — it pins the tuple by binding into a positional default at
function-definition time, not by closing over the surrounding name.
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from .context import SettingsContext


def build(ctx: SettingsContext) -> Adw.PreferencesGroup:
    config = ctx.config

    appearance_group = Adw.PreferencesGroup(title="Appearance")
    ctx.content.append(appearance_group)

    theme_modes = (
        ("System", "system"),
        ("Light", "light"),
        ("Dark", "dark"),
    )
    theme_model = Gtk.StringList.new([label for label, _ in theme_modes])
    theme_row = Adw.ComboRow(
        title="Theme",
        subtitle="Match desktop, or force light / dark mode.",
        model=theme_model,
    )
    current_mode = config.theme_mode
    for idx, (_, value) in enumerate(theme_modes):
        if value == current_mode:
            theme_row.set_selected(idx)
            break

    def on_theme_changed(combo, _pspec, modes=theme_modes):
        i = combo.get_selected()
        if 0 <= i < len(modes):
            new_mode = modes[i][1]
            if new_mode != config.theme_mode:
                config.theme_mode = new_mode
                # Live-apply to this window so the change is visible
                # without a restart. Other open subprocesses pick it
                # up on their next reload (config.json is the source
                # of truth).
                from ..brand import apply_theme_mode
                apply_theme_mode(new_mode)

    theme_row.connect("notify::selected", on_theme_changed)
    appearance_group.add(theme_row)

    return appearance_group
