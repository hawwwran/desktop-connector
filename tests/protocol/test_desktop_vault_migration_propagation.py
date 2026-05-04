"""T9.5 — Multi-device migration propagation helpers."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_migration_propagation import (  # noqa: E402
    PropagationDecision,
    can_switch_back,
    propagate_relay_migration,
)


SOURCE = "https://source.example.com/SERVICES/dc"
TARGET = "https://target.example.com/SERVICES/dc"
NOW = "2026-05-04T12:00:00.000Z"


class PropagateRelayMigrationTests(unittest.TestCase):
    def test_no_migrated_to_means_no_switch(self) -> None:
        decision = propagate_relay_migration(
            header_data={"migrated_to": None},
            current_relay_url=SOURCE,
            now=NOW,
        )
        self.assertFalse(decision.should_switch)

    def test_already_on_target_is_noop(self) -> None:
        decision = propagate_relay_migration(
            header_data={"migrated_to": TARGET},
            current_relay_url=TARGET,
            now=NOW,
        )
        self.assertFalse(decision.should_switch)
        self.assertEqual(decision.reason, "already_on_target")

    def test_migrated_to_set_triggers_switch_with_seven_day_grace(self) -> None:
        """T9.5 acceptance: Other devices receive on next GET /header,
        switch active relay, save previous_relay_url for 7 days."""
        decision = propagate_relay_migration(
            header_data={"migrated_to": TARGET},
            current_relay_url=SOURCE,
            now=NOW,
        )
        self.assertTrue(decision.should_switch)
        self.assertEqual(decision.new_relay_url, TARGET)
        self.assertEqual(decision.previous_relay_url, SOURCE)
        # Expiry is now + 7 days.
        self.assertEqual(
            decision.previous_relay_expires_at,
            "2026-05-11T12:00:00.000Z",
        )


class CanSwitchBackTests(unittest.TestCase):
    def test_no_previous_url_means_no_switch_back(self) -> None:
        self.assertFalse(can_switch_back(
            previous_relay_url=None,
            previous_relay_expires_at="2099-01-01T00:00:00.000Z",
        ))
        self.assertFalse(can_switch_back(
            previous_relay_url="",
            previous_relay_expires_at="2099-01-01T00:00:00.000Z",
        ))

    def test_within_grace_window_allows_switch_back(self) -> None:
        self.assertTrue(can_switch_back(
            previous_relay_url=SOURCE,
            previous_relay_expires_at="2026-05-11T12:00:00.000Z",
            now=NOW,
        ))

    def test_after_grace_window_disallows_switch_back(self) -> None:
        self.assertFalse(can_switch_back(
            previous_relay_url=SOURCE,
            previous_relay_expires_at="2026-05-04T11:59:59.000Z",
            now=NOW,
        ))

    def test_unparseable_expiry_disallows_switch_back(self) -> None:
        self.assertFalse(can_switch_back(
            previous_relay_url=SOURCE,
            previous_relay_expires_at="not a date",
            now=NOW,
        ))


class VaultSettingsMigrationTabSourceTests(unittest.TestCase):
    """T9.6 source-pin: settings UI exposes the Migration tab + switch-back."""

    def test_migration_tab_renders_current_relay_and_switch_back(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault.py").read_text(
            encoding="utf-8"
        )
        for needle in (
            'add_tab("migration", "Migration"',
            "from .vault_migration_propagation import can_switch_back",
            "Switch back to previous relay",
            "Migrate to another relay",
            "vault_previous_relay_url",
            "vault_previous_relay_expires_at",
        ):
            with self.subTest(text=needle):
                self.assertIn(needle, source)


if __name__ == "__main__":
    unittest.main()
