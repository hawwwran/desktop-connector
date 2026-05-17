"""T9.5 — Multi-device migration propagation helpers."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.migration.propagation import (  # noqa: E402
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


class VaultHttpRelayPropagationTests(unittest.TestCase):
    """Review §5.C3: ``VaultHttpRelay.get_header`` must apply the §H2
    propagation decision so a vault migrated by Device A is picked up
    automatically by Devices B…N on their next header fetch instead
    of staying invisible until the user manually edits config."""

    def setUp(self) -> None:
        import base64
        import importlib
        import tempfile

        self._tempdir = Path(tempfile.mkdtemp(prefix="vault_propagation_relay_"))
        # Build a minimal stand-in config: just exposes the
        # server_url and vault_* attributes the propagation path
        # touches, plus a save() that records calls.
        self.config = _StubConfig(server_url=SOURCE)
        # Construct VaultHttpRelay without going through __init__'s
        # network bootstrap: stub the connection by direct attribute
        # assignment.
        from src.vault.binding.runtime import VaultHttpRelay
        relay = VaultHttpRelay.__new__(VaultHttpRelay)
        relay._config = self.config
        relay._conn = _StubConn()
        self.relay = relay
        # Cache the base64 module so the test body can build a
        # response that exercises get_header's b64-decode path.
        self._b64encode = base64.b64encode

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tempdir, ignore_errors=True)

    def _build_header_response(self, migrated_to: str | None) -> dict:
        data = {
            "encrypted_header": self._b64encode(b"\x01envelope-bytes").decode("ascii"),
            "header_revision": 1,
        }
        if migrated_to is not None:
            data["migrated_to"] = migrated_to
        return {"ok": True, "data": data}

    def test_migrated_to_triggers_config_switch_and_persist(self) -> None:
        self.relay._conn.set_response(200, self._build_header_response(TARGET))
        header = self.relay.get_header("ABCD2345WXYZ", "vault-secret")
        self.assertEqual(header["header_revision"], 1)
        # Config rewritten to the new relay; previous_url retained
        # for the §H2 7-day switch-back grace.
        self.assertEqual(self.config.server_url, TARGET)
        self.assertEqual(self.config.vault_previous_relay_url, SOURCE)
        self.assertIsNotNone(self.config.vault_previous_relay_expires_at)
        # Persisted exactly once (idempotent on next get_header).
        self.assertEqual(self.config.save_calls, 1)

    def test_no_migrated_to_leaves_config_untouched(self) -> None:
        self.relay._conn.set_response(200, self._build_header_response(None))
        self.relay.get_header("ABCD2345WXYZ", "vault-secret")
        self.assertEqual(self.config.server_url, SOURCE)
        self.assertIsNone(self.config.vault_previous_relay_url)
        self.assertEqual(self.config.save_calls, 0)

    def test_already_on_target_is_noop(self) -> None:
        self.config.server_url = TARGET
        self.relay._conn.set_response(200, self._build_header_response(TARGET))
        self.relay.get_header("ABCD2345WXYZ", "vault-secret")
        # Already on target — propagation must not bump previous_url
        # or trigger a save.
        self.assertEqual(self.config.server_url, TARGET)
        self.assertIsNone(self.config.vault_previous_relay_url)
        self.assertEqual(self.config.save_calls, 0)


class _StubConfig:
    """Minimal Config surrogate exposing exactly the attributes the
    §5.C3 propagation path touches (server_url, vault_previous_relay_*,
    save). Captures save() call count so the test can assert exact
    persistence semantics."""

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url
        self.vault_previous_relay_url: str | None = None
        self.vault_previous_relay_expires_at: str | None = None
        self.save_calls = 0

    def save(self) -> None:
        self.save_calls += 1


class _StubConn:
    """Mock ConnectionManager that returns the canned ``set_response``
    on every request."""

    def __init__(self) -> None:
        self._status = 200
        self._body: dict = {}

    def set_response(self, status: int, body: dict) -> None:
        self._status = status
        self._body = body

    def request(self, method, path, *, headers=None, json=None):
        return _StubResponse(self._status, self._body)


class _StubResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


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
        pkg = Path(REPO_ROOT, "desktop/src/windows_vault")
        source = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py"))
        )
        for needle in (
            'add_tab("migration", "Migration"',
            "from ..vault.migration.propagation import can_switch_back",
            "Switch back to previous relay",
            "Migrate to another relay",
            "vault_previous_relay_url",
            "vault_previous_relay_expires_at",
        ):
            with self.subTest(text=needle):
                self.assertIn(needle, source)


if __name__ == "__main__":
    unittest.main()
