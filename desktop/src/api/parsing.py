"""Free helpers for reading delivery hints out of HTTP error responses."""

import requests


def _parse_retry_after_ms(resp: "requests.Response") -> int:
    """Read the server-suggested retry delay from a 425 response.

    Preference order: body `retry_after_ms` (ms precision) → header
    `Retry-After` (seconds) → default 1000 ms. Server emits both; mobile
    / desktop tooling only reliably reads headers, so we accept either.
    """
    default_ms = 1000
    try:
        body = resp.json()
        if isinstance(body, dict):
            ms = body.get("retry_after_ms")
            if isinstance(ms, int) and ms > 0:
                return ms
    except (ValueError, AttributeError):
        pass
    header = resp.headers.get("Retry-After")
    if header:
        try:
            secs = int(header)
            if secs > 0:
                return secs * 1000
        except ValueError:
            pass
    return default_ms


def _extract_abort_reason(resp: "requests.Response") -> str | None:
    """Read `abort_reason` from a 410 body, if present."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            reason = body.get("abort_reason")
            if isinstance(reason, str) and reason:
                return reason
    except (ValueError, AttributeError):
        pass
    return None
