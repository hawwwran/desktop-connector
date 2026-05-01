"""Poller integration tests for URL/Text receive actions."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

sys.path.insert(0, REPO_ROOT)

from desktop.src.config import (  # noqa: E402
    RECEIVE_ACTION_COPY,
    RECEIVE_ACTION_KEY_IMAGE_OPEN,
    RECEIVE_ACTION_KEY_TEXT_COPY,
    RECEIVE_ACTION_KEY_URL_OPEN,
    RECEIVE_ACTION_LIMIT_BATCH,
    RECEIVE_ACTION_LIMIT_MINUTE,
    RECEIVE_ACTION_NONE,
    RECEIVE_ACTION_OPEN,
    RECEIVE_KIND_IMAGE,
    RECEIVE_KIND_TEXT,
    RECEIVE_KIND_URL,
    RECEIVE_KIND_VIDEO,
)
from desktop.src.messaging.message_model import DeviceMessage  # noqa: E402
from desktop.src.messaging.message_types import MessageTransport, MessageType  # noqa: E402
from desktop.src.poller import Poller  # noqa: E402
from desktop.src.receive_actions import ReceiveActionResult  # noqa: E402


class _Config:
    def __init__(
        self,
        actions: dict[str, str],
        *,
        limits: dict[str, dict[str, int]] | None = None,
    ):
        self.actions = actions
        self.limits = limits or {}
        self.config_dir = Path(tempfile.mkdtemp(prefix="dc-recv-action-config-"))
        self.save_directory = self.config_dir / "save"
        self.save_directory.mkdir()
        self.paired_devices = {}

    def get_receive_action(self, kind: str) -> str:
        return self.actions.get(kind, RECEIVE_ACTION_NONE)

    def get_receive_action_limits(self, action_key: str) -> dict[str, int]:
        return dict(
            self.limits.get(
                action_key,
                {RECEIVE_ACTION_LIMIT_BATCH: 0, RECEIVE_ACTION_LIMIT_MINUTE: 0},
            )
        )


class _Clipboard:
    def __init__(self, *, result: bool = True):
        self.result = result
        self.written_text: list[str] = []

    def write_text(self, text: str) -> bool:
        self.written_text.append(text)
        return self.result


class _Shell:
    def __init__(self, *, result: bool = True):
        self.result = result
        self.opened_urls: list[str] = []
        self.opened_paths: list[Path] = []

    def open_url(self, url: str) -> bool:
        self.opened_urls.append(url)
        return self.result

    def open_path(self, path: Path) -> bool:
        self.opened_paths.append(path)
        return self.result


class _Notifications:
    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.file_received: list[Path] = []

    def notify(self, title: str, body: str, icon: str = "dialog-information") -> None:
        self.events.append((title, body))

    def notify_file_received(self, filepath: Path) -> None:
        self.file_received.append(filepath)


class _Platform:
    def __init__(
        self,
        *,
        clipboard: _Clipboard | None = None,
        shell: _Shell | None = None,
    ):
        from desktop.src.interfaces.location import NullLocationProvider

        self.clipboard = clipboard or _Clipboard()
        self.shell = shell or _Shell()
        self.notifications = _Notifications()
        # Production wiring relies on the typed `location` field on
        # DesktopPlatform; mirror it here so the responder can read
        # NullLocationProvider().get_current_fix without exploding.
        self.location = NullLocationProvider()


class ReceiveActionsPollerTests(unittest.TestCase):
    def _build_poller(
        self,
        actions: dict[str, str],
        *,
        platform: _Platform | None = None,
        limits: dict[str, dict[str, int]] | None = None,
    ):
        history = MagicMock()
        poller = Poller(
            config=_Config(actions, limits=limits),
            connection=MagicMock(),
            api=MagicMock(),
            crypto=MagicMock(),
            history=history,
            platform=platform or _Platform(),
        )
        return poller, history

    def _message(self, text: str) -> DeviceMessage:
        return DeviceMessage(
            type=MessageType.CLIPBOARD_TEXT,
            transport=MessageTransport.TRANSFER_FILE,
            payload={"text": text},
            metadata={"filename": ".fn.clipboard.text"},
        )

    def test_exact_url_open_runs_url_action_only(self):
        poller, history = self._build_poller({
            RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })

        poller._handle_message_clipboard_text(self._message("https://example.com"))

        self.assertEqual(poller.platform.shell.opened_urls, ["https://example.com"])
        self.assertEqual(poller.platform.clipboard.written_text, [])
        # Browser opening IS the user feedback — no extra "Clipboard
        # received" toast on top of it.
        self.assertEqual(poller.platform.notifications.events, [])
        history.add.assert_called_once()

    def test_exact_url_copy_copies_url_once(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_URL: RECEIVE_ACTION_COPY,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })

        poller._handle_message_clipboard_text(self._message("https://example.com"))

        self.assertEqual(poller.platform.shell.opened_urls, [])
        self.assertEqual(poller.platform.clipboard.written_text, ["https://example.com"])

    def test_exact_url_none_does_not_copy_or_open(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_URL: RECEIVE_ACTION_NONE,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })

        poller._handle_message_clipboard_text(self._message("https://example.com"))

        self.assertEqual(poller.platform.shell.opened_urls, [])
        self.assertEqual(poller.platform.clipboard.written_text, [])

    def test_plain_text_copy_uses_text_action(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })

        poller._handle_message_clipboard_text(self._message("hello"))

        self.assertEqual(poller.platform.shell.opened_urls, [])
        self.assertEqual(poller.platform.clipboard.written_text, ["hello"])

    def test_plain_text_none_does_not_copy(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_NONE,
        })

        poller._handle_message_clipboard_text(self._message("hello"))

        self.assertEqual(poller.platform.shell.opened_urls, [])
        self.assertEqual(poller.platform.clipboard.written_text, [])

    def test_embedded_url_runs_url_and_text_with_one_clipboard_write(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_URL: RECEIVE_ACTION_COPY,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })

        text = "See https://example.com/report.pdf when ready"
        poller._handle_message_clipboard_text(self._message(text))

        self.assertEqual(poller.platform.shell.opened_urls, [])
        self.assertEqual(poller.platform.clipboard.written_text, [text])

    def test_embedded_url_open_and_text_none_opens_only(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_NONE,
        })

        text = "See https://example.com/report.pdf when ready"
        poller._handle_message_clipboard_text(self._message(text))

        self.assertEqual(
            poller.platform.shell.opened_urls,
            ["https://example.com/report.pdf"],
        )
        self.assertEqual(poller.platform.clipboard.written_text, [])

    def test_action_failure_does_not_block_history_or_notification(self):
        platform = _Platform(clipboard=_Clipboard(result=False))
        poller, history = self._build_poller(
            {RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY},
            platform=platform,
        )

        poller._handle_message_clipboard_text(self._message("hello"))

        history.add.assert_called_once()
        self.assertEqual(platform.notifications.events, [("Clipboard received", "hello")])
        self.assertEqual(platform.clipboard.written_text, ["hello"])

    def test_classic_file_receive_runs_action_before_callbacks(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN,
        })
        poller.api.ack_transfer.return_value = True
        events: list[tuple[str, str]] = []
        poller.on_file_received(lambda p: events.append(("callback", p.name)))

        def record_action(_config, _platform, kind, *, path, **_kwargs):
            events.append(("action", path.name))
            self.assertEqual(kind, RECEIVE_KIND_IMAGE)
            return ReceiveActionResult(ok=True, action_ran=True)

        with patch.object(
            poller,
            "_download_and_decrypt_chunk",
            return_value=b"image",
        ), patch("desktop.src.poller.apply_receive_action", side_effect=record_action) as action:
            poller._receive_file_transfer(
                "tid-file",
                "sender",
                "photo.jpg",
                1,
                b"nonce",
                b"key",
            )

        self.assertTrue((poller.config.save_directory / "photo.jpg").exists())
        action.assert_called_once()
        self.assertEqual(events, [("action", "photo.jpg"), ("callback", "photo.jpg")])
        # Image viewer launching IS the user feedback — suppress the
        # file-received toast.
        self.assertEqual(poller.platform.notifications.file_received, [])

    def test_streaming_file_receive_runs_action_before_callbacks(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_VIDEO: RECEIVE_ACTION_OPEN,
        })
        poller.api.ack_chunk.return_value = True
        events: list[tuple[str, str]] = []
        poller.on_file_received(lambda p: events.append(("callback", p.name)))

        def record_action(_config, _platform, kind, *, path, **_kwargs):
            events.append(("action", path.name))
            self.assertEqual(kind, RECEIVE_KIND_VIDEO)
            return ReceiveActionResult(ok=True, action_ran=True)

        with patch.object(
            poller,
            "_stream_download_chunk",
            return_value=("ok", b"video"),
        ), patch("desktop.src.poller.apply_receive_action", side_effect=record_action) as action:
            poller._receive_streaming_transfer(
                "tid-stream",
                "sender",
                "movie.mp4",
                1,
                b"nonce",
                b"key",
            )

        self.assertTrue((poller.config.save_directory / "movie.mp4").exists())
        action.assert_called_once()
        self.assertEqual(events, [("action", "movie.mp4"), ("callback", "movie.mp4")])
        self.assertEqual(poller.platform.notifications.file_received, [])

    def test_file_action_is_not_run_after_failed_download(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN,
        })

        with patch.object(
            poller,
            "_download_and_decrypt_chunk",
            return_value=None,
        ), patch("desktop.src.poller.apply_receive_action") as action:
            poller._receive_file_transfer(
                "tid-fail",
                "sender",
                "photo.jpg",
                1,
                b"nonce",
                b"key",
            )

        action.assert_not_called()

    def test_file_action_failure_still_runs_callbacks(self):
        poller, _history = self._build_poller({
            RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN,
        })
        poller.api.ack_transfer.return_value = True
        callbacks: list[str] = []
        poller.on_file_received(lambda p: callbacks.append(p.name))

        with patch.object(
            poller,
            "_download_and_decrypt_chunk",
            return_value=b"image",
        ), patch(
            "desktop.src.poller.apply_receive_action",
            return_value=ReceiveActionResult(ok=False, action_ran=False),
        ):
            poller._receive_file_transfer(
                "tid-action-fail",
                "sender",
                "photo.jpg",
                1,
                b"nonce",
                b"key",
            )

        self.assertEqual(callbacks, ["photo.jpg"])
        # Action failed — the user didn't see the image viewer open, so
        # the toast IS helpful here.
        self.assertEqual(
            [p.name for p in poller.platform.notifications.file_received],
            ["photo.jpg"],
        )

    def test_other_file_type_does_not_run_action(self):
        poller, _history = self._build_poller({})

        with patch.object(
            poller,
            "_download_and_decrypt_chunk",
            return_value=b"archive",
        ), patch("desktop.src.poller.apply_receive_action") as action:
            poller._receive_file_transfer(
                "tid-archive",
                "sender",
                "archive.zip",
                1,
                b"nonce",
                b"key",
            )

        action.assert_not_called()

    def test_fn_clipboard_text_uses_receive_action_batch_limits(self):
        poller, _history = self._build_poller(
            {RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY},
            limits={
                RECEIVE_ACTION_KEY_TEXT_COPY: {
                    RECEIVE_ACTION_LIMIT_BATCH: 1,
                    RECEIVE_ACTION_LIMIT_MINUTE: 0,
                },
            },
        )
        batch = poller._receive_action_limiter.start_batch(2)

        first = poller.config.save_directory / ".fn.clipboard.text"
        first.write_text("first")
        poller._handle_fn_transfer(first, receive_action_batch=batch)

        second = poller.config.save_directory / ".fn.clipboard.text"
        second.write_text("second")
        poller._handle_fn_transfer(second, receive_action_batch=batch)

        self.assertEqual(poller.platform.clipboard.written_text, ["first"])
        self.assertEqual(
            poller._receive_action_limiter.finish_batch(batch).suppressed_counts,
            {RECEIVE_ACTION_KEY_TEXT_COPY: 1},
        )

    def test_fn_clipboard_image_is_saved_as_image_transfer(self):
        poller, history = self._build_poller({
            RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN,
        })
        callbacks: list[str] = []
        poller.on_file_received(lambda p: callbacks.append(p.name))

        source = poller.config.save_directory / ".fn.clipboard.image"
        source.write_bytes(b"\x89PNG\r\n\x1a\nimage")

        poller._handle_fn_transfer(
            source,
            sender_id="sender-1",
            transfer_id="tid-image",
            mime_type="image/png",
        )

        final_path = poller.config.save_directory / "clipboard-image.png"
        self.assertTrue(final_path.exists())
        self.assertFalse(source.exists())
        self.assertEqual(poller.platform.shell.opened_paths, [final_path])
        self.assertEqual(callbacks, ["clipboard-image.png"])
        history.add.assert_called_once_with(
            filename="clipboard-image.png",
            display_label="clipboard-image.png",
            direction="received",
            size=13,
            content_path=str(final_path),
            sender_id="sender-1",
            peer_device_id="sender-1",
            transfer_id="tid-image",
        )

    def test_fn_clipboard_image_uses_receive_action_batch_limits(self):
        poller, _history = self._build_poller(
            {RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN},
            limits={
                RECEIVE_ACTION_KEY_IMAGE_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 1,
                    RECEIVE_ACTION_LIMIT_MINUTE: 0,
                },
            },
        )
        batch = poller._receive_action_limiter.start_batch(2)

        first = poller.config.save_directory / ".fn.clipboard.image"
        first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
        poller._handle_fn_transfer(first, receive_action_batch=batch)

        second = poller.config.save_directory / ".fn.clipboard.image"
        second.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
        poller._handle_fn_transfer(second, receive_action_batch=batch)

        self.assertEqual(
            [path.name for path in poller.platform.shell.opened_paths],
            ["clipboard-image.png"],
        )
        self.assertTrue((poller.config.save_directory / "clipboard-image.png").exists())
        self.assertTrue((poller.config.save_directory / "clipboard-image_1.png").exists())
        self.assertEqual(
            poller._receive_action_limiter.finish_batch(batch).suppressed_counts,
            {RECEIVE_ACTION_KEY_IMAGE_OPEN: 1},
        )

    def test_poll_once_batches_receive_action_limits_and_notifies_summary(self):
        poller, _history = self._build_poller(
            {},
            limits={
                RECEIVE_ACTION_KEY_URL_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 1,
                    RECEIVE_ACTION_LIMIT_MINUTE: 0,
                },
            },
        )
        transfers = [{"transfer_id": "one"}, {"transfer_id": "two"}]
        poller.api.get_pending_transfers.return_value = transfers
        observed_batches = []

        def fake_download(_transfer, *, receive_action_batch=None):
            self.assertIsNotNone(receive_action_batch)
            observed_batches.append(receive_action_batch)
            poller._receive_action_limiter.allow(
                RECEIVE_ACTION_KEY_URL_OPEN,
                receive_action_batch,
            )

        with patch.object(poller, "_download_transfer", side_effect=fake_download):
            poller._poll_once()

        self.assertEqual(len(observed_batches), 2)
        self.assertIs(observed_batches[0], observed_batches[1])
        self.assertEqual(
            poller.platform.notifications.events,
            [(
                "Receive actions limited",
                "Received 2 items. Skipped 1 automatic action to prevent flooding.",
            )],
        )


if __name__ == "__main__":
    unittest.main()
