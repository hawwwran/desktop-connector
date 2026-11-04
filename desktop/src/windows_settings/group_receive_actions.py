"""Receive Actions + Receive Action Flood Protection groups.

Pre-split this lived inline in ``on_activate`` as two adjacent
``Adw.PreferencesGroup`` blocks plus their nested ``on_receive_action_changed``,
``make_limit_spin``, ``on_limit_changed`` and ``on_reset_limits``
closures. The spin-button registry (``limit_spinbuttons``) is shared
state — populated by ``make_limit_spin`` and read by
``on_reset_limits`` — and is carried by reference on
``ctx.limit_spinbuttons``.

The ``k=kind, acts=actions`` and ``_action_key=action_key,
_limit_name=limit_name`` default-arg captures are preserved verbatim:
they pin the loop variables at definition time, not at call time.
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

from ..config import (
    DEFAULT_RECEIVE_ACTION_LIMITS,
    RECEIVE_ACTION_COPY,
    RECEIVE_ACTION_KEY_DOCUMENT_OPEN,
    RECEIVE_ACTION_KEY_IMAGE_OPEN,
    RECEIVE_ACTION_KEY_TEXT_COPY,
    RECEIVE_ACTION_KEY_URL_COPY,
    RECEIVE_ACTION_KEY_URL_OPEN,
    RECEIVE_ACTION_KEY_VIDEO_OPEN,
    RECEIVE_ACTION_LIMIT_BATCH,
    RECEIVE_ACTION_LIMIT_MAX,
    RECEIVE_ACTION_LIMIT_MINUTE,
    RECEIVE_ACTION_NONE,
    RECEIVE_ACTION_OPEN,
    RECEIVE_KIND_DOCUMENT,
    RECEIVE_KIND_IMAGE,
    RECEIVE_KIND_TEXT,
    RECEIVE_KIND_URL,
    RECEIVE_KIND_VIDEO,
)
from .context import SettingsContext


def build(ctx: SettingsContext) -> None:
    """Append both groups to ``ctx.content`` in original order."""
    config = ctx.config

    # Receive actions
    receive_group = Adw.PreferencesGroup(title="Receive Actions")
    ctx.content.append(receive_group)

    receive_action_rows = (
        (
            RECEIVE_KIND_URL,
            "URL",
            "What to do when received text is detected as a URL.",
            (
                ("Open in default browser", RECEIVE_ACTION_OPEN),
                ("Copy to clipboard", RECEIVE_ACTION_COPY),
                ("No action", RECEIVE_ACTION_NONE),
            ),
        ),
        (
            RECEIVE_KIND_TEXT,
            "Text",
            "What to do after receiving text that is not only a URL.",
            (
                ("Copy to clipboard", RECEIVE_ACTION_COPY),
                ("No action", RECEIVE_ACTION_NONE),
            ),
        ),
        (
            RECEIVE_KIND_IMAGE,
            "Image",
            "What to do after receiving an image file.",
            (
                ("Open in default image viewer", RECEIVE_ACTION_OPEN),
                ("No action", RECEIVE_ACTION_NONE),
            ),
        ),
        (
            RECEIVE_KIND_VIDEO,
            "Video",
            "What to do after receiving a video file.",
            (
                ("Open in default video viewer", RECEIVE_ACTION_OPEN),
                ("No action", RECEIVE_ACTION_NONE),
            ),
        ),
        (
            RECEIVE_KIND_DOCUMENT,
            "Document",
            "What to do after receiving a document file.",
            (
                ("Open in default document viewer", RECEIVE_ACTION_OPEN),
                ("No action", RECEIVE_ACTION_NONE),
            ),
        ),
    )

    for kind, title, subtitle, options in receive_action_rows:
        labels = [label for label, _action in options]
        actions = [action for _label, action in options]
        row = Adw.ComboRow(
            title=title,
            subtitle=subtitle,
            model=Gtk.StringList.new(labels),
        )
        current_action = config.get_receive_action(kind)
        try:
            row.set_selected(actions.index(current_action))
        except ValueError:
            row.set_selected(0)

        def on_receive_action_changed(combo, _pspec, k=kind, acts=actions):
            selected = combo.get_selected()
            if 0 <= selected < len(acts):
                config.set_receive_action(k, acts[selected])

        row.connect("notify::selected", on_receive_action_changed)
        receive_group.add(row)

    # Receive action flood protection
    flood_group = Adw.PreferencesGroup(title="Receive Action Flood Protection")
    ctx.content.append(flood_group)

    reset_row = Adw.ActionRow(
        title="Flood limits",
        subtitle="0 means unlimited",
    )
    reset_btn = Gtk.Button(label="Reset to defaults", valign=Gtk.Align.CENTER)
    reset_row.add_suffix(reset_btn)
    flood_group.add(reset_row)

    limit_spinbuttons = ctx.limit_spinbuttons

    def make_limit_spin(action_key: str, limit_name: str) -> Gtk.SpinButton:
        value = config.get_receive_action_limits(action_key)[limit_name]
        adjustment = Gtk.Adjustment(
            value=float(value),
            lower=0.0,
            upper=float(RECEIVE_ACTION_LIMIT_MAX),
            step_increment=1.0,
            page_increment=10.0,
        )
        spin = Gtk.SpinButton(
            adjustment=adjustment,
            climb_rate=1.0,
            digits=0,
            numeric=True,
            valign=Gtk.Align.CENTER,
        )
        spin.set_width_chars(3)

        def on_limit_changed(widget, _action_key=action_key, _limit_name=limit_name):
            config.set_receive_action_limit(
                _action_key,
                _limit_name,
                int(widget.get_value()),
            )

        spin.connect("value-changed", on_limit_changed)
        limit_spinbuttons[(action_key, limit_name)] = spin
        return spin

    receive_action_limit_rows = (
        (RECEIVE_ACTION_KEY_URL_OPEN, "Open URL"),
        (RECEIVE_ACTION_KEY_URL_COPY, "Copy URL to clipboard"),
        (RECEIVE_ACTION_KEY_TEXT_COPY, "Copy text to clipboard"),
        (RECEIVE_ACTION_KEY_IMAGE_OPEN, "Open image"),
        (RECEIVE_ACTION_KEY_VIDEO_OPEN, "Open video"),
        (RECEIVE_ACTION_KEY_DOCUMENT_OPEN, "Open document"),
    )

    limits_table = Gtk.Grid(
        column_spacing=16,
        row_spacing=8,
        margin_top=8,
        margin_bottom=8,
        margin_start=12,
        margin_end=12,
    )
    flood_group.add(limits_table)

    table_headers = ("Action type", "Max per batch", "Max per minute")
    for column, header in enumerate(table_headers):
        label = Gtk.Label(label=header, xalign=0.0)
        label.add_css_class("dim-label")
        label.add_css_class("caption-heading")
        limits_table.attach(label, column, 0, 1, 1)

    for row_index, (action_key, title) in enumerate(receive_action_limit_rows, start=1):
        action_label = Gtk.Label(
            label=title,
            xalign=0.0,
            valign=Gtk.Align.CENTER,
            hexpand=True,
        )
        limits_table.attach(action_label, 0, row_index, 1, 1)
        limits_table.attach(
            make_limit_spin(action_key, RECEIVE_ACTION_LIMIT_BATCH),
            1,
            row_index,
            1,
            1,
        )
        limits_table.attach(
            make_limit_spin(action_key, RECEIVE_ACTION_LIMIT_MINUTE),
            2,
            row_index,
            1,
            1,
        )

    def on_reset_limits(btn):
        config.reset_receive_action_limits()
        for action_key, limits in DEFAULT_RECEIVE_ACTION_LIMITS.items():
            for limit_name, value in limits.items():
                spin = limit_spinbuttons.get((action_key, limit_name))
                if spin is not None:
                    spin.set_value(float(value))
        btn.set_label("✓ Reset")
        btn.set_sensitive(False)

        def restore_reset_label() -> bool:
            btn.set_label("Reset to defaults")
            btn.set_sensitive(True)
            return False

        GLib.timeout_add(2000, restore_reset_label)

    reset_btn.connect("clicked", on_reset_limits)
