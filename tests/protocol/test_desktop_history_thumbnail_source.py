"""Source-level checks for desktop history thumbnails."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class DesktopHistoryThumbnailSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(REPO_ROOT, "desktop/src/windows.py").read_text()

    def test_sent_clipboard_image_rows_can_render_thumbnail(self):
        for text in (
            'is_clipboard_image = filename.endswith(".fn.clipboard.image")',
            "has_existing_content = bool(content_path and Path(content_path).exists())",
            "can_thumbnail = (",
            "is_clipboard_image",
            "GdkPixbuf.Pixbuf.new_from_file_at_scale(content_path, 100, 100, True)",
            'Gtk.Image.new_from_icon_name("image-x-generic-symbolic")',
        ):
            self.assertIn(text, self.source)

    def test_history_mime_guess_falls_back_to_content_path(self):
        self.assertIn("mime, _ = _mt.guess_type(filename)", self.source)
        self.assertIn("if not mime and content_path:", self.source)
        self.assertIn("mime, _ = _mt.guess_type(content_path)", self.source)


if __name__ == "__main__":
    unittest.main()
