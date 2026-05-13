"""F-U09 + F-U10 + F-U16 — source-pin tests for the slice 06 polish.

The slice-06 polish landed three classes of small UX fix that share a
risk: someone deleting the surface inadvertently in a future refactor.
Behavioural correctness is unprovable without driving GTK4 + AT-SPI
in a Wayland session, so source-pinning catches removal regressions
at the cheapest layer per the F-T08 anti-regression doctrine.

- **F-U09**: the test-recovery dialog's wipe-after-success switch
  gains a ``warning``-styled loss banner explaining that wiping the
  kit removes the only restore path. ``feedback_security_ux.md``
  treats security-impacting toggles' loss surface as a hard
  requirement, not a copy preference.
- **F-U10**: ``Gtk.Switch`` and ``Gtk.Entry`` widgets in the dialog
  expose accessible-name properties via
  ``Gtk.AccessibleProperty.LABEL`` so dogtail / Accerciser see
  meaningful descriptions instead of "switch (no name)".
- **F-U16**: the browser's outer + inner ``GtkPaned`` accept
  ``set_shrink_start_child(True)`` so the tree pane doesn't pin the
  whole view at 220 px on small windows.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()


REPO_ROOT = Path(os.path.dirname(__file__) or ".").resolve().parent.parent
WINDOWS_VAULT_PKG = REPO_ROOT / "desktop" / "src" / "windows_vault"
WINDOWS_BROWSER_PKG = REPO_ROOT / "desktop" / "src" / "windows_vault_browser"
WINDOWS_IMPORT = REPO_ROOT / "desktop" / "src" / "windows_vault_import.py"


def _read_windows_vault_pkg() -> str:
    """Concatenate every module under ``windows_vault/`` into one string
    for the source-pin greppers (the historical monolith was split into
    a package on 2026-05-07)."""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(WINDOWS_VAULT_PKG.glob("*.py"))
    )


def _read_windows_vault_browser_pkg() -> str:
    """Concatenate every module under ``windows_vault_browser/`` so the
    F-U16 paned pins survive the mixin-extraction split (2026-05-08).
    The original monolithic app.py was broken into topical mixin
    modules; the layout builders now live in ``layout.py``."""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(WINDOWS_BROWSER_PKG.glob("*.py"))
    )


class WipeSwitchLossWarningTests(unittest.TestCase):
    """F-U09 — the wipe-after-success switch must surface a real warning."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_windows_vault_pkg()

    def test_wipe_warning_uses_warning_css_class(self) -> None:
        """The warning Label belongs to the same CSS family as the
        recovery-untested + export-reminder banners — ``warning`` is
        Adwaita's amber/yellow surface, distinct from ``dim-label``.
        """
        self.assertIn('wipe_warning.add_css_class("warning")', self.text)

    def test_wipe_warning_mentions_unrecoverability(self) -> None:
        """The label must spell out what the user loses, not just say
        "irreversible". Per ``feedback_security_ux.md``: prominent
        loss warning, not buried.
        """
        self.assertIn("permanently unrecoverable", self.text)
        self.assertIn("password manager", self.text)


class AccessibilityLabelBindingTests(unittest.TestCase):
    """F-U10 — AT-SPI labels on the test-recovery widgets."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_windows_vault_pkg()

    def test_widgets_use_accessible_property_label(self) -> None:
        """Each switch/entry that previously relied on placeholder text
        now binds an explicit ``Gtk.AccessibleProperty.LABEL``. We
        check for at least the four sites we wired (kit_entry,
        vault_id_entry, passphrase_entry, wipe_switch + delete_switch).
        """
        self.assertGreaterEqual(
            self.text.count("Gtk.AccessibleProperty.LABEL"), 5,
            "F-U10 wires accessible-name on at least 5 widgets",
        )

    def test_wipe_switch_label_text_matches_visible_label(self) -> None:
        """AT-SPI screen readers should announce the same string the
        sighted user reads — keep them aligned.
        """
        # The visible Gtk.Label and the accessible-name string share
        # the "Securely delete the recovery kit file" prefix.
        self.assertGreaterEqual(
            self.text.count(
                "Securely delete the recovery kit file after a successful test"
            ),
            2,
            "wipe_switch accessible-name should match its visible label",
        )


class BrowserPanedShrinkableTests(unittest.TestCase):
    """F-U16 — outer + inner GtkPaned allow the tree/detail panes to
    shrink under narrow windows."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_windows_vault_browser_pkg()

    def test_no_shrink_start_child_false(self) -> None:
        """The previous regression form pinned both panes with
        ``set_shrink_start_child(False)``. Polish removed both calls;
        guard against regressions reintroducing the pin.
        """
        self.assertNotIn("set_shrink_start_child(False)", self.text)

    def test_shrink_start_children_enabled(self) -> None:
        """``set_shrink_start_child(True)`` is enabled on the inner
        ``right`` Paned (file list ↔ details split).

        Wave 2.5 (2026-05-13): the outer Gtk.Paned was replaced by
        Adw.OverlaySplitView, which carries its own width-shrinkability
        semantics via ``set_min_sidebar_width`` / ``set_max_sidebar_width``
        — verified separately below. The inner pane retains the F-U16
        shrink-start enable.
        """
        self.assertGreaterEqual(
            self.text.count("set_shrink_start_child(True)"), 1,
            "F-U16 expects shrink-start-child enabled on the inner pane",
        )
        self.assertIn("set_min_sidebar_width", self.text)
        self.assertIn("set_max_sidebar_width", self.text)

    def test_min_content_widths_lowered(self) -> None:
        """The tree min was 220 (matching set_position) — fixed pin.
        Polish lowered to 160 / 200 so the user can drag the divider
        smaller than the natural width.
        """
        self.assertIn("min_content_width=160", self.text)
        self.assertIn("min_content_width=200", self.text)


