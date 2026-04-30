"""Functional join-flow tests for desktop-to-desktop pairing (M.11)."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.crypto import KeyManager  # noqa: E402
from src.devices import ConnectedDeviceRegistry  # noqa: E402
from src.pairing_key import (  # noqa: E402
    JoinRequestError,
    PairingKey,
    begin_join,
    build_local_key,
    complete_join,
    decode,
    encode,
    validate_for_join,
)


class _Inviter:
    """Minimal stand-in for the inviter's KeyManager.

    Tracks its private key so we can verify the joiner's derived
    symkey matches what the inviter would compute on its side using
    the joiner's pubkey.
    """

    def __init__(self, config_dir: Path) -> None:
        self.crypto = KeyManager(config_dir)


class JoinFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)

        self.joiner_dir = root / "joiner"
        self.joiner_dir.mkdir()
        self.config = Config(self.joiner_dir)
        self.config.server_url = "https://relay.example.com/dc"
        self.crypto = KeyManager(self.joiner_dir)

        self.inviter_dir = root / "inviter"
        self.inviter_dir.mkdir()
        inviter_config = Config(self.inviter_dir)
        inviter_config.server_url = "https://relay.example.com/dc"
        inviter_config._data["device_name"] = "Inviter Workstation"
        inviter_config.save()
        self.inviter_config = inviter_config
        self.inviter = _Inviter(self.inviter_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_inviter_key_text(self) -> str:
        key = build_local_key(self.inviter_config, self.inviter.crypto)
        return encode(key)

    def test_full_round_trip_persists_pair_and_matches_inviter_symkey(self) -> None:
        text = self._build_inviter_key_text()

        # Joiner side: parse + validate.
        key = decode(text)
        validate_for_join(key, config=self.config, crypto=self.crypto)

        # Joiner side: send pairing request via injected fake.
        sent: list[tuple] = []

        def send_pairing_request(target_device_id: str, requester_pubkey: str) -> bool:
            sent.append((target_device_id, requester_pubkey))
            return True

        handshake = begin_join(
            key, crypto=self.crypto, send_pairing_request=send_pairing_request,
        )

        # send_pairing_request was called exactly once with the right args.
        self.assertEqual(len(sent), 1)
        target_id, requester_pubkey = sent[0]
        self.assertEqual(target_id, self.inviter.crypto.get_device_id())
        self.assertEqual(requester_pubkey, self.crypto.get_public_key_b64())

        # Verification code is a 6-digit XXX-XXX string.
        self.assertRegex(handshake.verification_code, r"^\d{3}-\d{3}$")
        # Length sanity: 32-byte AES key.
        self.assertEqual(len(handshake.shared_key), 32)

        # Critically: the joiner's derived symkey must equal what the
        # INVITER would derive using the JOINER's pubkey. ECDH symmetry.
        inviter_derived = self.inviter.crypto.derive_shared_key(
            self.crypto.get_public_key_b64(),
        )
        self.assertEqual(handshake.shared_key, inviter_derived)

        # Persist.
        synced: list[bool] = []
        complete_join(
            handshake, config=self.config, name="Office Desktop",
            on_synced=lambda: synced.append(True),
        )

        # Pairing landed in config.paired_devices with the chosen name.
        paired = self.config.paired_devices
        self.assertIn(key.device_id, paired)
        self.assertEqual(paired[key.device_id]["name"], "Office Desktop")
        self.assertEqual(
            paired[key.device_id]["pubkey"],
            self.inviter.crypto.get_public_key_b64(),
        )

        # active_device_id flipped to the new pair (D2: paired action).
        self.assertEqual(self.config.active_device_id, key.device_id)

        # on_synced callback fired so file-manager sync runs after save.
        self.assertEqual(synced, [True])

        # Persisted symkey round-trips back to the same shared bytes.
        b64 = paired[key.device_id]["symmetric_key_b64"]
        self.assertEqual(base64.b64decode(b64), handshake.shared_key)

    def test_begin_join_raises_on_send_failure(self) -> None:
        key = decode(self._build_inviter_key_text())

        def send_pairing_request(*_args, **_kwargs) -> bool:
            return False

        with self.assertRaises(JoinRequestError):
            begin_join(
                key, crypto=self.crypto,
                send_pairing_request=send_pairing_request,
            )

    def test_complete_join_swallows_on_synced_exceptions(self) -> None:
        # File-manager sync failures should not back-propagate and
        # leave the user with a half-saved pair. The pair is already
        # persisted; the sync is best-effort.
        key = decode(self._build_inviter_key_text())
        validate_for_join(key, config=self.config, crypto=self.crypto)

        handshake = begin_join(
            key, crypto=self.crypto,
            send_pairing_request=lambda *_a, **_k: True,
        )

        def boom():
            raise RuntimeError("file system gone")

        # Must not raise.
        complete_join(
            handshake, config=self.config, name="Office Desktop",
            on_synced=boom,
        )
        self.assertIn(key.device_id, self.config.paired_devices)


class BuildLocalKeyTests(unittest.TestCase):
    def test_local_key_round_trips_through_encode_decode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            config = Config(config_dir)
            config.server_url = "https://relay.example.com/dc"
            config._data["device_name"] = "Studio"
            config.save()
            crypto = KeyManager(config_dir)

            key = build_local_key(config, crypto)
            text = encode(key)
            decoded = decode(text)

            self.assertEqual(decoded, key)
            self.assertEqual(decoded.device_id, crypto.get_device_id())
            self.assertEqual(decoded.pubkey, crypto.get_public_key_b64())
            self.assertEqual(decoded.name, "Studio")
            self.assertEqual(decoded.server, config.server_url)


if __name__ == "__main__":
    unittest.main()
