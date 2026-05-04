"""Multi-device propagation of a committed migration (T9.5).

When a device fetches ``GET /api/vaults/{id}/header`` and the response
carries ``migrated_to``, the §H2 contract is:

1. Switch the device's active relay URL to the target.
2. Save the previous relay URL with a 7-day grace period — the user
   can still "Switch back to previous relay" within that window if
   the migration turns out to have been a mistake.
3. After 7 days, drop the previous URL.

This module is pure logic — Config writes happen via the helpers in
:mod:`config`. Hook this from any code path that has a fresh header
response in hand (browser refresh, settings load, manifest fetch).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .vault_migration import PREVIOUS_RELAY_GRACE_DAYS


@dataclass(frozen=True)
class PropagationDecision:
    """What the caller should do with the device's relay config."""

    should_switch: bool
    new_relay_url: str | None = None
    previous_relay_url: str | None = None
    previous_relay_expires_at: str | None = None
    reason: str = ""


def propagate_relay_migration(
    *,
    header_data: dict[str, Any],
    current_relay_url: str,
    now: str | None = None,
) -> PropagationDecision:
    """Inspect a ``GET /header`` response and decide whether to swap relays.

    Returns ``should_switch=True`` when:
      - the server-side header has ``migrated_to`` set (source relay
        flipped read-only post-commit), and
      - the current device is still pointed at the source.

    The 7-day expiry is computed from "now" rather than the server's
    ``migrated_at`` so the grace window starts from the moment *this
    device* learns of the migration. (Server-side ``migrated_at`` is
    already days old by the time other devices catch up; if we anchored
    on it, devices could miss the rollback window entirely.)
    """
    migrated_to = str(header_data.get("migrated_to") or "").strip()
    if not migrated_to:
        return PropagationDecision(should_switch=False)
    if migrated_to == str(current_relay_url or "").strip():
        # Already pointed at the target — nothing to do.
        return PropagationDecision(
            should_switch=False,
            reason="already_on_target",
        )

    when = _parse_iso(now) if now else datetime.now(timezone.utc)
    expires = when + timedelta(days=PREVIOUS_RELAY_GRACE_DAYS)
    return PropagationDecision(
        should_switch=True,
        new_relay_url=migrated_to,
        previous_relay_url=str(current_relay_url),
        previous_relay_expires_at=expires.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        reason="migrated_to_set",
    )


def can_switch_back(
    *,
    previous_relay_url: str | None,
    previous_relay_expires_at: str | None,
    now: str | None = None,
) -> bool:
    """True iff "Switch back to previous relay" is still in its grace window."""
    if not previous_relay_url:
        return False
    if not previous_relay_expires_at:
        return False
    expiry = _parse_iso(previous_relay_expires_at)
    if expiry is None:
        return False
    when = _parse_iso(now) if now else datetime.now(timezone.utc)
    return when < expiry


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


__all__ = [
    "PropagationDecision",
    "can_switch_back",
    "propagate_relay_migration",
]
