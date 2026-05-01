"""Classification and safe post-receive actions for desktop receives."""

from __future__ import annotations

import logging
import mimetypes
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import (
    DEFAULT_RECEIVE_ACTIONS,
    DEFAULT_RECEIVE_ACTION_LIMITS,
    RECEIVE_ACTION_COPY,
    RECEIVE_ACTION_KEY_DOCUMENT_OPEN,
    RECEIVE_ACTION_KEY_IMAGE_OPEN,
    RECEIVE_ACTION_KEY_TEXT_COPY,
    RECEIVE_ACTION_KEY_URL_COPY,
    RECEIVE_ACTION_KEY_URL_OPEN,
    RECEIVE_ACTION_KEY_VIDEO_OPEN,
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

log = logging.getLogger(__name__)

RECEIVE_KIND_OTHER = "other"
RECEIVE_ACTION_WINDOW_S = 60.0

_URL_RE = re.compile(r"https?://\S+")
_TRAILING_URL_PUNCTUATION = ".,;:!?\"'"
_TRAILING_URL_DELIMITERS = {
    ")": "(",
    "]": "[",
    "}": "{",
}

_DOCUMENT_MIME_TYPES = {
    "application/msword",
    "application/pdf",
    "application/rtf",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/markdown",
    "text/plain",
}

_IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}

_VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}

_DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".md",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
}


@dataclass(frozen=True)
class ReceiveActionResult:
    """Outcome of one apply_receive_action / apply_receive_text_actions call.

    ``ok`` keeps the legacy bool semantics — falsy iff something went wrong
    (open returned False, clipboard write raised, …). Existing callers do
    ``if not apply_receive_action(...)`` and ``self.assertTrue(...)``; both
    keep working through ``__bool__``.

    ``action_ran`` is the new signal: True iff at least one configured
    action successfully took effect (URL opened, file launched, clipboard
    actually written). False when the configured action was ``none``,
    when the rate-limiter dropped it, or when the side effect failed.
    Notification suppression in the receiver gates on this — the
    user-visible action is enough of a "I just received something"
    signal, so an extra notification is noise.
    """
    ok: bool
    action_ran: bool

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class ReceiveActionBatch:
    """Counts action attempts for one pending-transfer poll result."""

    batch_size: int
    counts: dict[str, int] = field(default_factory=dict)
    suppressed_counts: dict[str, int] = field(default_factory=dict)

    def record_allowed(self, action_key: str) -> None:
        self.counts[action_key] = self.counts.get(action_key, 0) + 1

    def record_suppressed(self, action_key: str) -> None:
        self.suppressed_counts[action_key] = (
            self.suppressed_counts.get(action_key, 0) + 1
        )


@dataclass(frozen=True)
class ReceiveActionFloodSummary:
    batch_size: int
    suppressed_counts: dict[str, int]

    @property
    def total_suppressed(self) -> int:
        return sum(self.suppressed_counts.values())

    @property
    def has_suppressed(self) -> bool:
        return self.total_suppressed > 0


