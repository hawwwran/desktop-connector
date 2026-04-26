"""Classification and safe post-receive actions for desktop receives."""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

from .config import (
    DEFAULT_RECEIVE_ACTIONS,
    RECEIVE_ACTION_COPY,
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

_URL_RE = re.compile(r"https?://\S+")
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}\"'"

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


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _clean_url_candidate(value: str) -> str:
    return value.rstrip(_TRAILING_URL_PUNCTUATION)


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
) -> bool:
    action = _configured_action(config, kind)
    if action == RECEIVE_ACTION_NONE:
        return True

    try:
        if kind == RECEIVE_KIND_URL and action == RECEIVE_ACTION_OPEN and url:
            return bool(platform.shell.open_url(url))
        if kind == RECEIVE_KIND_URL and action == RECEIVE_ACTION_COPY and url:
            pending_clipboard[0] = url
            return True
        if (
            kind == RECEIVE_KIND_TEXT
            and action == RECEIVE_ACTION_COPY
            and text is not None
        ):
            pending_clipboard[0] = text
            return True
        if (
            kind in (RECEIVE_KIND_IMAGE, RECEIVE_KIND_VIDEO, RECEIVE_KIND_DOCUMENT)
            and action == RECEIVE_ACTION_OPEN
            and path is not None
        ):
            return bool(platform.shell.open_path(Path(path)))

        log.warning(
            "receive_action.unsupported kind=%s action=%s has_url=%s has_path=%s",
            kind,
            action,
            url is not None,
            path is not None,
        )
        return False
    except Exception as e:
        log.warning(
            "receive_action.failed kind=%s action=%s error_kind=%s",
            kind,
            action,
            type(e).__name__,
        )
        return False


def apply_receive_action(
    config,
    platform,
    kind: str,
    *,
    url: str | None = None,
    text: str | None = None,
    path: Path | None = None,
) -> bool:
    """Run one configured safe built-in receive action.

    The action is best-effort: failures are logged and returned as False,
    but exceptions never escape into the receive loop.
    """
    pending_clipboard: list[str | None] = [None]
    ok = _run_receive_action(
        config,
        platform,
        kind,
        url=url,
        text=text,
        path=path,
        pending_clipboard=pending_clipboard,
    )
    return _flush_pending_clipboard(platform, pending_clipboard) and ok


def apply_receive_text_actions(config, platform, text: str) -> bool:
    """Apply URL/text actions for a received text payload.

    Exact single-URL text runs only the URL action. Text that merely
    contains a URL runs the URL action for the first detected URL and
    then the text action for the full payload. Clipboard writes are
    staged and flushed once after all actions are evaluated.
    """
    pending_clipboard: list[str | None] = [None]
    ok = True

    kind, exact_url = classify_received_text(text)
    if kind == RECEIVE_KIND_URL and exact_url is not None:
        ok = _run_receive_action(
            config,
            platform,
            RECEIVE_KIND_URL,
            url=exact_url,
            pending_clipboard=pending_clipboard,
        ) and ok
    else:
        urls = extract_received_urls(text)
        if urls:
            ok = _run_receive_action(
                config,
                platform,
                RECEIVE_KIND_URL,
                url=urls[0],
                pending_clipboard=pending_clipboard,
            ) and ok
        ok = _run_receive_action(
            config,
            platform,
            RECEIVE_KIND_TEXT,
            text=text,
            pending_clipboard=pending_clipboard,
        ) and ok

    return _flush_pending_clipboard(platform, pending_clipboard) and ok
