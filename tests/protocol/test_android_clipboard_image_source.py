"""Source-level checks for Android clipboard image receive semantics."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class AndroidClipboardImageSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(
            REPO_ROOT,
            "android/app/src/main/kotlin/com/desktopconnector/service/PollService.kt",
        ).read_text()

    def test_clipboard_image_fn_transfer_is_saved_as_file(self):
        for text in (
            'fileName.startsWith(".fn.clipboard.image")',
            "saveClipboardImageTransfer(data, mimeType, transferId, senderId, prefs, db)",
            'saveFile("clipboard-image${imageType.first}", data)',
            "displayName = file.name",
            "displayLabel = file.name",
            "direction = TransferDirection.INCOMING",
            "status = TransferStatus.COMPLETE",
        ):
            self.assertIn(text, self.source)

    def test_clipboard_image_receive_no_longer_writes_clipboard(self):
        for text in (
            "register(MessageType.CLIPBOARD_IMAGE)",
            "pushImageToClipboard",
            "ClipData.newUri",
            "clipboard.write_image.succeeded",
        ):
            self.assertNotIn(text, self.source)


if __name__ == "__main__":
    unittest.main()
