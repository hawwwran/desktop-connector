"""Small button-builder helpers used by the row builders.

* :func:`_make_flat_action_button` — flat icon-plus-label button. Lighter
  than a pill, appropriate for cards.
* :func:`_make_overflow_button` — flat ``view-more-symbolic`` MenuButton
  whose popover holds a stack of secondary actions. Each action is
  ``(label, icon_name, callback, css_classes)``; the popover dismisses
  itself after a click so the user gets immediate feedback.
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402


def _make_flat_action_button(
    label: str,
    icon_name: str,
    callback,
    *,
    tooltip: str | None = None,
    css_classes: list[str] | None = None,
) -> Gtk.Button:
    """Flat icon-plus-label button. Lighter than a pill for cards."""
    button = Gtk.Button(css_classes=["flat", *(css_classes or [])])
    if tooltip:
        button.set_tooltip_text(tooltip)
    content = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        margin_start=4, margin_end=4,
    )
    content.append(Gtk.Image.new_from_icon_name(icon_name))
    content.append(Gtk.Label(label=label))
    button.set_child(content)
    button.connect("clicked", lambda _b: callback())
    return button


def _make_overflow_button(
    actions: list[tuple[str, str, callable, list[str]]],
) -> Gtk.MenuButton:
    """Return a flat ``view-more-symbolic`` button whose popover holds
    the supplied secondary actions.

    Each action is ``(label, icon_name, callback, css_classes)``;
    css_classes is applied to the popover button so destructive ops
    can render in the warning tone. The popover dismisses itself
    after a click so the user gets immediate feedback.
    """
    button = Gtk.MenuButton()
    button.set_icon_name("view-more-symbolic")
    button.add_css_class("flat")
    button.set_tooltip_text("More actions")

    popover = Gtk.Popover()
    popover_box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=2,
        margin_top=6, margin_bottom=6,
        margin_start=6, margin_end=6,
    )
    popover.set_child(popover_box)
    button.set_popover(popover)

    for label, icon_name, callback, css_classes in actions:
        row = Gtk.Button()
        row.add_css_class("flat")
        for klass in css_classes:
            row.add_css_class(klass)
        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            margin_top=4, margin_bottom=4,
            margin_start=4, margin_end=4,
        )
        if icon_name:
            content.append(Gtk.Image.new_from_icon_name(icon_name))
        content.append(Gtk.Label(label=label, xalign=0, hexpand=True))
        row.set_child(content)

        def _on_click(_b, cb=callback) -> None:
            popover.popdown()
            cb()

        row.connect("clicked", _on_click)
        popover_box.append(row)
    return button
