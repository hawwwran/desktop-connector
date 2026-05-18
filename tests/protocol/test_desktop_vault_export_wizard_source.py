"""§6.H3 — source-pin tests for the export wizard.

The data layer (write_export_bundle + read_export_bundle) is covered
end-to-end by ``test_desktop_vault_export.py``; this file pins that
the GTK subprocess actually wires the gating (passphrase length +
match, verify-default-on, shred confirmation, atomic-rename safety)
into visible widgets.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()


SRC_ROOT = Path(
    os.path.dirname(__file__) or "."
).resolve().parent.parent / "desktop" / "src"


class ExportWizardSubprocessSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault_export.py").read_text(encoding="utf-8")
        cls.windows_text = (SRC_ROOT / "windows.py").read_text(encoding="utf-8")

    def test_dispatcher_registers_vault_export(self) -> None:
        self.assertIn('"vault-export"', self.windows_text)
        self.assertIn(
            "from .windows_vault_export import show_vault_export",
            self.windows_text,
        )
        self.assertIn('args.window == "vault-export"', self.windows_text)
        self.assertIn("show_vault_export(config_dir)", self.windows_text)

    def test_passphrase_length_and_match_gate_continue(self) -> None:
        """Continue stays disabled until passphrase length ≥
        ``EXPORT_PASSPHRASE_MIN_LEN`` AND the confirm field matches.
        The data layer enforces the 8-char floor at the API; the
        wizard pre-validates so users don't waste an Argon2id
        derivation on a too-short string."""
        self.assertIn("EXPORT_PASSPHRASE_MIN_LEN", self.text)
        self.assertIn("pp == cf", self.text)
        self.assertIn("setup_continue.set_sensitive(False)", self.text)

    def test_passphrase_strength_hint_nudges_to_16_chars(self) -> None:
        """The strength hint flips from neutral → success once the
        passphrase reaches the recommended 16-character mark, even
        though the data-layer floor is 8."""
        self.assertIn("EXPORT_PASSPHRASE_RECOMMENDED_LEN", self.text)
        self.assertIn("16+ is recommended", self.text)

    def test_verify_default_on(self) -> None:
        """The verify-after-write switch defaults to ``active=True``;
        the post-write worker calls ``read_export_bundle`` against
        the just-written file to surface any disk-corruption class
        of error before the user is told it's safe to delete the
        original."""
        self.assertIn("active=True", self.text)
        self.assertIn("read_export_bundle", self.text)
        self.assertIn("verify_default_on", self.text)

    def test_uses_write_export_bundle_with_progress_callback(self) -> None:
        """The wizard streams progress callbacks back to the GTK
        main loop via ``GLib.idle_add`` — the engine runs in a
        worker thread, the UI updates from the main loop only."""
        self.assertIn("from .vault.export.bundle import", self.text)
        self.assertIn("write_export_bundle", self.text)
        self.assertIn("ExportProgress", self.text)
        self.assertIn("GLib.idle_add", self.text)

    def test_shred_action_requires_explicit_confirmation(self) -> None:
        """Shred is a destructive irreversible action — must surface
        a visible loss warning + a confirmation dialog before
        deleting the bundle."""
        self.assertIn("Shred bundle", self.text)
        self.assertIn("Shred bundle from this disk?", self.text)
        self.assertIn("ResponseAppearance.DESTRUCTIVE", self.text)
        self.assertIn("shred_file", self.text)

    def test_emits_diagnostics_events(self) -> None:
        """Audit anchors for the wizard lifecycle."""
        self.assertIn("vault.export.started", self.text)
        self.assertIn("vault.export.completed", self.text)
        self.assertIn("vault.export.verified", self.text)
        self.assertIn("vault.export.shredded", self.text)

    def test_failed_export_surfaces_atomic_rename_reassurance(self) -> None:
        """The bundle writer uses an atomic-rename pattern, so a
        failed run leaves no partial ``.dcvault`` file. The error
        page reaches that branch when the worker raises; pin the
        log + path so a refactor can't accidentally drop the
        ``vault.export.failed`` audit anchor."""
        self.assertIn("vault.export.failed", self.text)


class TraySubmenuExportEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tray_text = (SRC_ROOT / "tray" / "vault_submenu.py").read_text(encoding="utf-8")
        cls.ui_state_text = (SRC_ROOT / "vault" / "ui" / "ui_state.py").read_text(encoding="utf-8")

    def test_export_token_in_operating_branch(self) -> None:
        """``export`` joins the operating tokens (vault exists +
        unlocked locally) so the submenu offers the wizard."""
        self.assertIn('"export"', self.ui_state_text)
        self.assertIn(
            '"open_vault", "sync_now", "import", "export", "settings"',
            self.ui_state_text,
        )

    def test_tray_menu_entry_wires_subprocess(self) -> None:
        self.assertIn('"Export…"', self.tray_text)
        self.assertIn("_spawn_vault_export", self.tray_text)
        self.assertIn('"vault-export"', self.tray_text)


class RecoveryTabExportButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault" / "tab_recovery.py").read_text(encoding="utf-8")

    def test_recovery_tab_has_export_button(self) -> None:
        """Recovery tab is the natural in-Settings entry alongside
        "Test recovery" / "Update recovery material"; the wizard is
        the same one the tray's "Export…" menu item opens."""
        self.assertIn('label="Export vault…"', self.text)
        self.assertIn("export_bundle_btn", self.text)
        self.assertIn('"vault-export"', self.text)


if __name__ == "__main__":
    unittest.main()
