"""Pin that the --send one-shot runner produces the correct history
row shape for both classic and streaming sends.

C.5 rewires the callback in ``runners/send_runner.py`` to consume the
new ``on_stream_progress`` callback introduced in C.4. This test
exercises that wiring without touching the server — we patch
``ApiClient.send_file`` to drive the callbacks with canned sequences
and inspect the resulting history row.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

sys.path.insert(0, REPO_ROOT)

from desktop.src.history import TransferHistory, TransferStatus  # noqa: E402
from desktop.src.runners import send_runner as send_runner_mod  # noqa: E402


def _build_config(tmp: Path) -> MagicMock:
    config = MagicMock()
    config.is_registered = True
    config.is_paired = True
    config.server_url = "http://test.invalid"
    config.device_id = "dev-sender"
    config.auth_token = "tok"
    config.config_dir = tmp
    config.active_device_id = None
    config.paired_devices = {
        "peer-id": {
            "symmetric_key_b64": base64.b64encode(b"k" * 32).decode(),
            "name": "Peer",
            "paired_at": 1,
        },
    }
    config.get_first_paired_device.return_value = (
        "peer-id",
        {"symmetric_key_b64": base64.b64encode(b"k" * 32).decode()},
    )
    return config


class SendRunnerClassicTests(unittest.TestCase):
    """Classic-path callback produces the same history shape as before
    C.5 (mode=classic, status=uploading→complete)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-runner-classic-"))
        self.filepath = self.tmp / "payload.bin"
        self.filepath.write_bytes(b"hello")
        self.crypto = MagicMock()
        self.config = _build_config(self.tmp)

    def _fake_send(self, *args, **kwargs):
        """Drive the callbacks the way the real send_file would for
        a 2-chunk classic transfer."""
        on_progress = kwargs["on_progress"]
        tid = "tid-classic"
        on_progress(tid, 0, 2)
        on_progress(tid, 1, 2)
        on_progress(tid, 2, 2)
        return tid

    def test_classic_happy_path(self):
        with patch("desktop.src.runners.send_runner.ConnectionManager"), \
             patch("desktop.src.runners.send_runner.ApiClient") as ApiCls:
            api = ApiCls.return_value
            api.send_file.side_effect = self._fake_send
            api.conn.check_connection = MagicMock(return_value=True)
            # ConnectionManager.check_connection is called on the conn
            # object created at the top of run_send_file; simplify by
            # patching ConnectionManager too so .check_connection() is
            # a pass-through mock returning True.
            with patch("desktop.src.runners.send_runner.ConnectionManager") as ConnCls:
                ConnCls.return_value.check_connection.return_value = True
                rc = send_runner_mod.run_send_file(
                    self.config, self.crypto, self.filepath,
                )
        self.assertEqual(rc, 0)
        history = TransferHistory(self.tmp)
        [row] = history.items
        self.assertEqual(row["transfer_id"], "tid-classic")
        self.assertEqual(row["status"], TransferStatus.COMPLETE)
        # Classic rows persist mode=classic (default from C.2 history.add).
        self.assertEqual(row["mode"], "classic")


