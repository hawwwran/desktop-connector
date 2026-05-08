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
        """Both ``set_shrink_start_child(True)`` calls land on the outer
        ``paned`` and inner ``right`` paned.
        """
        self.assertGreaterEqual(
            self.text.count("set_shrink_start_child(True)"), 2,
            "F-U16 expects shrink-start-child enabled on both panes",
        )

    def test_min_content_widths_lowered(self) -> None:
        """The tree min was 220 (matching set_position) — fixed pin.
        Polish lowered to 160 / 200 so the user can drag the divider
        smaller than the natural width.
        """
        self.assertIn("min_content_width=160", self.text)
        self.assertIn("min_content_width=200", self.text)


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
