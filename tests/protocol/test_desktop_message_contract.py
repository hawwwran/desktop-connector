import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_src_on_path  # noqa: E402

ensure_desktop_src_on_path()

from messaging.fasttrack_adapter import FasttrackAdapter  # noqa: E402
from messaging.fn_transfer_adapter import FnTransferAdapter  # noqa: E402
from messaging.message_types import MessageTransport, MessageType  # noqa: E402


class DesktopMessageContractTests(unittest.TestCase):
    def test_fn_clipboard_text_contract(self):
        msg = FnTransferAdapter.to_device_message(
            ".fn.clipboard.text",
            "hello".encode("utf-8"),
            sender_id="sender-1",
        )
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.type, MessageType.CLIPBOARD_TEXT)
        self.assertEqual(msg.transport, MessageTransport.TRANSFER_FILE)
        self.assertEqual(msg.payload["text"], "hello")
        self.assertEqual(msg.metadata["filename"], ".fn.clipboard.text")

    def test_fn_clipboard_image_contract(self):
        msg = FnTransferAdapter.to_device_message(
            ".fn.clipboard.image",
            b"\x01\x02",
            sender_id="sender-1",
        )
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.type, MessageType.CLIPBOARD_IMAGE)
        self.assertEqual(msg.transport, MessageTransport.TRANSFER_FILE)
        self.assertEqual(msg.payload["image_bytes"], b"\x01\x02")

    def test_fn_unpair_contract(self):
        msg = FnTransferAdapter.to_device_message(
            ".fn.unpair",
            b"",
            sender_id="sender-1",
        )
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.type, MessageType.PAIRING_UNPAIR)
        self.assertEqual(msg.transport, MessageTransport.TRANSFER_FILE)

    def test_fasttrack_find_phone_contract(self):
        start = FasttrackAdapter.to_device_message({"fn": "find-phone", "action": "start"}, sender_id="sender-1")
        stop = FasttrackAdapter.to_device_message({"fn": "find-phone", "action": "stop"}, sender_id="sender-1")
        state = FasttrackAdapter.to_device_message({"fn": "find-phone", "state": "ringing"}, sender_id="sender-1")

        self.assertIsNotNone(start)
        self.assertIsNotNone(stop)
        self.assertIsNotNone(state)

        assert start is not None
        assert stop is not None
        assert state is not None

        self.assertEqual(start.type, MessageType.FIND_PHONE_START)
        self.assertEqual(stop.type, MessageType.FIND_PHONE_STOP)
        self.assertEqual(state.type, MessageType.FIND_PHONE_LOCATION_UPDATE)
        self.assertEqual(start.transport, MessageTransport.FASTTRACK)


if __name__ == "__main__":
    unittest.main()
