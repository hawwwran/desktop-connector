"""Poller fasttrack consumer tests for M.8."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.crypto import KeyManager  # noqa: E402
from src.find_device_responder import (  # noqa: E402
    NoopAlert,
    _sync_initial_tick_runner,
)
from src.history import TransferHistory  # noqa: E402
from src.interfaces.location import NullLocationProvider  # noqa: E402
from src.messaging import DeviceMessage, MessageTransport, MessageType  # noqa: E402
from src.poller import Poller  # noqa: E402


def _key() -> bytes:
    return b"\x00" * 32


def _key_b64() -> str:
    return base64.b64encode(_key()).decode()


def _make_poller(tmp: Path, *, paired: dict[str, dict] | None = None) -> tuple[Poller, MagicMock, KeyManager]:
    config_dir = tmp / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = Config(config_dir)
    crypto = KeyManager(config_dir)

    if paired:
        for device_id, info in paired.items():
            config.add_paired_device(
                device_id,
                info.get("pubkey", f"pk-{device_id}"),
                info.get("symmetric_key_b64", _key_b64()),
                name=info.get("name", device_id),
            )

    api = MagicMock()
    api.fasttrack_pending = MagicMock(return_value=[])
    api.fasttrack_send = MagicMock(return_value=42)
    api.fasttrack_ack = MagicMock(return_value=True)

    history = TransferHistory(config_dir)
    platform = MagicMock()
    # Real NullLocationProvider — MagicMock would auto-vivify a truthy
    # provider that returns MagicMock fixes, breaking JSON encoding in
    # the heartbeat path.
    platform.location = NullLocationProvider()
    conn = MagicMock()

    # Synchronous initial-tick runner so the responder's first
    # heartbeat is observable on the calling thread (production wires
    # a daemon thread to keep the MessageDispatcher non-blocking).
    poller = Poller(
        config, conn, api, crypto, history, platform,
        initial_tick_runner=_sync_initial_tick_runner,
    )
    return poller, api, crypto


def _make_pending(crypto: KeyManager, *, sender_id: str, payload: dict, msg_id: int = 1) -> dict:
    plaintext = json.dumps(payload).encode()
    encrypted = crypto.encrypt_blob(plaintext, _key())
    return {
        "id": msg_id,
        "sender_id": sender_id,
        "encrypted_data": base64.b64encode(encrypted).decode(),
    }


class FasttrackConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def test_inbound_start_dispatches_to_responder_and_acks(self) -> None:
        poller, api, crypto = _make_poller(
            self.tmp,
            paired={"peer-A": {"name": "Peer A"}},
        )
        # Replace the responder with a captured mock so we can assert
        # the dispatch path without spawning a real alert subprocess.
        captured = MagicMock()
        captured.handle_message = MagicMock()
        poller._find_device_responder = captured
        poller._message_dispatcher.register(
            MessageType.FIND_PHONE_START, captured.handle_message,
        )
        poller._message_dispatcher.register(
            MessageType.FIND_PHONE_STOP, captured.handle_message,
        )

        msg = _make_pending(
            crypto,
            sender_id="peer-A",
            payload={"fn": "find-phone", "action": "start", "volume": 80},
        )
        api.fasttrack_pending.return_value = [msg]

        poller._process_fasttrack_pending()

        captured.handle_message.assert_called_once()
        device_msg = captured.handle_message.call_args[0][0]
        self.assertEqual(device_msg.type, MessageType.FIND_PHONE_START)
        self.assertEqual(device_msg.sender_id, "peer-A")
        api.fasttrack_ack.assert_called_with(1)

    def test_unknown_sender_is_dropped_and_acked(self) -> None:
        poller, api, _crypto = _make_poller(self.tmp, paired={})
        api.fasttrack_pending.return_value = [{
            "id": 7,
            "sender_id": "peer-Z",
            "encrypted_data": base64.b64encode(b"junk").decode(),
        }]

        poller._process_fasttrack_pending()

        api.fasttrack_ack.assert_called_with(7)

    def test_decrypt_failure_is_dropped_and_acked(self) -> None:
        poller, api, _crypto = _make_poller(
            self.tmp, paired={"peer-A": {}},
        )
        api.fasttrack_pending.return_value = [{
            "id": 8,
            "sender_id": "peer-A",
            "encrypted_data": base64.b64encode(b"\x00" * 32).decode(),
        }]

        poller._process_fasttrack_pending()

        api.fasttrack_ack.assert_called_with(8)

    def test_send_update_encrypts_and_uses_recipient_symkey(self) -> None:
        poller, api, crypto = _make_poller(
            self.tmp, paired={"peer-A": {}},
        )

        ok = poller._send_find_device_update("peer-A", "ringing")

        self.assertTrue(ok)
        api.fasttrack_send.assert_called_once()
        recipient, encrypted_b64 = api.fasttrack_send.call_args[0]
        self.assertEqual(recipient, "peer-A")
        ciphertext = base64.b64decode(encrypted_b64)
        plaintext = crypto.decrypt_blob(ciphertext, _key())
        self.assertEqual(
            json.loads(plaintext),
            {"fn": "find-phone", "state": "ringing"},
        )

    def test_send_update_with_coordinates_serializes_them(self) -> None:
        poller, api, crypto = _make_poller(
            self.tmp, paired={"peer-A": {}},
        )

        poller._send_find_device_update(
            "peer-A", "ringing", lat=50.1, lng=14.4, accuracy=12.5,
        )

        _recipient, encrypted_b64 = api.fasttrack_send.call_args[0]
        ciphertext = base64.b64decode(encrypted_b64)
        payload = json.loads(crypto.decrypt_blob(ciphertext, _key()))
        self.assertEqual(payload["lat"], 50.1)
        self.assertEqual(payload["lng"], 14.4)
        self.assertEqual(payload["accuracy"], 12.5)

    def test_send_update_returns_false_for_unpaired_recipient(self) -> None:
        poller, api, _crypto = _make_poller(self.tmp, paired={})

        ok = poller._send_find_device_update("peer-Z", "ringing")

        self.assertFalse(ok)
        api.fasttrack_send.assert_not_called()

    def test_inbound_start_marks_active_device(self) -> None:
        poller, api, crypto = _make_poller(
            self.tmp,
            paired={"peer-A": {"name": "Peer A"}},
        )
        # Real responder, NoopAlert (default in __init__).
        msg = _make_pending(
            crypto,
            sender_id="peer-A",
            payload={"fn": "find-phone", "action": "start", "volume": 80},
        )
        api.fasttrack_pending.return_value = [msg]

        poller._process_fasttrack_pending()

        self.assertEqual(poller.config.active_device_id, "peer-A")
        # Responder fired its initial ringing heartbeat.
        api.fasttrack_send.assert_called_once()

    def test_sender_side_find_device_update_is_left_unacked(self) -> None:
        poller, api, crypto = _make_poller(
            self.tmp,
            paired={"peer-A": {"name": "Peer A"}},
        )
        msg = _make_pending(
            crypto,
            sender_id="peer-A",
            payload={"fn": "find-phone", "state": "ringing"},
            msg_id=9,
        )
        api.fasttrack_pending.return_value = [msg]

        poller._process_fasttrack_pending()

        api.fasttrack_ack.assert_not_called()
        api.fasttrack_send.assert_not_called()

    def test_unpair_message_removes_only_sender_pairing(self) -> None:
        poller, _api, _crypto = _make_poller(
            self.tmp,
            paired={
                "peer-A": {"name": "Peer A"},
                "peer-B": {"name": "Peer B"},
            },
        )
        message = DeviceMessage(
            type=MessageType.PAIRING_UNPAIR,
            transport=MessageTransport.TRANSFER_FILE,
            sender_id="peer-A",
        )

        with patch("src.poller.sync_file_manager_targets") as sync_targets:
            poller._handle_message_unpair(message)

        devices = poller.config.paired_devices
        self.assertNotIn("peer-A", devices)
        self.assertIn("peer-B", devices)
        sync_targets.assert_called_once_with(poller.config)
        poller.platform.notifications.notify.assert_called_once_with(
            "Unpaired",
            "Paired device disconnected",
        )


if __name__ == "__main__":
    unittest.main()
