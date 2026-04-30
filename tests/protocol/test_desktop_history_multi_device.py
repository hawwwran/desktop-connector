"""History peer attribution and active-device tracking for M.1."""

from __future__ import annotations

import base64
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
from src.history import TransferHistory, TransferStatus  # noqa: E402
from src.poller import Poller  # noqa: E402
from src.runners import send_runner as send_runner_mod  # noqa: E402


def _key_b64() -> str:
    return base64.b64encode(b"k" * 32).decode()


class HistoryPeerAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-history-peer-"))
        self.history = TransferHistory(self.tmp)

    def test_new_rows_persist_peer_device_id(self) -> None:
        self.history.add(
            filename="sent.txt",
            display_label="sent.txt",
            direction="sent",
            size=10,
            transfer_id="tid-sent",
            peer_device_id="peer-target",
        )

        [row] = self.history.items
        self.assertEqual(row["peer_device_id"], "peer-target")
        self.assertEqual(
            self.history.get_peer_device_id(row),
            "peer-target",
        )

    def test_legacy_received_rows_fall_back_to_sender_id(self) -> None:
        legacy = {
            "direction": "received",
            "sender_id": "legacy-sender",
        }

        self.assertEqual(
            self.history.get_peer_device_id(legacy),
            "legacy-sender",
        )

    def test_legacy_sent_rows_use_explicit_fallback(self) -> None:
        legacy = {"direction": "sent"}

        self.assertEqual(
            self.history.get_peer_device_id(
                legacy,
                fallback_device_id="active-peer",
            ),
            "active-peer",
        )


class SendRunnerPeerAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-send-peer-"))
        self.filepath = self.tmp / "payload.txt"
        self.filepath.write_text("payload")
        self.config = Config(self.tmp)
        self.config.device_id = "dev-self"
        self.config.auth_token = "tok"
        self.config.add_paired_device(
            "peer-send",
            "pk-peer",
            _key_b64(),
            name="Peer",
        )
        self.crypto = MagicMock()

    def test_send_file_marks_target_active_and_writes_peer_id(self) -> None:
        def fake_send(_path, _target_id, _key, **kwargs):
            tid = "tid-send"
            kwargs["on_progress"](tid, 0, 1)
            kwargs["on_progress"](tid, 1, 1)
            return tid

        with patch.object(send_runner_mod, "ConnectionManager") as ConnCls, \
             patch.object(send_runner_mod, "ApiClient") as ApiCls:
            ConnCls.return_value.check_connection.return_value = True
            ApiCls.return_value.send_file.side_effect = fake_send

            rc = send_runner_mod.run_send_file(
                self.config,
                self.crypto,
                self.filepath,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(self.config.active_device_id, "peer-send")
        [row] = TransferHistory(self.tmp).items
        self.assertEqual(row["peer_device_id"], "peer-send")
        self.assertEqual(row["status"], TransferStatus.COMPLETE)


class ReceivePeerAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-recv-peer-"))
        self.config = Config(self.tmp / "config")
        self.config.device_id = "dev-self"
        self.config.auth_token = "tok"
        self.config.save_directory = self.tmp / "save"
        self.config.add_paired_device(
            "peer-recv",
            "pk-peer",
            _key_b64(),
            name="Peer",
        )
        self.history = TransferHistory(self.config.config_dir)
        self.api = MagicMock()
        self.api.ack_transfer.return_value = True
        self.poller = Poller(
            config=self.config,
            connection=MagicMock(),
            api=self.api,
            crypto=MagicMock(),
            history=self.history,
            platform=MagicMock(),
        )
        self.poller.crypto.decrypt_metadata.return_value = {
            "filename": "incoming.txt",
            "mime_type": "text/plain",
            "base_nonce": base64.b64encode(b"n" * 24).decode(),
        }

    def test_incoming_file_marks_sender_active_and_writes_peer_id(self) -> None:
        transfer = {
            "transfer_id": "tid-recv",
            "sender_id": "peer-recv",
            "encrypted_meta": "meta",
            "chunk_count": 1,
            "mode": "classic",
        }

        with patch.object(
            self.poller,
            "_download_and_decrypt_chunk",
            return_value=b"payload",
        ), patch.object(self.poller, "_apply_receive_file_action"):
            self.poller._download_transfer(transfer)

        self.assertEqual(self.config.active_device_id, "peer-recv")
        [row] = TransferHistory(self.config.config_dir).items
        self.assertEqual(row["peer_device_id"], "peer-recv")
        self.assertEqual(row["sender_id"], "peer-recv")
        self.assertEqual(row["status"], TransferStatus.COMPLETE)

    def test_fn_clipboard_text_writes_peer_id(self) -> None:
        self.config.active_device_id = None
        source = self.config.save_directory / ".fn.clipboard.text"
        source.write_text("hello")

        self.poller._handle_fn_transfer(source, sender_id="peer-recv")

        self.assertEqual(self.config.active_device_id, "peer-recv")
        [row] = TransferHistory(self.config.config_dir).items
        self.assertEqual(row["peer_device_id"], "peer-recv")
        self.assertEqual(row["sender_id"], "peer-recv")
        self.assertEqual(row["display_label"], "hello")


if __name__ == "__main__":
    unittest.main()
