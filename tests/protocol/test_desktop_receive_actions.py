"""Unit tests for desktop receive-action classification and execution."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import (  # noqa: E402
    RECEIVE_ACTION_COPY,
    RECEIVE_ACTION_NONE,
    RECEIVE_ACTION_OPEN,
    RECEIVE_KIND_DOCUMENT,
    RECEIVE_KIND_IMAGE,
    RECEIVE_KIND_TEXT,
    RECEIVE_KIND_URL,
    RECEIVE_KIND_VIDEO,
)
from src.receive_actions import (  # noqa: E402
    RECEIVE_KIND_OTHER,
    apply_receive_action,
    apply_receive_text_actions,
    classify_received_file,
    classify_received_text,
    extract_received_urls,
)


class _FakeConfig:
    def __init__(self, actions: dict[str, str]):
        self._actions = actions

    def get_receive_action(self, kind: str) -> str:
        return self._actions.get(kind, RECEIVE_ACTION_NONE)


class _FakeShell:
    def __init__(self, result: bool = True, *, raise_on_call: bool = False):
        self.result = result
        self.raise_on_call = raise_on_call
        self.opened_urls: list[str] = []
        self.opened_paths: list[Path] = []

    def open_url(self, url: str) -> bool:
        if self.raise_on_call:
            raise RuntimeError("open failed")
        self.opened_urls.append(url)
        return self.result

    def open_path(self, path: Path) -> bool:
        if self.raise_on_call:
            raise RuntimeError("open failed")
        self.opened_paths.append(path)
        return self.result


class _FakeClipboard:
    def __init__(self, result: bool = True, *, raise_on_call: bool = False):
        self.result = result
        self.raise_on_call = raise_on_call
        self.written_text: list[str] = []

    def write_text(self, text: str) -> bool:
        if self.raise_on_call:
            raise RuntimeError("copy failed")
        self.written_text.append(text)
        return self.result


class _FakePlatform:
    def __init__(
        self,
        *,
        shell: _FakeShell | None = None,
        clipboard: _FakeClipboard | None = None,
    ):
        self.shell = shell or _FakeShell()
        self.clipboard = clipboard or _FakeClipboard()


class TextClassificationTests(unittest.TestCase):
    def test_exact_http_url_is_classified(self):
        self.assertEqual(
            classify_received_text("http://example.com/path"),
            (RECEIVE_KIND_URL, "http://example.com/path"),
        )

    def test_exact_https_url_is_trimmed_and_classified(self):
        self.assertEqual(
            classify_received_text("\n  https://example.com/a?b=c  \t"),
            (RECEIVE_KIND_URL, "https://example.com/a?b=c"),
        )

    def test_partial_text_url_is_rejected(self):
        self.assertEqual(
            classify_received_text("See https://example.com"),
            (None, None),
        )

    def test_multiple_urls_are_rejected(self):
        self.assertEqual(
            classify_received_text("https://a.example https://b.example"),
            (None, None),
        )

    def test_non_http_schemes_are_rejected(self):
        self.assertEqual(classify_received_text("mailto:a@example.com"), (None, None))
        self.assertEqual(classify_received_text("file:///tmp/report.pdf"), (None, None))

    def test_http_without_netloc_is_rejected(self):
        self.assertEqual(classify_received_text("https:///missing-host"), (None, None))

    def test_embedded_urls_are_extracted_for_url_action(self):
        self.assertEqual(
            extract_received_urls("See https://example.com/report.pdf, thanks"),
            ["https://example.com/report.pdf"],
        )


class FileClassificationTests(unittest.TestCase):
    def test_image_mime_is_classified(self):
        self.assertEqual(classify_received_file(Path("photo.jpg")), RECEIVE_KIND_IMAGE)

    def test_video_mime_is_classified(self):
        self.assertEqual(classify_received_file(Path("clip.mp4")), RECEIVE_KIND_VIDEO)

    def test_document_mime_is_classified(self):
        self.assertEqual(classify_received_file(Path("report.pdf")), RECEIVE_KIND_DOCUMENT)
        self.assertEqual(classify_received_file(Path("notes.txt")), RECEIVE_KIND_DOCUMENT)
        self.assertEqual(classify_received_file(Path("paper.docx")), RECEIVE_KIND_DOCUMENT)

    def test_extension_fallback_classifies_common_files(self):
        self.assertEqual(classify_received_file(Path("image.heic")), RECEIVE_KIND_IMAGE)
        self.assertEqual(classify_received_file(Path("movie.mkv")), RECEIVE_KIND_VIDEO)
        self.assertEqual(classify_received_file(Path("deck.odp")), RECEIVE_KIND_DOCUMENT)

    def test_archives_and_unknown_files_are_other(self):
        self.assertEqual(classify_received_file(Path("archive.zip")), RECEIVE_KIND_OTHER)
        self.assertEqual(classify_received_file(Path("archive.tar.gz")), RECEIVE_KIND_OTHER)
        self.assertEqual(classify_received_file(Path("blob.unknownbin")), RECEIVE_KIND_OTHER)


class ApplyReceiveActionTests(unittest.TestCase):
    def test_none_action_has_no_side_effects(self):
        config = _FakeConfig({RECEIVE_KIND_URL: RECEIVE_ACTION_NONE})
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url="https://example.com",
            )
        )
        self.assertEqual(platform.shell.opened_urls, [])
        self.assertEqual(platform.clipboard.written_text, [])

    def test_url_open_action_opens_url(self):
        config = _FakeConfig({RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN})
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url="https://example.com",
            )
        )
        self.assertEqual(platform.shell.opened_urls, ["https://example.com"])
        self.assertEqual(platform.clipboard.written_text, [])

    def test_url_copy_action_copies_url(self):
        config = _FakeConfig({RECEIVE_KIND_URL: RECEIVE_ACTION_COPY})
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url="https://example.com",
            )
        )
        self.assertEqual(platform.shell.opened_urls, [])
        self.assertEqual(platform.clipboard.written_text, ["https://example.com"])

    def test_text_copy_action_copies_text(self):
        config = _FakeConfig({RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY})
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_TEXT,
                text="plain text",
            )
        )
        self.assertEqual(platform.clipboard.written_text, ["plain text"])

    def test_file_open_action_opens_path(self):
        config = _FakeConfig({RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN})
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_IMAGE,
                path=Path("/tmp/photo.jpg"),
            )
        )
        self.assertEqual(platform.shell.opened_paths, [Path("/tmp/photo.jpg")])

    def test_backend_false_result_is_returned(self):
        config = _FakeConfig({RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN})
        platform = _FakePlatform(shell=_FakeShell(result=False))

        self.assertFalse(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url="https://example.com",
            )
        )

    def test_backend_exception_is_caught(self):
        config = _FakeConfig({RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN})
        platform = _FakePlatform(shell=_FakeShell(raise_on_call=True))

        self.assertFalse(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url="https://example.com",
            )
        )

    def test_unsupported_combination_returns_false(self):
        config = _FakeConfig({RECEIVE_KIND_IMAGE: RECEIVE_ACTION_COPY})
        platform = _FakePlatform()

        self.assertFalse(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_IMAGE,
                path=Path("/tmp/photo.jpg"),
            )
        )
        self.assertEqual(platform.shell.opened_paths, [])
        self.assertEqual(platform.clipboard.written_text, [])

    def test_other_none_action_has_no_side_effects(self):
        config = _FakeConfig({RECEIVE_KIND_OTHER: RECEIVE_ACTION_NONE})
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_OTHER,
                path=Path("/tmp/archive.zip"),
            )
        )
        self.assertEqual(platform.shell.opened_paths, [])
        self.assertEqual(platform.clipboard.written_text, [])


class ApplyReceiveTextActionsTests(unittest.TestCase):
    def test_exact_url_runs_only_url_action(self):
        config = _FakeConfig({
            RECEIVE_KIND_URL: RECEIVE_ACTION_NONE,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_text_actions(config, platform, "https://example.com")
        )
        self.assertEqual(platform.shell.opened_urls, [])
        self.assertEqual(platform.clipboard.written_text, [])

    def test_plain_text_runs_text_action(self):
        config = _FakeConfig({RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY})
        platform = _FakePlatform()

        self.assertTrue(apply_receive_text_actions(config, platform, "hello"))
        self.assertEqual(platform.shell.opened_urls, [])
        self.assertEqual(platform.clipboard.written_text, ["hello"])

    def test_embedded_url_runs_url_and_text_actions(self):
        config = _FakeConfig({
            RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })
        platform = _FakePlatform()

        text = "See https://example.com/report.pdf when ready"
        self.assertTrue(apply_receive_text_actions(config, platform, text))
        self.assertEqual(
            platform.shell.opened_urls,
            ["https://example.com/report.pdf"],
        )
        self.assertEqual(platform.clipboard.written_text, [text])

    def test_embedded_url_copy_and_text_copy_flushes_clipboard_once(self):
        config = _FakeConfig({
            RECEIVE_KIND_URL: RECEIVE_ACTION_COPY,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
        })
        platform = _FakePlatform()

        text = "See https://example.com/report.pdf when ready"
        self.assertTrue(apply_receive_text_actions(config, platform, text))
        self.assertEqual(platform.shell.opened_urls, [])
        self.assertEqual(platform.clipboard.written_text, [text])

    def test_embedded_url_copy_with_text_none_copies_url_once(self):
        config = _FakeConfig({
            RECEIVE_KIND_URL: RECEIVE_ACTION_COPY,
            RECEIVE_KIND_TEXT: RECEIVE_ACTION_NONE,
        })
        platform = _FakePlatform()

        self.assertTrue(
            apply_receive_text_actions(
                config,
                platform,
                "See https://example.com/report.pdf when ready",
            )
        )
        self.assertEqual(
            platform.clipboard.written_text,
            ["https://example.com/report.pdf"],
        )


if __name__ == "__main__":
    unittest.main()
