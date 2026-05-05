"""Filesystem watcher → pending-ops queue (T10.4 / §H13).

Two pure-logic primitives + one orchestrator + an optional watchdog
adapter. The pure pieces let the test suite drive every edge case
deterministically (synthetic clock, synthetic stat values), and the
adapter only kicks in when the runtime has the ``watchdog`` package
available.

- ``EventDebouncer`` — coalesces rapid filesystem events on the same
  path inside a 500 ms window. The watcher fires far more often than
  the sync loop drains, so left raw, a single-file edit produces dozens
  of "modified" events. The debouncer hands the latest one through to
  the stability gate.

- ``StabilityGate`` — emits a "file is stable, you can upload it" signal
  only after ``(size, mtime)`` has been unchanged for ``stability_window
  _s``. Defaults: 3 s on local filesystems, 10 s on network shares
  (NFS / SMB / FUSE) where the metadata view is laggy. A 5-minute
  ``hung_after_s`` cap stops the gate from waiting forever on a file
  that never settles (a partial download from another tool, say).

- ``WatcherCoordinator`` — couples the two: ``observe(path, event)`` →
  debounce → stability → ``store.coalesce_op(...)`` once the file is
  ready. Tests poke the coordinator's ``tick(now)`` to drain
  ready/timed-out paths without any real time passing.

- ``start_watchdog_observer(...)`` — optional thin adapter over
  ``watchdog.observers.Observer``. Imported lazily so the module loads
  in environments without watchdog (e.g., the test container).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .vault_bindings import normalize_relative_path


log = logging.getLogger(__name__)


DEBOUNCE_WINDOW_S = 0.5
STABILITY_WINDOW_LOCAL_S = 3.0
STABILITY_WINDOW_NETWORK_S = 10.0
STABILITY_HUNG_AFTER_S = 5 * 60  # §H13 hung-detection cap

EventKind = Literal["created", "modified", "deleted", "moved"]


@dataclass
class _PendingPath:
    relative_path: str
    last_event_at: float                           # most recent observation time
    last_event_kind: EventKind
    first_event_at: float                          # to honor STABILITY_HUNG_AFTER_S
    last_size: int | None = None
    last_mtime_ns: int | None = None
    last_stable_check_at: float | None = None
    enqueued: bool = False


class EventDebouncer:
    """Coalesces events on the same path within a debounce window.

    ``observe(path, kind, now)`` returns True iff the caller should
    treat this event as the "fresh" trigger to start a stability
    measurement; subsequent events inside the window only refresh
    ``last_event_at`` so the file doesn't get checked while it's still
    actively changing.
    """

    def __init__(self, window_s: float = DEBOUNCE_WINDOW_S) -> None:
        self.window_s = float(window_s)
        self._last_seen: dict[str, float] = {}

    def observe(self, path: str, *, now: float) -> bool:
        previous = self._last_seen.get(path)
        self._last_seen[path] = now
        if previous is None:
            return True
        return (now - previous) >= self.window_s

    def forget(self, path: str) -> None:
        self._last_seen.pop(path, None)


@dataclass(frozen=True)
class StabilityVerdict:
    ready: bool
    timed_out: bool
    reason: str = ""


class StabilityGate:
    """Per-path stability tracker.

    ``check(size, mtime_ns, now, *, first_event_at)`` is called by the
    coordinator on each tick. Returns ``ready=True`` when the file's
    `(size, mtime_ns)` has been unchanged for at least
    ``window_s`` seconds; ``timed_out=True`` once the file's been
    "almost ready but kept moving" for longer than ``hung_after_s``.
    """

    def __init__(
        self,
        *,
        window_s: float = STABILITY_WINDOW_LOCAL_S,
        hung_after_s: float = STABILITY_HUNG_AFTER_S,
    ) -> None:
        self.window_s = float(window_s)
        self.hung_after_s = float(hung_after_s)
        self._snapshots: dict[str, tuple[int, int, float]] = {}
        # path → (size, mtime_ns, since_when_unchanged)

    def check(
        self,
        path: str,
        *,
        size: int,
        mtime_ns: int,
        now: float,
        first_event_at: float,
    ) -> StabilityVerdict:
        prior = self._snapshots.get(path)
        if prior is not None and prior[0] == size and prior[1] == mtime_ns:
            unchanged_for = now - prior[2]
            if unchanged_for >= self.window_s:
                return StabilityVerdict(ready=True, timed_out=False)
        else:
            self._snapshots[path] = (int(size), int(mtime_ns), now)
        if (now - first_event_at) >= self.hung_after_s:
            return StabilityVerdict(
                ready=False, timed_out=True,
                reason="hung_after_cap_exceeded",
            )
        return StabilityVerdict(ready=False, timed_out=False)

    def forget(self, path: str) -> None:
        self._snapshots.pop(path, None)


class _StoreSink:
    """Minimal protocol the coordinator expects from a sink."""

    def coalesce_op(self, *, binding_id: str, op_type: str, relative_path: str, now: int | None = None): ...


class WatcherCoordinator:
    """Glue between filesystem events and the bindings-store queue.

    Tests inject a fake clock (``clock`` callable) and a fake
    ``stat_provider(path) -> (size, mtime_ns) | None`` so they can
    drive the whole state machine without any real I/O.

    ``previously_synced`` (T12.2) is the per-path predicate the
    coordinator consults on a delete: only paths that were *previously
    uploaded successfully* may produce a tombstone. Files the user
    deletes before they ever flowed up (or "extra" files seeded by
    baseline with an empty fingerprint) flow silently. Default: always
    True, preserving the T10.4 acceptance shape.
    """

    def __init__(
        self,
        *,
        binding_id: str,
        local_root: Path,
        store: _StoreSink,
        is_network_share: bool = False,
        clock: Callable[[], float] | None = None,
        stat_provider: Callable[[str], tuple[int, int] | None] | None = None,
        previously_synced: Callable[[str], bool] | None = None,
    ) -> None:
        self.binding_id = binding_id
        self.local_root = Path(local_root)
        self.store = store
        self._clock = clock or time.monotonic
        self._stat_provider = stat_provider or self._real_stat
        self._previously_synced = previously_synced
        self._debouncer = EventDebouncer()
        window = (
            STABILITY_WINDOW_NETWORK_S
            if is_network_share else STABILITY_WINDOW_LOCAL_S
        )
        self._gate = StabilityGate(window_s=window)
        self._pending: dict[str, _PendingPath] = {}

    # --- ingress -------------------------------------------------------

    def observe(
        self,
        relative_path: str,
        *,
        kind: EventKind,
        now: float | None = None,
    ) -> None:
        """Receive a single filesystem event."""
        now_t = self._clock() if now is None else float(now)
        # F-Y16: NFC-normalize at the watcher boundary so the same
        # Czech-named file doesn't enqueue twice (NFD bytes from a Mac
        # plus NFC bytes from a Linux walker would otherwise key
        # distinct rows in vault_pending_operations).
        path = normalize_relative_path(relative_path)
        if not path:
            return

        if kind == "deleted":
            self._pending.pop(path, None)
            self._debouncer.forget(path)
            self._gate.forget(path)
            # F-Y22: persist the wall-clock timestamp regardless of the
            # monotonic clock injected for stability-gate math.
            self._enqueue_delete_if_synced(path, now=int(time.time()))
            return

        # Always update debouncer + pending-path bookkeeping; tick()
        # is what checks stability.
        is_fresh = self._debouncer.observe(path, now=now_t)
        existing = self._pending.get(path)
        if existing is None:
            self._pending[path] = _PendingPath(
                relative_path=path,
                last_event_at=now_t,
                last_event_kind=kind,
                first_event_at=now_t,
            )
        else:
            existing.last_event_at = now_t
            existing.last_event_kind = kind
            existing.enqueued = False  # reopen the gate if the file changed again
            # F-Y19: actively-edited files (e.g. KeePass long save) hit
            # the hung-after cap when first_event_at stays pinned to the
            # original event. Advance it so the cap counts "tried to
            # settle but failed" rather than "user is editing fast".
            existing.first_event_at = now_t

    # --- driver --------------------------------------------------------

    def tick(self, *, now: float | None = None) -> int:
        """Drain ready paths into the store; return how many were enqueued.

        Tests call this manually after advancing the clock; production
        wires it to a periodic ``GLib.timeout_add`` or thread loop.
        """
        now_t = self._clock() if now is None else float(now)
        enqueued = 0
        for path, pending in list(self._pending.items()):
            if pending.enqueued:
                continue
            stat = self._stat_provider(path)
            if stat is None:
                # Path vanished without us seeing a "deleted" event
                # (e.g. atomic rename overwrite). Treat as deletion,
                # gated on the §T12.2 "was previously synced" rule.
                self._pending.pop(path, None)
                self._debouncer.forget(path)
                self._gate.forget(path)
                if self._enqueue_delete_if_synced(path, now=int(now_t)):
                    enqueued += 1
                continue
            size, mtime_ns = stat
            verdict = self._gate.check(
                path,
                size=size, mtime_ns=mtime_ns,
                now=now_t, first_event_at=pending.first_event_at,
            )
            if verdict.timed_out:
                log.warning(
                    "vault.sync.file_stability_hung path=%s waited=%.1fs",
                    path, now_t - pending.first_event_at,
                )
                self._pending.pop(path, None)
                self._debouncer.forget(path)
                self._gate.forget(path)
                continue
            if not verdict.ready:
                continue
            self.store.coalesce_op(
                binding_id=self.binding_id,
                op_type="upload",
                relative_path=path,
                # F-Y22: use wall-clock for persistence (the column is
                # consumed by humans/timeline UIs); the monotonic clock
                # stays inside StabilityGate.
                now=int(time.time()),
            )
            pending.enqueued = True
            self._pending.pop(path, None)
            self._debouncer.forget(path)
            self._gate.forget(path)
            enqueued += 1
        return enqueued

    def pending_paths(self) -> list[str]:
        return list(self._pending.keys())

    # --- helpers -------------------------------------------------------

    def _enqueue_delete_if_synced(self, path: str, *, now: int) -> bool:
        """Enqueue a tombstone op only when the path was previously synced.

        Returns ``True`` iff the op was enqueued. T12.2: silent on
        never-synced paths so the user deleting a file before its first
        upload, or removing a baseline-extra file, never produces a
        remote tombstone.
        """
        if self._previously_synced is not None:
            try:
                synced = bool(self._previously_synced(path))
            except Exception:  # noqa: BLE001
                log.warning(
                    "vault.sync.previously_synced_check_failed binding=%s path=%s",
                    self.binding_id, path, exc_info=True,
                )
                synced = False
            if not synced:
                log.info(
                    "vault.sync.local_delete_unsynced_silent binding=%s path=%s",
                    self.binding_id, path,
                )
                return False
        self.store.coalesce_op(
            binding_id=self.binding_id,
            op_type="delete",
            relative_path=path,
            now=now,
        )
        return True

    def _real_stat(self, relative_path: str) -> tuple[int, int] | None:
        target = self.local_root / relative_path
        try:
            stat = target.stat()
        except OSError:
            return None
        return int(stat.st_size), int(stat.st_mtime_ns)


# ---------------------------------------------------------------------------
# Optional watchdog adapter
# ---------------------------------------------------------------------------


def start_watchdog_observer(
    coordinator: WatcherCoordinator,
    *,
    poll_interval_s: float = 1.0,
) -> "_WatchdogHandle | None":
    """Start a watchdog Observer thread that feeds ``coordinator``.

    Returns None if ``watchdog`` isn't installed — the caller can fall
    back to a polling-only mode or surface a warning. The returned
    handle's ``.stop()`` joins the observer thread.
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        log.warning(
            "vault.sync.watchdog_unavailable reason=python3-watchdog not installed"
        )
        return None

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:  # type: ignore[override]
            if event.is_directory:
                return
            try:
                relative = (
                    Path(event.src_path).resolve().relative_to(
                        coordinator.local_root.resolve()
                    ).as_posix()
                )
            except (ValueError, OSError):
                return
            kind: EventKind
            if event.event_type == "deleted":
                kind = "deleted"
            elif event.event_type == "moved":
                kind = "moved"
            elif event.event_type == "created":
                kind = "created"
            else:
                kind = "modified"
            coordinator.observe(relative, kind=kind)

    observer = Observer()
    observer.schedule(
        _Handler(),
        str(coordinator.local_root),
        recursive=True,
    )
    observer.start()
    return _WatchdogHandle(observer, poll_interval_s)


