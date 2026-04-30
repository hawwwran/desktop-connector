"""Target-device resolution for desktop non-GTK send paths."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.bootstrap import args as args_mod  # noqa: E402
from src.config import Config  # noqa: E402
from src.history import TransferHistory  # noqa: E402
from src.runners import send_runner as send_runner_mod  # noqa: E402
from src.tray import TrayApp  # noqa: E402


def _key_b64(byte: bytes = b"k") -> str:
    return base64.b64encode(byte * 32).decode()


def _paired_config(tmp: Path) -> Config:
    config = Config(tmp)
    config.device_id = "dev-self"
    config.auth_token = "tok"
    config.add_paired_device(
        "peer-active",
        "pk-active",
        _key_b64(b"a"),
        name="Active",
    )
    config.add_paired_device(
        "peer-explicit",
        "pk-explicit",
        _key_b64(b"b"),
        name="Explicit",
    )
    config.active_device_id = "peer-active"
    return config


class StartupArgsTargetTests(unittest.TestCase):
    def test_target_device_id_is_parsed_for_send(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "dc",
                "--headless",
                "--send",
                "/tmp/payload.txt",
                "--target-device-id",
                "peer-explicit",
            ],
        ):
            parsed = args_mod.parse_startup_args()

        self.assertEqual(parsed.send, "/tmp/payload.txt")
        self.assertEqual(parsed.target_device_id, "peer-explicit")


class SendRunnerTargetResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-target-send-"))
        self.filepath = self.tmp / "payload.txt"
        self.filepath.write_text("payload")
        self.config = _paired_config(self.tmp)
        self.crypto = MagicMock()

    def _run_with_fake_send(
        self,
        *,
        target_device_id: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        captured: dict[str, object] = {}

        def fake_send(_path, target_id, symmetric_key, **kwargs):
            captured["target_id"] = target_id
            captured["symmetric_key"] = symmetric_key
            tid = f"tid-{target_id}"
            kwargs["on_progress"](tid, 0, 1)
            return tid

        with patch.object(send_runner_mod, "ConnectionManager") as ConnCls, \
             patch.object(send_runner_mod, "ApiClient") as ApiCls:
            ConnCls.return_value.check_connection.return_value = True
            ApiCls.return_value.send_file.side_effect = fake_send
            rc = send_runner_mod.run_send_file(
                self.config,
                self.crypto,
                self.filepath,
                target_device_id=target_device_id,
            )

        return rc, captured

    def test_explicit_target_device_id_wins(self) -> None:
        rc, captured = self._run_with_fake_send(
            target_device_id="peer-explicit",
        )

        self.assertEqual(rc, 0)
        self.assertEqual(captured["target_id"], "peer-explicit")
        self.assertEqual(self.config.active_device_id, "peer-explicit")
        [row] = TransferHistory(self.tmp).items
        self.assertEqual(row["peer_device_id"], "peer-explicit")

    def test_without_explicit_target_uses_active_device(self) -> None:
        rc, captured = self._run_with_fake_send()

        self.assertEqual(rc, 0)
        self.assertEqual(captured["target_id"], "peer-active")
        [row] = TransferHistory(self.tmp).items
        self.assertEqual(row["peer_device_id"], "peer-active")

    def test_unpaired_explicit_target_fails_before_server_call(self) -> None:
        with patch.object(send_runner_mod, "ConnectionManager") as ConnCls, \
             patch.object(send_runner_mod, "ApiClient") as ApiCls:
            rc = send_runner_mod.run_send_file(
                self.config,
                self.crypto,
                self.filepath,
                target_device_id="peer-missing",
            )

        self.assertEqual(rc, 1)
        self.assertEqual(self.config.active_device_id, "peer-active")
        ConnCls.assert_not_called()
        ApiCls.assert_not_called()


class TrayTargetResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-target-tray-"))
        self.config = _paired_config(self.tmp)
        self.history = TransferHistory(self.tmp)
        self.api = MagicMock()
        self.platform = MagicMock()
        self.tray = TrayApp(
            connection=MagicMock(),
            poller=MagicMock(),
            api=self.api,
            config=self.config,
            crypto=MagicMock(),
            history=self.history,
            save_dir=self.tmp,
            platform=self.platform,
        )

    def test_clipboard_send_uses_active_device(self) -> None:
        captured: dict[str, str] = {}
        self.platform.clipboard.read_clipboard.return_value = (
            ".fn.clipboard.text",
            b"hello",
            "text/plain",
        )

        def fake_send(_path, target_id, _symmetric_key, **kwargs):
            captured["target_id"] = target_id
            tid = "tid-clipboard"
            kwargs["on_progress"](tid, 0, 1)
            return tid

        self.api.send_file.side_effect = fake_send

        self.tray._do_send_clipboard()

        self.assertEqual(captured["target_id"], "peer-active")
        [row] = TransferHistory(self.tmp).items
        self.assertEqual(row["peer_device_id"], "peer-active")

    def test_remote_status_ping_uses_active_device(self) -> None:
        called = threading.Event()
        captured: list[str] = []

        def fake_ping(target_id):
            captured.append(target_id)
            called.set()
            return {"online": True, "via": "fresh", "rtt_ms": 1}

        self.api.ping_device.side_effect = fake_ping
        self.tray._ping_lock = threading.Lock()
        self.tray._ping_in_flight = False
        self.tray._last_ping_time = 0.0
        self.tray._remote_online = False

        self.tray._maybe_ping(0.0)

        deadline = time.time() + 1.0
        while time.time() < deadline and not called.is_set():
            time.sleep(0.01)

        self.assertTrue(called.is_set())
        self.assertEqual(captured, ["peer-active"])


if __name__ == "__main__":
    unittest.main()
