"""Pairing naming + active-marking behaviour for M.5."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.crypto import KeyManager  # noqa: E402
from src.devices import ConnectedDeviceRegistry  # noqa: E402
from src.pairing import run_pairing_headless  # noqa: E402


def _make_peer_pubkey_b64() -> str:
    """Generate a fresh X25519 public key the desktop can derive against."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
    )
    from cryptography.hazmat.primitives import serialization

    priv = X25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


class _FakeApi:
    def __init__(self, requests: list[dict]) -> None:
        self._requests = requests
        self.confirmed: list[str] = []

    def poll_pairing(self) -> list[dict]:
        if self._requests:
            return [self._requests.pop(0)]
        return []

    def confirm_pairing(self, phone_id: str) -> bool:
        self.confirmed.append(phone_id)
        return True


class HeadlessPairingNamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        self.config = Config(self.config_dir)
        self.crypto = KeyManager(self.config_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_pair_uses_default_name_and_marks_active(self) -> None:
        peer_pubkey = _make_peer_pubkey_b64()
        peer_id = "peer-aaa"
        api = _FakeApi([{"phone_id": peer_id, "phone_pubkey": peer_pubkey}])

        with unittest.mock.patch("time.sleep", lambda *_: None):
            ok = run_pairing_headless(self.config, self.crypto, api, timeout=5)

        self.assertTrue(ok)
        self.assertEqual(api.confirmed, [peer_id])
        self.assertIn(peer_id, self.config.paired_devices)
        self.assertEqual(
            self.config.paired_devices[peer_id]["name"], "Device 1"
        )
        self.assertEqual(self.config.active_device_id, peer_id)

    def test_second_pair_default_increments(self) -> None:
        # Existing pair already named "Device 1"
        existing_pubkey = _make_peer_pubkey_b64()
        self.config.add_paired_device(
            device_id="existing-peer",
            pubkey=existing_pubkey,
            symmetric_key_b64=base64.b64encode(b"k" * 32).decode(),
            name="Device 1",
        )

        peer_pubkey = _make_peer_pubkey_b64()
        peer_id = "peer-bbb"
        api = _FakeApi([{"phone_id": peer_id, "phone_pubkey": peer_pubkey}])

        with unittest.mock.patch("time.sleep", lambda *_: None):
            ok = run_pairing_headless(self.config, self.crypto, api, timeout=5)

        self.assertTrue(ok)
        self.assertEqual(
            self.config.paired_devices[peer_id]["name"], "Device 2"
        )
        # Newly paired device should be marked active, replacing any
        # prior active id.
        self.assertEqual(self.config.active_device_id, peer_id)

    def test_default_name_skips_taken_slot(self) -> None:
        # Existing pair named "Device 2" — paired-count + 1 == 2 is taken
        existing_pubkey = _make_peer_pubkey_b64()
        self.config.add_paired_device(
            device_id="existing-peer",
            pubkey=existing_pubkey,
            symmetric_key_b64=base64.b64encode(b"k" * 32).decode(),
            name="Device 2",
        )

        peer_pubkey = _make_peer_pubkey_b64()
        peer_id = "peer-ccc"
        api = _FakeApi([{"phone_id": peer_id, "phone_pubkey": peer_pubkey}])

        with unittest.mock.patch("time.sleep", lambda *_: None):
            ok = run_pairing_headless(self.config, self.crypto, api, timeout=5)

        self.assertTrue(ok)
        self.assertEqual(
            self.config.paired_devices[peer_id]["name"], "Device 3"
        )

    def test_registry_rename_and_unpair_align_with_pair_list(self) -> None:
        # Settings rename + unpair go through the same registry; pin
        # the contract M.5 relies on.
        existing_pubkey = _make_peer_pubkey_b64()
        self.config.add_paired_device(
            device_id="dev-A",
            pubkey=existing_pubkey,
            symmetric_key_b64=base64.b64encode(b"k" * 32).decode(),
            name="Device 1",
        )
        self.config.add_paired_device(
            device_id="dev-B",
            pubkey=_make_peer_pubkey_b64(),
            symmetric_key_b64=base64.b64encode(b"k" * 32).decode(),
            name="Device 2",
        )
        self.config.active_device_id = "dev-B"

        registry = ConnectedDeviceRegistry(self.config)

        renamed = registry.rename("dev-A", "Workstation")
        self.assertEqual(renamed.name, "Workstation")
        self.assertEqual(
            self.config.paired_devices["dev-A"]["name"], "Workstation"
        )

        # Unpairing the active device clears active_device_id; the
        # other pair stays.
        registry.unpair("dev-B")
        self.assertNotIn("dev-B", self.config.paired_devices)
        self.assertIn("dev-A", self.config.paired_devices)
        self.assertIsNone(self.config.active_device_id)


if __name__ == "__main__":
    unittest.main()