class SendRunnerStreamingTests(unittest.TestCase):
    """The streaming callback path writes mode=streaming +
    status=sending to the row as the stream progresses, then flips to
    complete on successful return."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-runner-stream-"))
        self.filepath = self.tmp / "payload.bin"
        self.filepath.write_bytes(b"stream")
        self.crypto = MagicMock()
        self.config = _build_config(self.tmp)

    def _fake_send_stream(self, *args, **kwargs):
        on_progress = kwargs["on_progress"]
        on_stream_progress = kwargs["on_stream_progress"]
        tid = "tid-stream"
        on_progress(tid, 0, 3)            # initial placeholder row
        on_stream_progress(tid, 0, 3, "sending")
        on_stream_progress(tid, 1, 3, "sending")
        on_stream_progress(tid, 2, 3, "sending")
        on_stream_progress(tid, 3, 3, "sending")
        return tid

    def test_streaming_happy_path(self):
        with patch("desktop.src.runners.send_runner.ConnectionManager") as ConnCls, \
             patch("desktop.src.runners.send_runner.ApiClient") as ApiCls:
            ConnCls.return_value.check_connection.return_value = True
            api = ApiCls.return_value
            api.send_file.side_effect = self._fake_send_stream
            rc = send_runner_mod.run_send_file(
                self.config, self.crypto, self.filepath,
            )
        self.assertEqual(rc, 0)
        history = TransferHistory(self.tmp)
        [row] = history.items
        self.assertEqual(row["status"], TransferStatus.COMPLETE)
        self.assertEqual(row["mode"], "streaming")

    def test_streaming_waiting_stream_then_recovery(self):
        """507 mid-stream flips the row to waiting_stream with
        waiting_started_at; recovery flips back to sending."""
        captured_rows: list[dict] = []

        def fake_send(*args, **kwargs):
            on_progress = kwargs["on_progress"]
            on_stream_progress = kwargs["on_stream_progress"]
            tid = "tid-wait"
            on_progress(tid, 0, 3)
            on_stream_progress(tid, 0, 3, "sending")
            on_stream_progress(tid, 1, 3, "sending")
            # 507 on chunk 2
            on_stream_progress(tid, 1, 3, "waiting_stream")
            # Snapshot row state DURING waiting
            captured_rows.append(
                next(it for it in TransferHistory(self.tmp).items
                     if it["transfer_id"] == tid)
            )
            on_stream_progress(tid, 1, 3, "sending")  # recovered
            on_stream_progress(tid, 2, 3, "sending")
            on_stream_progress(tid, 3, 3, "sending")
            return tid

        with patch("desktop.src.runners.send_runner.ConnectionManager") as ConnCls, \
             patch("desktop.src.runners.send_runner.ApiClient") as ApiCls:
            ConnCls.return_value.check_connection.return_value = True
            api = ApiCls.return_value
            api.send_file.side_effect = fake_send
            send_runner_mod.run_send_file(
                self.config, self.crypto, self.filepath,
            )
        # Mid-waiting snapshot must show waiting_stream.
        [snap] = captured_rows
        self.assertEqual(snap["status"], TransferStatus.WAITING_STREAM)
        self.assertIn("waiting_started_at", snap)

    def test_streaming_recipient_abort(self):
        """410 from recipient → state='aborted' fires → row marked
        Aborted with abort_reason=recipient_abort. The post-send_file
        fallback must NOT overwrite to Failed."""
        def fake_send(*args, **kwargs):
            on_progress = kwargs["on_progress"]
            on_stream_progress = kwargs["on_stream_progress"]
            tid = "tid-recipient-abort"
            on_progress(tid, 0, 3)
            on_stream_progress(tid, 0, 3, "sending")
            on_stream_progress(tid, 1, 3, "aborted")
            return None  # send_file returns None on any terminal non-ok

        with patch("desktop.src.runners.send_runner.ConnectionManager") as ConnCls, \
             patch("desktop.src.runners.send_runner.ApiClient") as ApiCls:
            ConnCls.return_value.check_connection.return_value = True
            ApiCls.return_value.send_file.side_effect = fake_send
            rc = send_runner_mod.run_send_file(
                self.config, self.crypto, self.filepath,
            )
        self.assertEqual(rc, 1)
        history = TransferHistory(self.tmp)
        [row] = history.items
        self.assertEqual(row["status"], TransferStatus.ABORTED)
        self.assertEqual(row["abort_reason"], "recipient_abort")

    def test_streaming_sender_failed_quota_timeout(self):
        """waiting_stream followed by state='failed' → row =
        Failed with failure_reason='quota_timeout'."""
        def fake_send(*args, **kwargs):
            on_progress = kwargs["on_progress"]
            on_stream_progress = kwargs["on_stream_progress"]
            tid = "tid-quota"
            on_progress(tid, 0, 3)
            on_stream_progress(tid, 0, 3, "sending")
            on_stream_progress(tid, 1, 3, "waiting_stream")
            on_stream_progress(tid, 1, 3, "failed")
            return None

        with patch("desktop.src.runners.send_runner.ConnectionManager") as ConnCls, \
             patch("desktop.src.runners.send_runner.ApiClient") as ApiCls:
            ConnCls.return_value.check_connection.return_value = True
            ApiCls.return_value.send_file.side_effect = fake_send
            rc = send_runner_mod.run_send_file(
                self.config, self.crypto, self.filepath,
            )
        self.assertEqual(rc, 1)
        history = TransferHistory(self.tmp)
        [row] = history.items
        self.assertEqual(row["status"], TransferStatus.FAILED)
        self.assertEqual(row["failure_reason"], "quota_timeout")


if __name__ == "__main__":
    unittest.main()