class ReceiveActionLimiter:
    """Applies per-batch and rolling-minute limits to receive side effects."""

    def __init__(self, config, *, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._clock = clock
        self._recent: dict[str, deque[float]] = {
            action_key: deque()
            for action_key in DEFAULT_RECEIVE_ACTION_LIMITS
        }

    def start_batch(self, batch_size: int) -> ReceiveActionBatch:
        return ReceiveActionBatch(batch_size=max(0, int(batch_size)))

    def allow(self, action_key: str,
              batch: ReceiveActionBatch | None = None) -> bool:
        if action_key not in DEFAULT_RECEIVE_ACTION_LIMITS:
            return True
        limits = self._limits_for(action_key)
        now = self._clock()
        recent = self._recent[action_key]
        self._prune_recent(recent, now)

        batch_limit = limits.get(RECEIVE_ACTION_LIMIT_BATCH, 0)
        if (
            batch is not None
            and batch_limit > 0
            and batch.counts.get(action_key, 0) >= batch_limit
        ):
            batch.record_suppressed(action_key)
            return False

        minute_limit = limits.get(RECEIVE_ACTION_LIMIT_MINUTE, 0)
        if minute_limit > 0 and len(recent) >= minute_limit:
            if batch is not None:
                batch.record_suppressed(action_key)
            return False

        recent.append(now)
        if batch is not None:
            batch.record_allowed(action_key)
        return True

    def finish_batch(self, batch: ReceiveActionBatch) -> ReceiveActionFloodSummary:
        return ReceiveActionFloodSummary(
            batch_size=batch.batch_size,
            suppressed_counts=dict(batch.suppressed_counts),
        )

    def _limits_for(self, action_key: str) -> dict[str, int]:
        getter = getattr(self.config, "get_receive_action_limits", None)
        if callable(getter):
            limits = getter(action_key)
        else:
            limits = getattr(self.config, "receive_action_limits", {}).get(
                action_key,
                DEFAULT_RECEIVE_ACTION_LIMITS.get(action_key, {}),
            )

        if not isinstance(limits, dict):
            limits = {}
        return {
            RECEIVE_ACTION_LIMIT_BATCH: self._limit_value(
                limits.get(
                    RECEIVE_ACTION_LIMIT_BATCH,
                    DEFAULT_RECEIVE_ACTION_LIMITS.get(action_key, {}).get(
                        RECEIVE_ACTION_LIMIT_BATCH, 0),
                )
            ),
            RECEIVE_ACTION_LIMIT_MINUTE: self._limit_value(
                limits.get(
                    RECEIVE_ACTION_LIMIT_MINUTE,
                    DEFAULT_RECEIVE_ACTION_LIMITS.get(action_key, {}).get(
                        RECEIVE_ACTION_LIMIT_MINUTE, 0),
                )
            ),
        }

    @staticmethod
    def _limit_value(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    @staticmethod
    def _prune_recent(recent: deque[float], now: float) -> None:
        cutoff = now - RECEIVE_ACTION_WINDOW_S
        while recent and recent[0] <= cutoff:
            recent.popleft()


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _clean_url_candidate(value: str) -> str:
    candidate = value
    while candidate:
        last = candidate[-1]
        if last in _TRAILING_URL_PUNCTUATION:
            candidate = candidate[:-1]
            continue
        opener = _TRAILING_URL_DELIMITERS.get(last)
        if opener and candidate.count(last) > candidate.count(opener):
            candidate = candidate[:-1]
            continue
        break
    return candidate


def classify_received_text(text: str) -> tuple[str | None, str | None]:
    """Return (kind, url) for exact URL text, otherwise (None, None)."""
    if not isinstance(text, str):
        return None, None

    candidate = text.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None, None

    if _valid_http_url(candidate):
        return RECEIVE_KIND_URL, candidate
    return None, None


def extract_received_urls(text: str) -> list[str]:
    """Return valid http(s) URLs found inside a text payload."""
    if not isinstance(text, str):
        return []

    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        candidate = _clean_url_candidate(match.group(0))
        if _valid_http_url(candidate):
            urls.append(candidate)
    return urls


def classify_received_file(path: Path) -> str:
    """Classify a received file for v1 receive-action handling."""
    p = Path(path)
    mime, _ = mimetypes.guess_type(p.name)
    if mime:
        mime = mime.lower()
        if mime.startswith("image/"):
            return RECEIVE_KIND_IMAGE
        if mime.startswith("video/"):
            return RECEIVE_KIND_VIDEO
        if mime in _DOCUMENT_MIME_TYPES:
            return RECEIVE_KIND_DOCUMENT

    suffix = p.suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return RECEIVE_KIND_IMAGE
    if suffix in _VIDEO_EXTENSIONS:
        return RECEIVE_KIND_VIDEO
    if suffix in _DOCUMENT_EXTENSIONS:
        return RECEIVE_KIND_DOCUMENT
    return RECEIVE_KIND_OTHER


def _configured_action(config, kind: str) -> str:
    getter = getattr(config, "get_receive_action", None)
    if callable(getter):
        action = getter(kind)
        if isinstance(action, str):
            return action
        return DEFAULT_RECEIVE_ACTIONS.get(kind, RECEIVE_ACTION_NONE)

    actions = getattr(config, "receive_actions", {})
    if isinstance(actions, dict):
        return actions.get(
            kind,
            DEFAULT_RECEIVE_ACTIONS.get(kind, RECEIVE_ACTION_NONE),
        )
    return DEFAULT_RECEIVE_ACTIONS.get(kind, RECEIVE_ACTION_NONE)


def _receive_action_key(kind: str, action: str) -> str | None:
    if kind == RECEIVE_KIND_URL and action == RECEIVE_ACTION_OPEN:
        return RECEIVE_ACTION_KEY_URL_OPEN
    if kind == RECEIVE_KIND_URL and action == RECEIVE_ACTION_COPY:
        return RECEIVE_ACTION_KEY_URL_COPY
    if kind == RECEIVE_KIND_TEXT and action == RECEIVE_ACTION_COPY:
        return RECEIVE_ACTION_KEY_TEXT_COPY
    if kind == RECEIVE_KIND_IMAGE and action == RECEIVE_ACTION_OPEN:
        return RECEIVE_ACTION_KEY_IMAGE_OPEN
    if kind == RECEIVE_KIND_VIDEO and action == RECEIVE_ACTION_OPEN:
        return RECEIVE_ACTION_KEY_VIDEO_OPEN
    if kind == RECEIVE_KIND_DOCUMENT and action == RECEIVE_ACTION_OPEN:
        return RECEIVE_ACTION_KEY_DOCUMENT_OPEN
    return None


def _action_allowed(
    limiter: ReceiveActionLimiter | None,
    batch: ReceiveActionBatch | None,
    action_key: str | None,
) -> bool:
    if limiter is None or action_key is None:
        return True
    allowed = limiter.allow(action_key, batch)
    if not allowed:
        log.info("receive_action.suppressed action_key=%s", action_key)
    return allowed


def _flush_pending_clipboard(platform, pending_clipboard: list[str | None]) -> bool:
    value = pending_clipboard[0]
    if value is None:
        return True
    try:
        return bool(platform.clipboard.write_text(value))
    except Exception as e:
        log.warning("receive_action.clipboard_failed error_kind=%s", type(e).__name__)
        return False


def _run_receive_action(
    config,
    platform,
    kind: str,
    *,
    url: str | None = None,
    text: str | None = None,
    path: Path | None = None,
    pending_clipboard: list[str | None],
    limiter: ReceiveActionLimiter | None = None,
    batch: ReceiveActionBatch | None = None,
) -> tuple[bool, bool]:
    """Returns ``(ok, attempted)``.

    ``attempted`` is True iff a real action passed the configured /
    rate-limit gates and reached the dispatch try-block — i.e. we
    actually fired ``open_url`` / ``open_path`` or staged a clipboard
    write. ``RECEIVE_ACTION_NONE`` and rate-limited drops keep
    ``attempted=False`` so the caller can still notify.
    """
    action = _configured_action(config, kind)
    if action == RECEIVE_ACTION_NONE:
        return True, False

    action_key = _receive_action_key(kind, action)
    if not _action_allowed(limiter, batch, action_key):
        return True, False

    try:
        if kind == RECEIVE_KIND_URL and action == RECEIVE_ACTION_OPEN and url:
            return bool(platform.shell.open_url(url)), True
        if kind == RECEIVE_KIND_URL and action == RECEIVE_ACTION_COPY and url:
            pending_clipboard[0] = url
            return True, True
        if (
            kind == RECEIVE_KIND_TEXT
            and action == RECEIVE_ACTION_COPY
            and text is not None
        ):
            pending_clipboard[0] = text
            return True, True
        if (
            kind in (RECEIVE_KIND_IMAGE, RECEIVE_KIND_VIDEO, RECEIVE_KIND_DOCUMENT)
            and action == RECEIVE_ACTION_OPEN
            and path is not None
        ):
            return bool(platform.shell.open_path(Path(path))), True

        log.warning(
            "receive_action.unsupported kind=%s action=%s has_url=%s has_path=%s",
            kind,
            action,
            url is not None,
            path is not None,
        )
        return False, True
    except Exception as e:
        log.warning(
            "receive_action.failed kind=%s action=%s error_kind=%s",
            kind,
            action,
            type(e).__name__,
        )
        return False, True


def apply_receive_action(
    config,
    platform,
    kind: str,
    *,
    url: str | None = None,
    text: str | None = None,
    path: Path | None = None,
    limiter: ReceiveActionLimiter | None = None,
    batch: ReceiveActionBatch | None = None,
) -> ReceiveActionResult:
    """Run one configured safe built-in receive action.

    The action is best-effort: failures are logged and reflected as
    ``ok=False`` on the result, but exceptions never escape into the
    receive loop. ``action_ran`` is True iff a configured action passed
    the rate-limit gate AND its side effect completed (open succeeded
    or clipboard write flushed).
    """
    pending_clipboard: list[str | None] = [None]
    ok, attempted = _run_receive_action(
        config,
        platform,
        kind,
        url=url,
        text=text,
        path=path,
        pending_clipboard=pending_clipboard,
        limiter=limiter,
        batch=batch,
    )
    flushed = _flush_pending_clipboard(platform, pending_clipboard)
    final_ok = bool(flushed and ok)
    return ReceiveActionResult(ok=final_ok, action_ran=attempted and final_ok)


def apply_receive_text_actions(
    config,
    platform,
    text: str,
    *,
    limiter: ReceiveActionLimiter | None = None,
    batch: ReceiveActionBatch | None = None,
) -> ReceiveActionResult:
    """Apply URL/text actions for a received text payload.

    Exact single-URL text runs only the URL action. Text that merely
    contains a URL runs the URL action for the first detected URL and
    then the text action for the full payload. Clipboard writes are
    staged and flushed once after all actions are evaluated.

    ``action_ran`` is True iff at least one of the two possible actions
    (URL + text) actually fired and succeeded — used by the receiver
    to suppress the redundant "Clipboard received" notification when
    the user already saw the action effect.
    """
    pending_clipboard: list[str | None] = [None]
    ok = True
    any_attempted = False

    kind, exact_url = classify_received_text(text)
    if kind == RECEIVE_KIND_URL and exact_url is not None:
        url_ok, url_attempted = _run_receive_action(
            config,
            platform,
            RECEIVE_KIND_URL,
            url=exact_url,
            pending_clipboard=pending_clipboard,
            limiter=limiter,
            batch=batch,
        )
        ok = url_ok and ok
        any_attempted = any_attempted or url_attempted
    else:
        urls = extract_received_urls(text)
        if urls:
            url_ok, url_attempted = _run_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url=urls[0],
                pending_clipboard=pending_clipboard,
                limiter=limiter,
                batch=batch,
            )
            ok = url_ok and ok
            any_attempted = any_attempted or url_attempted
        text_ok, text_attempted = _run_receive_action(
            config,
            platform,
            RECEIVE_KIND_TEXT,
            text=text,
            pending_clipboard=pending_clipboard,
            limiter=limiter,
            batch=batch,
        )
        ok = text_ok and ok
        any_attempted = any_attempted or text_attempted

    flushed = _flush_pending_clipboard(platform, pending_clipboard)
    final_ok = bool(flushed and ok)
    return ReceiveActionResult(ok=final_ok, action_ran=any_attempted and final_ok)
