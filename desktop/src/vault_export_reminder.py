"""Vault export-reminder cadence (T8.6 / §gaps §16).

The user is nudged to make a fresh export periodically. Default cadence
is monthly; configurable to ``off`` / ``weekly`` / ``monthly`` /
``quarterly`` / ``yearly`` in Vault settings → Recovery. Reminders are
dismissable per occurrence — dismissing snoozes for one full cadence
period, then the reminder reappears.

This module is pure logic: a clock-injectable predicate plus cadence
constants. UI surfacing lives in the vault settings / browser windows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal


Cadence = Literal["off", "weekly", "monthly", "quarterly", "yearly"]
CADENCE_DAYS: dict[Cadence, int | None] = {
    "off": None,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "yearly": 365,
}
DEFAULT_CADENCE: Cadence = "monthly"


def normalize_cadence(value: str | None) -> Cadence:
    """Map config strings (case-insensitive) to a known cadence; falls back to default."""
    if value is None:
        return DEFAULT_CADENCE
    key = str(value).strip().lower()
    if key in CADENCE_DAYS:
        return key  # type: ignore[return-value]
    return DEFAULT_CADENCE


def should_show_export_reminder(
    *,
    last_export_at: str | None,
    last_dismissed_at: str | None,
    cadence: str,
    now: str,
) -> bool:
    """Return True iff the export reminder banner should be visible.

    A reminder fires when **both** of these have been true for at least
    one full cadence period:
      1. No export has happened recently.
      2. The user hasn't dismissed the reminder recently.

    ``cadence == "off"`` disables the reminder unconditionally.
    """
    cad = normalize_cadence(cadence)
    days = CADENCE_DAYS[cad]
    if days is None:
        return False
    now_dt = _parse_iso(now)
    if now_dt is None:
        return False
    threshold = timedelta(days=days)

    if last_export_at:
        last_dt = _parse_iso(last_export_at)
        if last_dt is not None and (now_dt - last_dt) < threshold:
            return False
    if last_dismissed_at:
        last_dt = _parse_iso(last_dismissed_at)
        if last_dt is not None and (now_dt - last_dt) < threshold:
            return False
    return True


def next_reminder_due(
    *,
    last_export_at: str | None,
    last_dismissed_at: str | None,
    cadence: str,
) -> str | None:
    """Return the RFC3339 instant the reminder *would* re-fire, or None.

    Useful for "Reminder due in N days" copy without re-checking on
    every UI tick. Returns None when the cadence is ``off``.
    """
    cad = normalize_cadence(cadence)
    days = CADENCE_DAYS[cad]
    if days is None:
        return None
    delta = timedelta(days=days)
    candidates: list[datetime] = []
    for raw in (last_export_at, last_dismissed_at):
        when = _parse_iso(raw) if raw else None
        if when is not None:
            candidates.append(when + delta)
    if not candidates:
        # Never exported, never dismissed → due immediately.
        return _now_rfc3339()
    return max(candidates).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        when = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "CADENCE_DAYS",
    "Cadence",
    "DEFAULT_CADENCE",
    "next_reminder_due",
    "normalize_cadence",
    "should_show_export_reminder",
]