class BrowserChromeRedesignTests(unittest.TestCase):
    """Vault Browser chrome redesign (Waves 1–3.5, landed 2026-05-13).

    The chrome moved from a body strip of 8 pill buttons to Adwaita
    HeaderBar + per-row hamburger menus + responsive
    Adw.OverlaySplitView. The surfaces are easy to silently revert in
    a future refactor (e.g. swapping the listbox back to a grid for
    "easier" rendering), so source-pin the key anchors.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_windows_vault_browser_pkg()

    def test_header_bar_status_icon_present(self) -> None:
        """Wave 3.1: status moved off the body label onto a fixed-position
        header-bar Gtk.Image so painting status never shifts layout."""
        self.assertIn("self._status_icon = Gtk.Image()", self.text)
        self.assertIn("header_bar.pack_end(self._status_icon)", self.text)

    def test_header_window_title_subtitle(self) -> None:
        """Wave 1.5: breadcrumb absorbed into the Adw.WindowTitle
        subtitle. Title stays ``Vault``; subtitle carries the path."""
        self.assertIn('Adw.WindowTitle(title="Vault"', self.text)
        # _render_all drives the subtitle on every render pass.
        self.assertIn("set_subtitle(", self.text)

    def test_responsive_overlay_split_view(self) -> None:
        """Wave 2.5: outer split is Adw.OverlaySplitView, not Gtk.Paned,
        with a max-width: 600sp breakpoint that collapses the sidebar
        and a header-bar toggle button to reveal it.
        """
        self.assertIn("Adw.OverlaySplitView()", self.text)
        self.assertIn("max-width: 600sp", self.text)
        self.assertIn("Adw.Breakpoint.new", self.text)
        self.assertIn('icon_name="sidebar-show-symbolic"', self.text)
        # Sidebar width clamp lives on the OverlaySplitView, not the
        # retired outer Gtk.Paned.
        self.assertIn("set_min_sidebar_width", self.text)
        self.assertIn("set_max_sidebar_width", self.text)

    def test_navigation_sidebar_listbox(self) -> None:
        """Wave 2: folder tree is a Gtk.ListBox carrying the Adwaita
        ``navigation-sidebar`` class so it picks up the same selection
        + hover treatment Files/Calendar/Settings sidebars use."""
        self.assertIn('add_css_class("navigation-sidebar")', self.text)
        self.assertIn("self.tree_listbox = Gtk.ListBox()", self.text)
        # Row activation drives navigation, not per-button clicked.
        self.assertIn("_on_tree_row_activated", self.text)

    def test_file_card_listbox(self) -> None:
        """Wave 3.2: file list rebuilt as a ``boxed-list`` ListBox of
        cards. The old Gtk.Grid with hardcoded columns is retired."""
        self.assertIn('add_css_class("boxed-list")', self.text)
        self.assertIn("_make_file_card_row", self.text)
        self.assertIn("_make_folder_card_row", self.text)
        # No regression to "render the file list as a Gtk.Grid": the
        # grid helper attribute should stay None.
        self.assertIn(
            "self.list_grid: Gtk.Grid | None = None", self.text,
        )

    def test_per_row_hamburger_menus(self) -> None:
        """Waves 3.2 / 3.3: file cards and non-root sidebar rows carry
        per-row Gtk.MenuButton with ``view-more-symbolic``."""
        self.assertIn('icon_name="view-more-symbolic"', self.text)
        # ``_make_row_menu_button`` is the shared factory; counted
        # callsites: definition + folder card + file card + sidebar
        # row = 4. Drop below that and we've lost a surface.
        self.assertGreaterEqual(
            self.text.count("_make_row_menu_button"), 4,
            "expected definition + 3 callsites for the row menu factory",
        )

    def test_destructive_popover_brand_css_present(self) -> None:
        """The destructive-menu items in popovers must not paint as
        solid orange pills (heavy for popover rows). The brand CSS
        adds a scoped override for ``popover button.destructive-action``
        that flattens the background — guard against its removal.
        """
        # The brand stylesheet lives outside the browser package, so
        # this anchor is checked against the brand module directly.
        brand_path = (
            REPO_ROOT / "desktop" / "src" / "brand.py"
        )
        brand_text = brand_path.read_text(encoding="utf-8")
        self.assertIn("popover button.destructive-action", brand_text)
        # Same file should also remap status-icon colours so the
        # header status icon paints brand blue / orange instead of
        # Adwaita default green / red.
        self.assertIn("image.success", brand_text)
        self.assertIn("image.error", brand_text)


class BrowserChromeA11yTests(unittest.TestCase):
    """F-U10 — AT-SPI accessible-name bindings on the new chrome.

    Tooltip text alone is not an accessible name. Screen readers /
    dogtail / Accerciser see icon-only buttons as "togglebutton"
    or "menu button" with no context unless an explicit
    ``Gtk.AccessibleProperty.LABEL`` is bound. The chrome redesign
    added three icon-only surfaces (status icon, sidebar toggle, per-
    row hamburger menus) that need the binding.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_windows_vault_browser_pkg()

    def test_status_icon_has_accessible_label(self) -> None:
        """Wave 3.1 status icon — labelled ``Status indicator`` so a
        screen reader can find it. Tooltip carries the live state."""
        self.assertIn("Status indicator", self.text)

    def test_sidebar_toggle_has_accessible_label(self) -> None:
        """Wave 2.5 sidebar toggle — labelled ``Toggle folder sidebar``
        so the narrow-mode reveal button is named."""
        self.assertIn("Toggle folder sidebar", self.text)

    def test_per_row_hamburger_has_contextual_label(self) -> None:
        """Waves 3.2 / 3.3 — file and folder rows attach a label
        like ``Actions for file foo.txt`` / ``Actions for folder
        Documents`` so a row's menu button is identifiable in
        sequence with the rest of the row contents.
        """
        self.assertIn("Actions for file", self.text)
        self.assertIn("Actions for folder", self.text)

    def test_chrome_a11y_label_count(self) -> None:
        """At least 3 ``Gtk.AccessibleProperty.LABEL`` bindings landed
        for the chrome redesign (status icon, sidebar toggle, per-row
        menu). Catches a regression that drops all of them.
        """
        self.assertGreaterEqual(
            self.text.count("Gtk.AccessibleProperty.LABEL"), 3,
            "expected at least 3 accessible-label bindings on the chrome",
        )


class ImportPassphraseAsymmetryTests(unittest.TestCase):
    """F-U07 — confirm-field asymmetry between create and import wizards
    is intentionally documented; the rationale must stay near the entry
    so a later contributor doesn't "fix" the asymmetry without reading
    the threat model.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WINDOWS_IMPORT.read_text(encoding="utf-8")

    def test_no_confirm_field_in_import_wizard(self) -> None:
        """Two PasswordEntry widgets would imply a confirm field; we
        keep it at one. Catches the polish "fix" of adding pp_confirm
        to import without reading the comment.
        """
        self.assertEqual(
            self.text.count("Gtk.PasswordEntry"), 1,
            "import wizard should keep a single passphrase entry",
        )

    def test_asymmetry_rationale_documented(self) -> None:
        """The single-entry decision is explained in-line — anyone
        adding a confirm field has to read past the comment first.
        """
        # The rationale comment names create wizard's lockout class +
        # spells out import's symmetric "AEAD fails loudly" backstop.
        # Either anchor proves the comment survived a future refactor.
        text_lower = self.text.lower()
        self.assertIn("silently locks the user out", text_lower)
        self.assertIn("bundle decryption failed", text_lower)


if __name__ == "__main__":
    unittest.main()
