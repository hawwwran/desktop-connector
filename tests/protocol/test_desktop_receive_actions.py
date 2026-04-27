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
    RECEIVE_ACTION_KEY_DOCUMENT_OPEN,
    RECEIVE_ACTION_KEY_IMAGE_OPEN,
    RECEIVE_ACTION_KEY_TEXT_COPY,
    RECEIVE_ACTION_KEY_URL_COPY,
    RECEIVE_ACTION_KEY_URL_OPEN,
    RECEIVE_ACTION_LIMIT_BATCH,
    RECEIVE_ACTION_LIMIT_MINUTE,
    RECEIVE_ACTION_NONE,
    RECEIVE_ACTION_OPEN,
    RECEIVE_KIND_DOCUMENT,
    RECEIVE_KIND_IMAGE,
    RECEIVE_KIND_TEXT,
    RECEIVE_KIND_URL,
    RECEIVE_KIND_VIDEO,
)
from src.receive_actions import (  # noqa: E402
    RECEIVE_ACTION_WINDOW_S,
    RECEIVE_KIND_OTHER,
    ReceiveActionLimiter,
    apply_receive_action,
    apply_receive_text_actions,
    classify_received_file,
    classify_received_text,
    extract_received_urls,
)


class _FakeConfig:
    def __init__(
        self,
        actions: dict[str, str],
        *,
        limits: dict[str, dict[str, int]] | None = None,
    ):
        self._actions = actions
        self._limits = limits or {}

    def get_receive_action(self, kind: str) -> str:
        return self._actions.get(kind, RECEIVE_ACTION_NONE)

    def get_receive_action_limits(self, action_key: str) -> dict[str, int]:
        return dict(
            self._limits.get(
                action_key,
                {RECEIVE_ACTION_LIMIT_BATCH: 0, RECEIVE_ACTION_LIMIT_MINUTE: 0},
            )
        )


class _FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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

    def test_embedded_url_preserves_balanced_parentheses(self):
        self.assertEqual(
            extract_received_urls(
                "See https://en.wikipedia.org/wiki/Function_(mathematics) today",
            ),
            ["https://en.wikipedia.org/wiki/Function_(mathematics)"],
        )

    def test_embedded_url_preserves_balanced_brackets(self):
        self.assertEqual(
            extract_received_urls("See https://example.com/path[section] today"),
            ["https://example.com/path[section]"],
        )

    def test_embedded_url_strips_unmatched_wrapper_delimiters(self):
        self.assertEqual(
            extract_received_urls("See (https://example.com/report.pdf)."),
            ["https://example.com/report.pdf"],
        )

    def test_embedded_url_strips_only_extra_closing_delimiter(self):
        self.assertEqual(
            extract_received_urls("See https://example.com/path[section]) today"),
            ["https://example.com/path[section]"],
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

    def test_limited_url_open_is_successful_noop(self):
        config = _FakeConfig(
            {RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN},
            limits={
                RECEIVE_ACTION_KEY_URL_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 1,
                    RECEIVE_ACTION_LIMIT_MINUTE: 0,
                },
            },
        )
        platform = _FakePlatform()
        limiter = ReceiveActionLimiter(config, clock=_FakeClock())
        batch = limiter.start_batch(2)

        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url="https://first.example",
                limiter=limiter,
                batch=batch,
            )
        )
        self.assertTrue(
            apply_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url="https://second.example",
                limiter=limiter,
                batch=batch,
            )
        )

        self.assertEqual(platform.shell.opened_urls, ["https://first.example"])
        self.assertEqual(
            limiter.finish_batch(batch).suppressed_counts,
            {RECEIVE_ACTION_KEY_URL_OPEN: 1},
        )


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

    def test_limited_text_copy_keeps_prior_staged_url_copy(self):
        config = _FakeConfig(
            {
                RECEIVE_KIND_URL: RECEIVE_ACTION_COPY,
                RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
            },
            limits={
                RECEIVE_ACTION_KEY_URL_COPY: {
                    RECEIVE_ACTION_LIMIT_BATCH: 0,
                    RECEIVE_ACTION_LIMIT_MINUTE: 0,
                },
                RECEIVE_ACTION_KEY_TEXT_COPY: {
                    RECEIVE_ACTION_LIMIT_BATCH: 0,
                    RECEIVE_ACTION_LIMIT_MINUTE: 1,
                },
            },
        )
        platform = _FakePlatform()
        limiter = ReceiveActionLimiter(config, clock=_FakeClock())
        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_TEXT_COPY))

        text = "See https://example.com/report.pdf when ready"
        self.assertTrue(
            apply_receive_text_actions(
                config,
                platform,
                text,
                limiter=limiter,
                batch=limiter.start_batch(1),
            )
        )

        self.assertEqual(
            platform.clipboard.written_text,
            ["https://example.com/report.pdf"],
        )


