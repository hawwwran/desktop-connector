"""Local-timezone display formatter for Vault UI surfaces.

Manifest entries store timestamps in RFC-3339 UTC (e.g.
``2026-05-07T11:00:47.000Z``). That format is right for the wire but
wrong for humans: timezone math in the user's head plus a noisy
``.000Z`` tail. UI surfaces should pass every manifest timestamp
through :func:`format_local` to render
``YYYY-MM-DD HH:MM:SS`` in the device's local timezone.

The transform is display-only — the manifest is never mutated.
Defensive: a malformed string passes through unchanged so a single
bad entry never blanks a row.
"""

from __future__ import annotations

from datetime import datetime, timezone


_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_local(value: object) -> str:
    """Render an RFC-3339 timestamp in the device's local timezone.

    Returns ``""`` for empty / ``None`` input. Returns the original
    string unchanged if parsing fails.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    parsed = _parse_rfc3339(text)
    if parsed is None:
        return text
    return parsed.astimezone().strftime(_DISPLAY_FORMAT)


def _parse_rfc3339(text: str) -> datetime | None:
    candidate = text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = ["format_local"]
