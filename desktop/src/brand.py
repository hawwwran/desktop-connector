"""
Brand palette + light GTK/Tk theming hooks.

Intentionally calm: we only redefine Adwaita's accent + destructive color slots
so existing .suggested-action / .destructive-action buttons pick up the brand
blue / orange. The rest of libadwaita (fonts, spacing, chrome, list rows,
switches, menus) keeps following the system theme.

Palette mirrors the Android rollout (see docs/visual-identity-guide.md).
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# --- Palette (hex strings for direct CSS / Tk / PIL usage) -------------------

DC_BLUE_970 = "#000733"
DC_BLUE_950 = "#00146C"
DC_BLUE_900 = "#0920AC"
DC_BLUE_800 = "#1032D0"
DC_BLUE_700 = "#2058F0"
DC_BLUE_500 = "#3986FC"   # primary accent — connected, delivered, focus
DC_BLUE_400 = "#5898FB"   # sky — downloading
DC_BLUE_200 = "#A4D0FB"   # pale — disconnected (muted)

DC_YELLOW_500 = "#FDD00C"  # spark / uploading / reconnecting
DC_YELLOW_600 = "#FAA602"

DC_ORANGE_700 = "#EA7601"  # destructive / error (red is fully retired)

DC_WHITE_SOFT = "#E8EEFD"

# RGB tuples for PIL compositing in the tray
DC_BLUE_800_RGB = (0x10, 0x32, 0xD0)
DC_BLUE_500_RGB = (0x39, 0x86, 0xFC)
DC_BLUE_400_RGB = (0x58, 0x98, 0xFB)
DC_BLUE_200_RGB = (0xA4, 0xD0, 0xFB)
DC_YELLOW_500_RGB = (0xFD, 0xD0, 0x0C)
DC_ORANGE_700_RGB = (0xEA, 0x76, 0x01)


# --- App identity (taskbar, WM_CLASS, default window icon) -------------------

APP_ID = "com.desktopconnector.Desktop"
APP_NAME = "Desktop Connector"
ICON_NAME = "desktop-connector"  # matches hicolor install + .desktop Icon=

_ASSETS_DIR = Path(__file__).parent.parent / "assets" / "brand"


def _bundled_icon_path(preferred_size: int = 256) -> Path | None:
    candidate = _ASSETS_DIR / f"desktop-connector-{preferred_size}.png"
    if candidate.exists():
        return candidate
    for size in (256, 128, 64, 48):
        p = _ASSETS_DIR / f"desktop-connector-{size}.png"
        if p.exists():
            return p
    return None


def claim_gtk_identity() -> None:
    """
    Call once near the top of each GTK4 subprocess entry, BEFORE creating
    Adw.Application / windows. Ensures the compositor (Wayland app_id,
    X11 WM_CLASS) matches the installed .desktop file so the taskbar tile
    renders the brand icon instead of a generic 'python3' blob.
    """
    try:
        from gi.repository import GLib, Gtk, GdkPixbuf
    except Exception:
        return

    GLib.set_application_name(APP_NAME)
    GLib.set_prgname(ICON_NAME)

    # Named-theme lookup first (works after install.sh ran); file fallback for
    # devs running from source without installing.
    try:
        Gtk.Window.set_default_icon_name(ICON_NAME)
    except Exception:
        pass
    bundled = _bundled_icon_path()
    if bundled is not None:
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(str(bundled))
            Gtk.Window.set_default_icon(pb)
        except Exception as e:
            log.debug("bundled icon load failed: %s", e)


def apply_brand_css() -> None:
    """
    Install a tiny application-priority CSS provider that redefines Adwaita's
    accent + destructive color slots. Touches .suggested-action /
    .destructive-action buttons, focus rings, switches, links — nothing else.
    """
    try:
        from gi.repository import Gdk, Gtk
    except Exception:
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    css = f"""
    @define-color accent_bg_color        {DC_BLUE_500};
    @define-color accent_color           {DC_BLUE_500};
    @define-color accent_fg_color        #ffffff;
    @define-color destructive_bg_color   {DC_ORANGE_700};
    @define-color destructive_color      {DC_ORANGE_700};
    @define-color destructive_fg_color   #ffffff;
    """.encode("utf-8")

    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(css)
    except TypeError:
        # Older PyGObject signature: (data, length)
        provider.load_from_data(css, -1)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def brand_gtk_window() -> None:
    """Convenience: call once per GTK4 subprocess entry before on_activate."""
    claim_gtk_identity()
    apply_brand_css()


def brand_tk_window(root) -> None:
    """
    Apply brand identity to a Tk root window: WM_CLASS + window icon.
    Colors are applied per-widget where needed.
    """
    try:
        root.wm_title(APP_NAME)
    except Exception:
        pass
    try:
        root.wm_class(ICON_NAME, ICON_NAME)
    except Exception:
        pass
    bundled = _bundled_icon_path(128)
    if bundled is not None:
        try:
            import tkinter as tk
            img = tk.PhotoImage(file=str(bundled))
            root.wm_iconphoto(True, img)
            # Keep a reference on the root so Tk doesn't GC it.
            root._brand_icon = img  # type: ignore[attr-defined]
        except Exception as e:
            log.debug("Tk icon load failed: %s", e)
