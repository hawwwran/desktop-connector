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
    Install a tiny application-priority CSS provider that paints the brand
    on Adwaita's accent + destructive slots, and forces switches in the
    OFF state to orange (Adwaita's default is a neutral grey trough).

    Buttons end up sky blue (DcBlue400) for suggested actions and orange
    (DcOrange700) for destructive. Focus rings, links, switches in the ON
    state inherit the accent (sky blue). Switches in the OFF state get
    an explicit orange trough so ON/OFF reads as on-brand on either side.
    """
    try:
        from gi.repository import Gdk, Gtk
    except Exception:
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    # Sky blue for buttons, orange for destructive + OFF toggles.
    #
    # libadwaita 1.5 bakes the accent/destructive colours into its
    # compiled theme at SCSS build time — so redefining the classic
    # @destructive_bg_color named token doesn't reach .destructive-action
    # buttons because the theme references a compile-time SCSS variable,
    # not @destructive_bg_color at runtime. CSS custom properties
    # (--destructive-bg-color) don't exist in 1.5 either; the parser
    # rejects them as unknown.
    #
    # The only reliable override on 1.5 is an explicit selector rule
    # loaded at application priority, which beats the theme-priority
    # compiled Adwaita stylesheet. @define-color is kept as a belt for
    # the few paths that still resolve it at runtime (links, focus
    # rings, some AdwPreferences accents on newer libadwaita).
    css = f"""
    @define-color accent_bg_color        {DC_BLUE_400};
    @define-color accent_color           {DC_BLUE_400};
    @define-color accent_fg_color        #ffffff;
    @define-color destructive_bg_color   {DC_ORANGE_700};
    @define-color destructive_color      {DC_ORANGE_700};
    @define-color destructive_fg_color   #ffffff;

    /* Explicit selectors + background-image: none. libadwaita paints
       accent buttons with a linear-gradient background-image (not
       background-color), so a plain background-color override is
       drawn UNDER the gradient and never shows. Killing the gradient
       lets the colour through.

       GTK4 CSS doesn't support !important — priority is purely per
       provider. This sheet loads at USER priority so it sits above
       libadwaita's bundled theme.

       Regular (non-flat, non-image-only, non-destructive) buttons
       paint sky blue. Flat / circular / image-only / titlebutton
       (window controls) keep the neutral theme look.

       For icon-only buttons we want coloured, add the explicit
       .brand-action-accent or .brand-action-destructive class. */
    button:not(.flat):not(.circular):not(.titlebutton):not(.destructive-action):not(.image-button):not(.close):not(.minimize):not(.maximize):not(.icon),
    button:not(.flat):not(.circular):not(.titlebutton):not(.destructive-action):not(.image-button):not(.close):not(.minimize):not(.maximize):not(.icon):hover,
    button:not(.flat):not(.circular):not(.titlebutton):not(.destructive-action):not(.image-button):not(.close):not(.minimize):not(.maximize):not(.icon):focus,
    button:not(.flat):not(.circular):not(.titlebutton):not(.destructive-action):not(.image-button):not(.close):not(.minimize):not(.maximize):not(.icon):active,
    button:not(.flat):not(.circular):not(.titlebutton):not(.destructive-action):not(.image-button):not(.close):not(.minimize):not(.maximize):not(.icon):checked {{
        background-color: {DC_BLUE_400};
        background-image: none;
        color: #ffffff;
    }}
    /* Belt + suspenders: anything inside a windowcontrols widget is
       window chrome, period. Reset whatever the cascade above did. */
    windowcontrols button,
    windowcontrols button:hover,
    windowcontrols button:focus,
    windowcontrols button:active {{
        background-color: transparent;
        background-image: none;
    }}
    button.destructive-action,
    button.destructive-action:hover,
    button.destructive-action:focus,
    button.destructive-action:active,
    button.destructive-action:checked {{
        background-color: {DC_ORANGE_700};
        background-image: none;
        color: #ffffff;
    }}

    /* Explicit opt-in for icon-only buttons that want brand colour.
       These override the "flat/image-button stays neutral" default —
       used for per-feature toolbar buttons we deliberately colour
       (Open Save Folder = accent, Clear All / trash = destructive). */
    button.brand-action-accent,
    button.brand-action-accent:hover,
    button.brand-action-accent:focus,
    button.brand-action-accent:active,
    button.brand-action-accent:checked {{
        background-color: {DC_BLUE_400};
        background-image: none;
        color: #ffffff;
    }}
    button.brand-action-destructive,
    button.brand-action-destructive:hover,
    button.brand-action-destructive:focus,
    button.brand-action-destructive:active,
    button.brand-action-destructive:checked {{
        background-color: {DC_ORANGE_700};
        background-image: none;
        color: #ffffff;
    }}
    /* Icon-only destructive: transparent background, orange symbolic
       icon. Used for per-row trash where a solid orange button would
       dominate the row. */
    button.brand-icon-destructive,
    button.brand-icon-destructive:hover,
    button.brand-icon-destructive:focus,
    button.brand-icon-destructive:active {{
        background-color: transparent;
        background-image: none;
        color: {DC_ORANGE_700};
    }}

    /* Switches: trough colour on both states. ON = sky blue, OFF =
       orange. Kill the background-image so our solid colour shows. */
    switch {{
        background-color: {DC_ORANGE_700};
        background-image: none;
    }}
    switch:checked {{
        background-color: {DC_BLUE_400};
    }}

    /* Sliders / scales (find-my-device volume): filled part of the
       trough paints sky blue. `highlight` is the filled sub-element
       on GtkScale in GTK4. */
    scale > trough > highlight {{
        background-color: {DC_BLUE_400};
        background-image: none;
    }}

    /* Status-icon recolour: Adwaita's default `.success` is green and
       `.error` is red, but the visual identity guide retires red and
       uses brand blue for success / brand orange for error. Scope to
       `image` so labels (which also use these classes) keep their
       semantic colouring elsewhere in the app. */
    image.success {{
        color: {DC_BLUE_500};
    }}
    image.error {{
        color: {DC_ORANGE_700};
    }}

    /* Destructive popover menu items: the global `button.destructive-action`
       rule above paints a solid-orange pill, which is right for banner
       Cancel buttons but heavy for a row inside a popover. Inside any
       popover, flatten the destructive style to transparent background
       + orange text so per-row hamburger menus read as conventional
       Adwaita destructive items. */
    popover button.destructive-action,
    popover button.destructive-action:hover,
    popover button.destructive-action:focus,
    popover button.destructive-action:active,
    popover button.destructive-action:checked {{
        background-color: transparent;
        background-image: none;
        color: {DC_ORANGE_700};
    }}

    /* Vault browser folder sidebar.

       Diagnosis (colour-coded debug pass, 2026-05-14): the gray
       "frame" around the sidebar card is painted by *three* stacked
       widgets, not one. Working outward:

         ListBox          — we paint this gray (the visible card).
         ScrolledWindow   — Adwaita paints this opaque too (sidebar
                            slot styling). We transparentize it via
                            `.vault-folder-sidebar-wrap`.
         Bin .sidebar-pane — AdwOverlaySplitView's internal sidebar-
                            slot widget. Adwaita paints it
                            @sidebar_bg_color via `.sidebar-pane`
                            (a CSS CLASS on a Bin, NOT a node name —
                            so a `sidebar-pane` type selector
                            silently matches nothing; only the
                            class-form `.sidebar-pane` lands). This
                            is what was still showing through after
                            we silenced the scroller — a faint gray
                            rim hugging the OSV edge.

       Why the right-hand content pane has no equivalent gray:
       Adwaita's analogous rule is `.sidebar-pane .content-pane`
       (paints `@secondary_sidebar_bg_color`) — it only kicks in
       when the content-pane is NESTED inside another sidebar-pane.
       A standalone `.content-pane` is unstyled, so the content
       slot just shows through to the window bg.

       Fix: transparentize both the scroller and the OSV pane Bin,
       paint @sidebar_bg_color on the ListBox alone with 12px
       rounded corners so the sidebar reads as a single gray card
       aligned with the right-hand boxed-list pane.

       Selector quirks worth keeping:
       - `.sidebar-pane` is a class on a Bin widget, not the
         widget's CSS node name. A bare `sidebar-pane` selector
         (no dot) matches nothing.
       - Gtk.ListBox's GTK4 node name isn't `listbox`, so a
         `listbox.vault-folder-sidebar` selector also matches
         nothing — the class-only form is what works.
       - The `vault-folder-split` marker class is set on
         `self.split_view` in layout.py so the `.sidebar-pane`
         transparentization is scoped to the vault browser only.
       - The CSS provider only reads `brand.py` once per subprocess;
         restart the vault browser to see changes. */
    .vault-folder-split > .sidebar-pane {{
        background-color: transparent;
    }}
    .vault-folder-sidebar-wrap {{
        background-color: transparent;
    }}
    .vault-folder-sidebar {{
        background-color: @sidebar_bg_color;
        border-radius: 12px;
    }}

    /* Vault browser loading skeleton (Wave 3.6).

       Painted while the centre pane is showing the "loading" stack
       child (layout.py `_build_panes`). Each row is a 56px-tall
       Gtk.Box matching the boxed-list card geometry so the layout
       doesn't jump when real folder/file rows replace them.
       `currentColor` follows the theme — light text on dark, dark
       text on light — and 0.08 alpha keeps the placeholder gentle.
       A subtle 1.4 s opacity pulse signals "in progress" without
       distracting; GTK4 supports @keyframes (see Adwaita's own
       `needs_attention` / `spin` animations). */
    @keyframes vault-skeleton-pulse {{
        0%   {{ opacity: 0.5; }}
        50%  {{ opacity: 1.0; }}
        100% {{ opacity: 0.5; }}
    }}
    .vault-skeleton-row {{
        background-color: alpha(currentColor, 0.08);
        border-radius: 12px;
        animation: vault-skeleton-pulse 1.4s ease-in-out infinite;
    }}
    """.encode("utf-8")

    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(css)
    except TypeError:
        # Older PyGObject signature: (data, length)
        provider.load_from_data(css, -1)
    # USER priority (800) > APPLICATION (600) > THEME (200). Load at USER
    # priority so libadwaita's theme can't win the cascade against us —
    # GTK4 CSS has no !important, and selector specificity within same
    # priority can go either way.
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
    )


def apply_theme_mode_from_config_dir(config_dir) -> None:
    """
    Convenience for `windows.py`: each GTK4 subprocess takes a
    `config_dir: Path`, but Config construction is heavy and not
    every window needs it. Read `theme_mode` directly from
    `config.json` (it's non-secret, not in the keyring) and apply.

    Falls back to "system" on any error — missing file (fresh
    install), malformed JSON, or absent key are all benign.
    """
    import json
    from pathlib import Path
    mode = "system"
    try:
        cfg_file = Path(config_dir) / "config.json"
        if cfg_file.exists():
            data = json.loads(cfg_file.read_text())
            value = data.get("theme_mode")
            if value in ("system", "light", "dark"):
                mode = value
    except Exception as e:
        log.debug("theme_mode read failed: %s", e)
    apply_theme_mode(mode)


def apply_theme_mode(mode: str) -> None:
    """
    Route the user's `theme_mode` config pref through libadwaita's
    Adw.StyleManager so each GTK4 subprocess honours light / dark /
    system. Mirrors the Android `ThemeMode` pref semantics:

      * "system" → Adw.ColorScheme.DEFAULT (follow desktop env)
      * "light"  → Adw.ColorScheme.FORCE_LIGHT
      * "dark"   → Adw.ColorScheme.FORCE_DARK

    Unknown values fall through to DEFAULT. Safe to call before any
    window is created — StyleManager is a process-wide singleton.
    """
    try:
        from gi.repository import Adw
    except Exception:
        return
    sm = Adw.StyleManager.get_default()
    if mode == "light":
        sm.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    elif mode == "dark":
        sm.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    else:
        sm.set_color_scheme(Adw.ColorScheme.DEFAULT)


def brand_gtk_window() -> None:
    """Convenience: call once per GTK4 subprocess entry before on_activate."""
    claim_gtk_identity()
    apply_brand_css()


def apply_pointer_cursors(root) -> None:
    """Walk `root`'s widget tree and set the pointer cursor on every
    Gtk.Button / Gtk.Switch / Gtk.LinkButton descendant.

    GTK4 doesn't support the CSS `cursor` property — cursor is a widget
    attribute, not a style. Adwaita's default theme doesn't set it on
    buttons either, so without this helper every interactive element
    keeps the default arrow. Called from each window's on_activate
    after the UI tree is built.

    Re-call after dynamically adding interactive widgets (e.g. when
    the history window rebuilds rows). The helper is cheap — walking
    a window's tree is O(n) and setting an identical cursor on a
    widget that already has one is a no-op.
    """
    try:
        from gi.repository import Gtk, Gdk
    except Exception:
        return
    pointer = Gdk.Cursor.new_from_name("pointer")

    def walk(w):
        if isinstance(w, (Gtk.Button, Gtk.Switch, Gtk.LinkButton)):
            w.set_cursor(pointer)
        try:
            child = w.get_first_child()
        except AttributeError:
            return
        while child is not None:
            walk(child)
            try:
                child = child.get_next_sibling()
            except AttributeError:
                break

    walk(root)


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
