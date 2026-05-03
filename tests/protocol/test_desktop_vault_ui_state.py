"""T3.3 / T3.5 / T3.6 — Vault UI state-decision tests.

The decision functions for the main-settings button, tray submenu, and
wizard cancel rule are pure transformations from a small input space.
This file exhaustively covers every cell of the §D16 / §A2 tables.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_ui_state import (  # noqa: E402
    should_show_vault_submenu,
    vault_settings_button_state,
    vault_submenu_entries,
    wizard_cancel_rule,
)


class VaultSettingsButtonStateTests(unittest.TestCase):
    """T3.3 — three cells of the §D16 wizard-routing table."""

    def test_off_toggle_disables_button(self) -> None:
        for vault_exists in (False, True):
            with self.subTest(vault_exists=vault_exists):
                state = vault_settings_button_state(
                    toggle_active=False, vault_exists=vault_exists,
                )
                self.assertFalse(state.enabled)
                self.assertEqual(state.action, "disabled")
                self.assertTrue(state.is_disabled)

    def test_on_toggle_no_vault_launches_wizard(self) -> None:
        state = vault_settings_button_state(toggle_active=True, vault_exists=False)
        self.assertTrue(state.enabled)
        self.assertEqual(state.action, "launch_wizard")

    def test_on_toggle_vault_exists_launches_settings(self) -> None:
        state = vault_settings_button_state(toggle_active=True, vault_exists=True)
        self.assertTrue(state.enabled)
        self.assertEqual(state.action, "launch_settings")

    def test_full_truth_table(self) -> None:
        # Property-style: every (toggle, vault) → (action, enabled) cell.
        expected = {
            (False, False): ("disabled", False),
            (False, True):  ("disabled", False),
            (True,  False): ("launch_wizard", True),
            (True,  True):  ("launch_settings", True),
        }
        for (toggle, vault), (expected_action, expected_enabled) in expected.items():
            with self.subTest(toggle=toggle, vault=vault):
                state = vault_settings_button_state(
                    toggle_active=toggle, vault_exists=vault,
                )
                self.assertEqual(state.action, expected_action)
                self.assertEqual(state.enabled, expected_enabled)


class VaultSubmenuTests(unittest.TestCase):
    """T3.5 — tray submenu visibility + contents."""

    def test_submenu_hidden_when_toggle_off(self) -> None:
        self.assertFalse(should_show_vault_submenu(False))
        for vault_exists in (False, True):
            with self.subTest(vault_exists=vault_exists):
                self.assertEqual(
                    vault_submenu_entries(toggle_active=False, vault_exists=vault_exists),
                    [],
                )

    def test_submenu_shows_wizard_entries_when_no_vault(self) -> None:
        self.assertTrue(should_show_vault_submenu(True))
        entries = vault_submenu_entries(toggle_active=True, vault_exists=False)
        self.assertEqual(entries, ["create_vault", "import_vault"])

    def test_submenu_shows_operating_entries_when_vault_exists(self) -> None:
        entries = vault_submenu_entries(toggle_active=True, vault_exists=True)
        self.assertEqual(
            entries,
            ["open_vault", "sync_now", "export", "import", "settings"],
        )


class WizardCancelRuleTests(unittest.TestCase):
    """T3.6 — §A2 wizard cancellation behavior."""

    def test_cancel_with_no_vault_flips_toggle_off(self) -> None:
        self.assertEqual(wizard_cancel_rule(vault_exists=False), "flip_toggle_off")

    def test_cancel_with_existing_vault_does_nothing(self) -> None:
        self.assertEqual(wizard_cancel_rule(vault_exists=True), "no_change")


class ConfigVaultActiveTests(unittest.TestCase):
    """T3.3 — Config.vault_active getter/setter persistence."""

    def setUp(self) -> None:
        from pathlib import Path
        from src.config import Config

        self.tmpdir = tempfile.mkdtemp(prefix="vault_active_test_")
        self.config_dir = Path(self.tmpdir)
        self.Config = Config

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _open_config(self):
        return self.Config(config_dir=self.config_dir)

    def test_default_is_on_for_fresh_install(self) -> None:
        # No config.json yet — defaults to ON per §D16.
        cfg = self._open_config()
        self.assertTrue(cfg.vault_active)

    def test_setter_persists_false(self) -> None:
        cfg = self._open_config()
        cfg.vault_active = False
        self.assertFalse(cfg.vault_active)

        # Survives reload (acceptance criterion: "Toggle survives app restart").
        reopened = self._open_config()
        self.assertFalse(reopened.vault_active)

    def test_setter_persists_true_after_false(self) -> None:
        cfg = self._open_config()
        cfg.vault_active = False
        cfg.vault_active = True
        self.assertTrue(cfg.vault_active)

        reopened = self._open_config()
        self.assertTrue(reopened.vault_active)

    def test_existing_config_without_vault_key_defaults_to_on(self) -> None:
        # Older installs upgrading to vault-aware code.
        import json
        with open(self.config_dir / "config.json", "w") as f:
            json.dump({"theme_mode": "dark"}, f)
        cfg = self._open_config()
        self.assertTrue(cfg.vault_active)


if __name__ == "__main__":
    unittest.main()
