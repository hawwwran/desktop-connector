"""Registration recovery tests for desktop bootstrap."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