@dataclass
class _WatchdogHandle:
    observer: object
    poll_interval_s: float

    def stop(self) -> None:
        try:
            self.observer.stop()  # type: ignore[union-attr]
            self.observer.join(timeout=2)  # type: ignore[union-attr]
        except Exception:
            pass


def make_previously_synced_predicate(
    store: Any, binding_id: str,
) -> Callable[[str], bool]:
    """Build the ``previously_synced`` callable for a real bindings store.

    The predicate returns ``True`` iff there is a ``vault_local_entries``
    row for ``(binding_id, relative_path)`` *and* its ``content_fingerprint``
    is non-empty — i.e. the file was actually uploaded at some point. Rows
    seeded as "extras" by baseline (fingerprint = "") count as not-yet-
    synced and the watcher should silently ignore their deletion (T12.2).
    """
    def check(relative_path: str) -> bool:
        entry = store.get_local_entry(binding_id, relative_path)
        if entry is None:
            return False
        fingerprint = getattr(entry, "content_fingerprint", "") or ""
        return bool(fingerprint)
    return check


__all__ = [
    "DEBOUNCE_WINDOW_S",
    "EventDebouncer",
    "EventKind",
    "STABILITY_HUNG_AFTER_S",
    "STABILITY_WINDOW_LOCAL_S",
    "STABILITY_WINDOW_NETWORK_S",
    "StabilityGate",
    "StabilityVerdict",
    "WatcherCoordinator",
    "make_previously_synced_predicate",
    "start_watchdog_observer",
]
