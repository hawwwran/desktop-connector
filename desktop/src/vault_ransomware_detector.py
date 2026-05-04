"""Ransomware / mass-change detector for binding sync (T12.3, §A15).

Per §3.15 and the §T0 surface notes, every binding gets a sliding
5-minute counter of *touched* files (modify + delete + rename
events). When the counter crosses the configured threshold, the
binding flips to ``state = paused`` and the user gets the §A15 banner
with [Review] / [Rollback] / [Resume] / [Keep paused] actions.

Defaults match the spec:

- ``MAX_EVENTS_PER_WINDOW = 200``
- ``WINDOW_SECONDS = 300``  (5 minutes)
- ``RENAME_RATIO_THRESHOLD = 0.5`` over ``RENAME_RATIO_MIN_EVENTS = 20``

The detector is pure logic — production wires it into the watcher
(see :mod:`vault_filesystem_watcher`) and the sync-cycle drivers, so
deletion ops + edit ops both record an event before the queue is
drained. The pause itself is a state-machine transition handed to
:meth:`vault_bindings.VaultBindingsStore.update_binding_state`; the
detector reports the verdict + reason and lets the caller flip the
state, so tests can drive the detector with a synthetic clock and no
real store at all.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, Literal


log = logging.getLogger(__name__)


EventKind = Literal["modify", "delete", "rename"]


WINDOW_SECONDS = 300.0
MAX_EVENTS_PER_WINDOW = 200
RENAME_RATIO_MIN_EVENTS = 20
RENAME_RATIO_THRESHOLD = 0.5


# §A15 banner copy — used verbatim by the GTK Sync-safety panel and the
# tray notification text. Keeping them here (in the data layer) lets us
# unit-test "the surface text matches the spec" without driving GTK.
BANNER_TITLE = "Suspicious mass change detected"
BANNER_BODY = "Vault sync has been paused for this folder. Review changes before uploading."

# §T0 button vocabulary (decisions.md). Tests assert the exact list
# rather than the localized ordering, so a translation pass can move
# them around as long as every label is still represented.
ACTION_REVIEW = "Review changes"
ACTION_ROLLBACK = "Rollback to previous version"
ACTION_RESUME = "Resume sync"
ACTION_KEEP_PAUSED = "Keep paused"
BANNER_ACTIONS: tuple[str, ...] = (
    ACTION_REVIEW, ACTION_ROLLBACK, ACTION_RESUME, ACTION_KEEP_PAUSED,
)


@dataclass(frozen=True)
class DetectorThresholds:
    window_seconds: float = WINDOW_SECONDS
    max_events: int = MAX_EVENTS_PER_WINDOW
    rename_ratio_threshold: float = RENAME_RATIO_THRESHOLD
    rename_ratio_min_events: int = RENAME_RATIO_MIN_EVENTS


@dataclass(frozen=True)
class DetectorVerdict:
    tripped: bool
    reason: str  # "" when not tripped
    total_events: int
    rename_events: int


class RansomwareDetector:
    """Sliding-window mass-change detector for one binding.

    The detector is event-source-agnostic: any caller can hand it
    ``(now, kind, path)`` triples. The caller decides what to do with
    a tripped verdict — production flips ``binding_state = "paused"``
    via ``VaultBindingsStore.update_binding_state``.
    """

    def __init__(
        self,
        *,
        binding_id: str,
        thresholds: DetectorThresholds | None = None,
    ) -> None:
        self.binding_id = binding_id
        self.thresholds = thresholds or DetectorThresholds()
        self._events: Deque[tuple[float, EventKind, str]] = deque()

    def record(
        self, *, kind: EventKind, path: str, now: float,
    ) -> DetectorVerdict:
        """Append one event, evict expired, and report the current verdict."""
        if kind not in ("modify", "delete", "rename"):
            raise ValueError(f"unknown event kind: {kind!r}")
        self._evict(now)
        self._events.append((float(now), kind, str(path)))
        return self._verdict(now)

    def evict(self, *, now: float) -> None:
        """Drop events older than the window. Cheap to call from a tick."""
        self._evict(now)

    def verdict(self, *, now: float) -> DetectorVerdict:
        """Current verdict without recording a new event."""
        self._evict(now)
        return self._verdict(now)

    def reset(self) -> None:
        """Clear the sliding window — used when the user picks Resume."""
        self._events.clear()

    def event_count(self) -> int:
        return len(self._events)

    def _evict(self, now: float) -> None:
        cutoff = float(now) - self.thresholds.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _verdict(self, now: float) -> DetectorVerdict:
        total = len(self._events)
        renames = sum(1 for _, k, _ in self._events if k == "rename")
        if total >= self.thresholds.max_events:
            log.warning(
                "vault.sync.ransomware_threshold_total binding=%s total=%d window_s=%.0f",
                self.binding_id, total, self.thresholds.window_seconds,
            )
            return DetectorVerdict(
                tripped=True,
                reason=f"too_many_changes:{total}_in_{int(self.thresholds.window_seconds)}s",
                total_events=total,
                rename_events=renames,
            )
        if total >= self.thresholds.rename_ratio_min_events:
            ratio = renames / total
            if ratio >= self.thresholds.rename_ratio_threshold:
                log.warning(
                    "vault.sync.ransomware_threshold_rename_ratio "
                    "binding=%s total=%d renames=%d ratio=%.2f",
                    self.binding_id, total, renames, ratio,
                )
                return DetectorVerdict(
                    tripped=True,
                    reason=f"rename_ratio_high:{renames}/{total}",
                    total_events=total,
                    rename_events=renames,
                )
        return DetectorVerdict(
            tripped=False, reason="",
            total_events=total, rename_events=renames,
        )


def banner_action_for(label: str) -> str:
    """Identity helper that asserts the label is one of the §A15 actions."""
    if label not in BANNER_ACTIONS:
        raise ValueError(
            f"unknown ransomware-banner action: {label!r} "
            f"(expected one of {BANNER_ACTIONS})"
        )
    return label


__all__ = [
    "ACTION_KEEP_PAUSED",
    "ACTION_RESUME",
    "ACTION_REVIEW",
    "ACTION_ROLLBACK",
    "BANNER_ACTIONS",
    "BANNER_BODY",
    "BANNER_TITLE",
    "DetectorThresholds",
    "DetectorVerdict",
    "EventKind",
    "MAX_EVENTS_PER_WINDOW",
    "RENAME_RATIO_MIN_EVENTS",
    "RENAME_RATIO_THRESHOLD",
    "RansomwareDetector",
    "WINDOW_SECONDS",
    "banner_action_for",
]
