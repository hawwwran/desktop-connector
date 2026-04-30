"""Validation tests for desktop-to-desktop pairing keys (M.11)."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.crypto import KeyManager  # noqa: E402
from src.devices import ConnectedDeviceRegistry  # noqa: E402
from src.pairing_key import (  # noqa: E402
    AlreadyPairedError,
    PairingKey,
    RelayMismatchError,
    SelfPairError,
    _normalize_server_url,
    validate_for_join,
)


def _peer_pubkey_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
    )

    priv = X25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


class PairingKeyValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        self.config = Config(self.config_dir)
        self.config.server_url = "https://relay.example.com/dc/"
        self.crypto = KeyManager(self.config_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _key(self, **overrides) -> PairingKey:
        defaults = dict(
            server="https://relay.example.com/dc",
            device_id="peer-deadbeef-1234567890ab",
            pubkey=_peer_pubkey_b64(),
            name="Other Desktop",
        )
        defaults.update(overrides)
        return PairingKey(**defaults)

    def test_self_pair_refused(self) -> None:
        key = self._key(device_id=self.crypto.get_device_id())
        with self.assertRaises(SelfPairError):
            validate_for_join(key, config=self.config, crypto=self.crypto)

    def test_relay_mismatch_refused_on_different_host(self) -> None:
        key = self._key(server="https://elsewhere.example.com/dc")
        with self.assertRaises(RelayMismatchError) as ctx:
            validate_for_join(key, config=self.config, crypto=self.crypto)
        # Mismatch error carries normalized URLs for diagnostics use.
        self.assertIn("relay.example.com", ctx.exception.local)
        self.assertIn("elsewhere.example.com", ctx.exception.remote)

    def test_relay_mismatch_refused_on_different_path(self) -> None:
        key = self._key(server="https://relay.example.com/other")
        with self.assertRaises(RelayMismatchError):
            validate_for_join(key, config=self.config, crypto=self.crypto)

    def test_relay_match_tolerates_trailing_slash(self) -> None:
        # config has trailing slash; key omits it. Should pass.
        self.config.server_url = "https://relay.example.com/dc/"
        key = self._key(server="https://relay.example.com/dc")
        validate_for_join(key, config=self.config, crypto=self.crypto)

    def test_relay_match_is_case_insensitive_for_scheme_and_host(self) -> None:
        self.config.server_url = "HTTPS://Relay.Example.COM/dc"
        key = self._key(server="https://relay.example.com/dc")
        validate_for_join(key, config=self.config, crypto=self.crypto)

    def test_already_paired_refused(self) -> None:
        # Pre-add the device so the registry sees it.
        peer_id = "peer-deadbeef-1234567890ab"
        self.config.add_paired_device(
            peer_id, _peer_pubkey_b64(), "AAAA" * 8, name="Workstation",
        )
        key = self._key(device_id=peer_id)
        with self.assertRaises(AlreadyPairedError) as ctx:
            validate_for_join(key, config=self.config, crypto=self.crypto)
        self.assertEqual(ctx.exception.device_id, peer_id)
        self.assertEqual(ctx.exception.name, "Workstation")

    def test_happy_path_returns_none(self) -> None:
        key = self._key()
        self.assertIsNone(
            validate_for_join(key, config=self.config, crypto=self.crypto)
        )

    def test_explicit_registry_argument_is_used(self) -> None:
        # Pass a registry that has the device pre-baked, even though
        # config.paired_devices is empty. Validate must consult the
        # injected registry.
        self.assertEqual(self.config.paired_devices, {})
        registry = ConnectedDeviceRegistry(self.config)
        # Add via registry's underlying config so the membership check
        # via registry.get works.
        peer_id = "peer-injected-id-0000000000ab"
        self.config.add_paired_device(
            peer_id, _peer_pubkey_b64(), "BBBB" * 8, name="Existing",
        )
        key = self._key(device_id=peer_id)
        with self.assertRaises(AlreadyPairedError):
            validate_for_join(
                key, config=self.config, crypto=self.crypto,
                registry=registry,
            )


class NormalizeServerUrlTests(unittest.TestCase):
    def test_strips_trailing_slash(self) -> None:
        self.assertEqual(
            _normalize_server_url("https://x.example.com/"),
            "https://x.example.com",
        )

    def test_lowercases_scheme_and_host(self) -> None:
        self.assertEqual(
            _normalize_server_url("HTTPS://X.example.COM/path"),
            "https://x.example.com/path",
        )

    def test_strips_query_and_fragment(self) -> None:
        # URL fragments/queries are not part of the relay endpoint
        # contract — strip them.
        self.assertEqual(
            _normalize_server_url("https://x/y?z=1#frag"),
            "https://x/y",
        )


if __name__ == "__main__":
    unittest.main()
