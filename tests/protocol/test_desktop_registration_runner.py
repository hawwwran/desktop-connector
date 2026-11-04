"""Registration recovery tests for desktop bootstrap."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.api_client import DeviceRegistrationResult  # noqa: E402
from src.config import Config  # noqa: E402
from src.runners.registration_runner import register_device  # noqa: E402


class _FakeCrypto:
    def __init__(self) -> None:
        self.reset_count = 0

    def reset_keys(self) -> None:
        self.reset_count += 1


class _FakeApi:
    def __init__(self, results: list[DeviceRegistrationResult | None]) -> None:
        self.crypto = _FakeCrypto()
        self._results = list(results)
        self.server_urls: list[str] = []

    def register_with_status(self, server_url: str) -> DeviceRegistrationResult | None:
        self.server_urls.append(server_url)
        return self._results.pop(0)


class RegistrationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def test_registration_conflict_rotates_identity_and_retries(self) -> None:
        config = Config(self.config_dir)
        config.server_url = "http://relay.example"
        config.device_id = "old-device"
        config.add_paired_device("peer-A", "pk-peer-A", "sk-peer-A", name="Peer A")
        api = _FakeApi([
            DeviceRegistrationResult(409, {"error": "already_registered"}),
            DeviceRegistrationResult(
                201,
                {"device_id": "new-device", "auth_token": "new-token"},
            ),
        ])

        registered = register_device(config, api)  # type: ignore[arg-type]

        self.assertTrue(registered)
        self.assertEqual(api.server_urls, ["http://relay.example", "http://relay.example"])
        self.assertEqual(api.crypto.reset_count, 1)
        self.assertEqual(config.device_id, "new-device")
        self.assertEqual(config.auth_token, "new-token")
        self.assertEqual(config.paired_devices, {})

    def test_device_id_persists_via_secret_store_through_reopen(self) -> None:
        """Regression: the (device_id, auth_token) pair must share
        storage so a kill between writes can't leave a half-state
        where keyring has auth_token but config.json has no device_id.

        Suite 0002 test 04 hit exactly that on 2026-05-06: the wizard
        subprocess loaded a Config whose ``device_id`` was missing
        from JSON (yet keyring still had ``auth_token``) and the
        relay rejected publish with "Desktop Connector is not
        registered with the relay." Fix: device_id now lives in the
        secret store next to auth_token. Reopening the config has
        to round-trip both fields together.
        """
        config = Config(self.config_dir)
        config.device_id = "round-trip-device"
        config.auth_token = "round-trip-token"

        # Drop the in-memory instance and reload from disk.
        del config
        reopened = Config(self.config_dir)
        self.assertEqual(reopened.device_id, "round-trip-device")
        self.assertEqual(reopened.auth_token, "round-trip-token")
        self.assertTrue(reopened.is_registered)

    def test_legacy_device_id_in_config_json_migrates_to_secret_store(self) -> None:
        """A pre-fix install carries ``device_id`` as plaintext in
        ``config.json`` (alongside the legacy plaintext ``auth_token``
        the H.4 migration was already moving). On the first boot
        post-fix, the same migration pass walks ``device_id`` over
        and deletes the JSON copy, just like ``auth_token``.
        """
        # Hand-write a legacy config.json with both secrets in
        # plaintext, the way pre-H.4 / pre-this-fix installs looked.
        legacy_config = {
            "device_id": "legacy-device",
            "auth_token": "legacy-token",
            "server_url": "http://relay.example",
        }
        (self.config_dir / "config.json").write_text(
            json.dumps(legacy_config, indent=2),
        )

        config = Config(self.config_dir)

        # The active store on this host decides whether the
        # migration ran. With the JSON fallback (test runner default
        # via DESKTOP_CONNECTOR_NO_KEYRING=1), values stay where
        # they are at rest but are reachable through the property
        # API. With libsecret, they migrate to keyring and the
        # JSON file no longer holds them. Either way, the property
        # API returns the legacy values and ``is_registered`` is
        # True.
        self.assertEqual(config.device_id, "legacy-device")
        self.assertEqual(config.auth_token, "legacy-token")
        self.assertTrue(config.is_registered)

        if config.is_secret_storage_secure():
            on_disk = json.loads(
                (self.config_dir / "config.json").read_text(),
            )
            self.assertNotIn("device_id", on_disk)
            self.assertNotIn("auth_token", on_disk)
            self.assertIn("secrets_migrated_at", on_disk)

    def test_wipe_credentials_full_clears_both_identity_fields(self) -> None:
        config = Config(self.config_dir)
        config.device_id = "to-be-wiped"
        config.auth_token = "to-be-wiped-token"
        self.assertTrue(config.is_registered)

        config.wipe_credentials("full")

        self.assertIsNone(config.device_id)
        self.assertIsNone(config.auth_token)
        self.assertFalse(config.is_registered)


if __name__ == "__main__":
    unittest.main()
