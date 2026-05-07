"""Two-column key/value row helper shared between vault settings tabs."""

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


def _kv_row(label: str, value_widget: "Gtk.Widget") -> "Gtk.Box":
    """Two-column row for a labelled read-only value pane (settings tabs)."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    key = Gtk.Label(label=label + ":", xalign=0, css_classes=["dim-label"])
    key.set_size_request(220, -1)
    row.append(key)
    value_widget.set_hexpand(True)
    value_widget.set_xalign(0)
    row.append(value_widget)
    return row