class ReceiveActionLimiterTests(unittest.TestCase):
    def test_batch_limit_suppresses_after_threshold(self):
        config = _FakeConfig(
            {},
            limits={
                RECEIVE_ACTION_KEY_URL_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 3,
                    RECEIVE_ACTION_LIMIT_MINUTE: 0,
                },
            },
        )
        limiter = ReceiveActionLimiter(config, clock=_FakeClock())
        batch = limiter.start_batch(5)

        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN, batch))
        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN, batch))
        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN, batch))
        self.assertFalse(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN, batch))

        summary = limiter.finish_batch(batch)
        self.assertTrue(summary.has_suppressed)
        self.assertEqual(summary.total_suppressed, 1)
        self.assertEqual(
            summary.suppressed_counts,
            {RECEIVE_ACTION_KEY_URL_OPEN: 1},
        )

    def test_minute_limit_suppresses_until_window_expires(self):
        clock = _FakeClock()
        config = _FakeConfig(
            {},
            limits={
                RECEIVE_ACTION_KEY_URL_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 0,
                    RECEIVE_ACTION_LIMIT_MINUTE: 2,
                },
            },
        )
        limiter = ReceiveActionLimiter(config, clock=clock)

        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN))
        clock.advance(10)
        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN))
        clock.advance(10)
        self.assertFalse(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN))
        clock.advance(RECEIVE_ACTION_WINDOW_S - 20 + 0.01)
        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN))

    def test_zero_limits_are_unlimited(self):
        config = _FakeConfig(
            {},
            limits={
                RECEIVE_ACTION_KEY_TEXT_COPY: {
                    RECEIVE_ACTION_LIMIT_BATCH: 0,
                    RECEIVE_ACTION_LIMIT_MINUTE: 0,
                },
            },
        )
        limiter = ReceiveActionLimiter(config, clock=_FakeClock())
        batch = limiter.start_batch(50)

        for _i in range(50):
            self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_TEXT_COPY, batch))

        summary = limiter.finish_batch(batch)
        self.assertFalse(summary.has_suppressed)
        self.assertEqual(summary.suppressed_counts, {})

    def test_action_keys_have_independent_counts(self):
        config = _FakeConfig(
            {},
            limits={
                RECEIVE_ACTION_KEY_URL_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 1,
                    RECEIVE_ACTION_LIMIT_MINUTE: 1,
                },
                RECEIVE_ACTION_KEY_DOCUMENT_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 1,
                    RECEIVE_ACTION_LIMIT_MINUTE: 1,
                },
            },
        )
        limiter = ReceiveActionLimiter(config, clock=_FakeClock())
        batch = limiter.start_batch(4)

        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN, batch))
        self.assertFalse(limiter.allow(RECEIVE_ACTION_KEY_URL_OPEN, batch))
        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_DOCUMENT_OPEN, batch))
        self.assertFalse(limiter.allow(RECEIVE_ACTION_KEY_DOCUMENT_OPEN, batch))

        summary = limiter.finish_batch(batch)
        self.assertEqual(
            summary.suppressed_counts,
            {
                RECEIVE_ACTION_KEY_URL_OPEN: 1,
                RECEIVE_ACTION_KEY_DOCUMENT_OPEN: 1,
            },
        )

    def test_minute_suppression_is_recorded_in_batch_summary(self):
        config = _FakeConfig(
            {},
            limits={
                RECEIVE_ACTION_KEY_IMAGE_OPEN: {
                    RECEIVE_ACTION_LIMIT_BATCH: 0,
                    RECEIVE_ACTION_LIMIT_MINUTE: 1,
                },
            },
        )
        limiter = ReceiveActionLimiter(config, clock=_FakeClock())
        batch = limiter.start_batch(2)

        self.assertTrue(limiter.allow(RECEIVE_ACTION_KEY_IMAGE_OPEN, batch))
        self.assertFalse(limiter.allow(RECEIVE_ACTION_KEY_IMAGE_OPEN, batch))

        summary = limiter.finish_batch(batch)
        self.assertEqual(summary.batch_size, 2)
        self.assertEqual(summary.total_suppressed, 1)
        self.assertEqual(
            summary.suppressed_counts,
            {RECEIVE_ACTION_KEY_IMAGE_OPEN: 1},
        )

    def test_unknown_action_keys_are_unlimited(self):
        limiter = ReceiveActionLimiter(_FakeConfig({}), clock=_FakeClock())
        batch = limiter.start_batch(20)

        for _i in range(20):
            self.assertTrue(limiter.allow("unknown.open", batch))

        self.assertEqual(limiter.finish_batch(batch).suppressed_counts, {})


if __name__ == "__main__":
    unittest.main()
