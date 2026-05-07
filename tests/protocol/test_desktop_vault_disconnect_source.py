"""Source checks for the Vault settings disconnect action.

Source-pin file (one of five). See
``test_desktop_vault_browser_source`` for the policy: these greppers
catch UI-string regressions only — disconnect-flow correctness is
covered by ``test_desktop_vault_disconnect`` and the
``vault_grant`` unit tests, not here.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _vault_main_source() -> str:
    """Concatenate every module under ``windows_vault/`` *except* the
    onboarding wizard and the passphrase generator. The post-split
    layout spreads the recovery / danger / activity / etc. tabs across
    sibling tab modules; tests that used to grep one giant file now
    grep the concatenation of those siblings.
    """
    pkg = Path(REPO_ROOT, "desktop/src/windows_vault")
    parts = []
    for path in sorted(pkg.glob("*.py")):
        if path.name in ("onboard_window.py", "passphrase_generator.py"):
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _vault_onboard_source() -> str:
    return Path(
        REPO_ROOT, "desktop/src/windows_vault/onboard_window.py",
    ).read_text(encoding="utf-8")


def _vault_runtime_source() -> str:
    return Path(REPO_ROOT, "desktop/src/vault_runtime.py").read_text()


class VaultDisconnectSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _vault_main_source()

    def test_danger_zone_has_disconnect_confirmation_copy(self) -> None:
        for text in (
            'danger.append(Gtk.Label(label="Disconnect vault"',
            'disconnect_btn = Gtk.Button(label="Disconnect vault"',
            'heading="Disconnect vault?"',
            # F-U18: copy now spells out the local-data wipe.
            "Removes all local vault material",
            "The relay vault is untouched",
            'dlg.add_response("disconnect", "Disconnect vault")',
            "Adw.ResponseAppearance.DESTRUCTIVE",
            "disconnect_local_vault(config)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_recovery_test_button_opens_real_modal(self) -> None:
        # F-U17: dialog migrated from ``Adw.ApplicationWindow`` to
        # ``Adw.Dialog``. The new shape floats over the vault-settings
        # window, auto-handles transient ownership, and auto-closes
        # when the parent closes.
        for text in (
            'test_recovery_btn = Gtk.Button(label="Test recovery now"',
            'test_recovery_btn.connect("clicked", open_recovery_test_dialog)',
            "dialog = Adw.Dialog()",
            'dialog.set_title("Test recovery")',
            "dialog.set_content_width(560)",
            "dialog.set_content_height(420)",
            "dialog.set_child(extra)",
            "dialog.present(win)",
            "Gtk.FileDialog()",
            "Gtk.PasswordEntry",
            "wipe_switch = Gtk.Switch",
            "run_recovery_material_test(",
            "recovery_envelope_meta_from_json",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_recovery_dialog_no_longer_uses_application_window(self) -> None:
        """F-U17 anti-regression: the recovery dialog must not be
        rebuilt as an Adw.ApplicationWindow. The other three uses of
        Adw.ApplicationWindow in this file (vault settings, wizard,
        passphrase generator) are real top-level windows; the recovery
        tester is a child dialog and belongs to its parent's lifecycle."""
        marker = "def open_recovery_test_dialog(_btn):"
        idx = self.source.index(marker)
        # End at the test_recovery_btn.connect line that follows the
        # inner function — stable boundary at the same outer indent.
        end_marker = 'test_recovery_btn.connect("clicked", open_recovery_test_dialog)'
        end = self.source.index(end_marker, idx)
        body = self.source[idx:end]
        # Strip comment lines (and the comment portion of mixed lines)
        # so a future "F-U17 replaced Adw.ApplicationWindow…" comment
        # explaining the migration doesn't trip the assertion.
        code_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "  # " in line:
                line = line.split("  # ", 1)[0]
            code_lines.append(line)
        code = "\n".join(code_lines)
        self.assertNotIn(
            "Adw.ApplicationWindow(",
            code,
            msg="F-U17: recovery test dialog regressed to Adw.ApplicationWindow",
        )
        # And the FileDialog must use the parent vault-settings window,
        # not the dialog (Adw.Dialog isn't a Gtk.Window).
        self.assertIn(
            "file_dialog.open(parent=win, callback=on_file_chosen)",
            code,
        )
        self.assertNotIn(
            "file_dialog.open(parent=dialog,",
            code,
        )

    def test_update_recovery_material_is_not_left_as_a_dead_button(self) -> None:
        for text in (
            'update_recovery_btn = Gtk.Button(label="Update recovery material"',
            "update_recovery_btn.set_sensitive(False)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_recovery_test_refreshes_open_settings_window(self) -> None:
        for text in (
            # The "untested / stale" gate decides when the orange
            # recovery-warning banner shows; the vault-presence gate
            # added 2026-05-06 suppresses the banner on the empty
            # state where there's nothing to recover.
            "bool(vault_id_undashed)",
            'recovery_status_text in ("Untested", "Stale")',
            "def refresh_recovery_summary(status: str, last_tested: str | None = None) -> None:",
            'status in ("Untested", "Stale")',
            'config._data["vault"]["recovery_last_tested"] = now',
            'refresh_recovery_summary("Verified", now)',
            'refresh_recovery_summary("Failed", now)',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_wizard_persists_recovery_test_metadata(self) -> None:
        source = _vault_onboard_source()
        for text in (
            "recovery_envelope_meta_to_json",
            'config._data["vault"]["recovery_envelope_meta"]',
            "recovery_envelope_meta=state[\"recovery_envelope_meta\"]",
        ):
            self.assertIn(text, source, msg=f"missing: {text!r}")

    def test_wizard_creates_vault_on_real_relay(self) -> None:
        source = _vault_onboard_source()
        runtime_source = _vault_runtime_source()
        for text in (
            "relay = create_vault_relay(config)",
            "save_local_vault_grant(config_dir, config, vault)",
        ):
            self.assertIn(text, source, msg=f"missing: {text!r}")
        for text in (
            "def create_vault_relay(config):",
            'os.environ.get("DESKTOP_CONNECTOR_VAULT_LOCAL_RELAY") == "1"',
            "class VaultHttpRelay:",
            "class VaultLocalDevelopmentRelay:",
            'self._conn.request("POST", "/api/vaults", json=payload)',
            '"vault_access_token_hash": base64.b64encode(vault_access_token_hash).decode("ascii")',
            '"encrypted_header": base64.b64encode(encrypted_header).decode("ascii")',
            '"initial_manifest_ciphertext": base64.b64encode(initial_manifest_ciphertext).decode("ascii")',
        ):
            self.assertIn(text, runtime_source, msg=f"missing: {text!r}")
        self.assertNotIn("_BarebonesRelay", runtime_source)


if __name__ == "__main__":
    unittest.main()
