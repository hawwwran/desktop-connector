"""Source checks for the Vault settings disconnect action."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _vault_main_source() -> str:
    source = Path(REPO_ROOT, "desktop/src/windows_vault.py").read_text()
    start = source.index("def show_vault_main(")
    end = source.index("def show_vault_onboard(", start)
    return source[start:end]


def _vault_onboard_source() -> str:
    source = Path(REPO_ROOT, "desktop/src/windows_vault.py").read_text()
    start = source.index("def show_vault_onboard(")
    end = source.index("def show_vault_passphrase_generator(", start)
    return source[start:end]


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
        for text in (
            'test_recovery_btn = Gtk.Button(label="Test recovery now"',
            'test_recovery_btn.connect("clicked", open_recovery_test_dialog)',
            'title="Test recovery"',
            "Adw.ApplicationWindow(",
            "Gtk.FileDialog()",
            "Gtk.PasswordEntry",
            "wipe_switch = Gtk.Switch",
            "run_recovery_material_test(",
            "recovery_envelope_meta_from_json",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_update_recovery_material_is_not_left_as_a_dead_button(self) -> None:
        for text in (
            'update_recovery_btn = Gtk.Button(label="Update recovery material"',
            "update_recovery_btn.set_sensitive(False)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_recovery_test_refreshes_open_settings_window(self) -> None:
        for text in (
            "recovery_warning.set_visible(recovery_status_text in (\"Untested\", \"Stale\"))",
            "def refresh_recovery_summary(status: str, last_tested: str | None = None) -> None:",
            "recovery_warning.set_visible(status in (\"Untested\", \"Stale\"))",
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
