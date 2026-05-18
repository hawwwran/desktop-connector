"""Wire-up layer for filesystem watchers + ransomware detectors (F-Y13).

Per-vault entry point invoked when the tray opens a vault: walks
``store.list_bindings(state="bound")`` and starts one watcher observer
per binding, with a shared :class:`RansomwareDetector` per binding. On
tripped verdicts the detector pauses the binding; the user reads the
§A15 banner before clicking Resume / Review.

The runtime keeps a small registry so the same vault can be re-opened
without leaking observer threads.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..atomic import sweep_orphan_temp_files
from .lifecycle import BindingCancellationRegistry, pause_binding
from .bindings import VaultBindingsStore
from .filesystem_watcher import (
    WatcherCoordinator,
    start_watchdog_observer,
)
from ..diagnostics.ransomware_detector import (
    BANNER_BODY,
    BANNER_TITLE,
    RansomwareDetector,
)


log = logging.getLogger(__name__)


@dataclass
class _BindingRuntime:
    binding_id: str
    coordinator: WatcherCoordinator
    detector: RansomwareDetector
    observer_handle: Any = None  # opaque handle returned by start_watchdog_observer
    paused_for_ransomware: bool = False


@dataclass
class VaultWatcherRuntime:
    """Live runtime for one open vault.

    Keeps watcher coordinators + ransomware detectors keyed by binding
    id. The same vault re-opening reuses the runtime; closing the vault
    stops every observer.
    """

    vault_id: str
    store: VaultBindingsStore
    # Review §3.C2: shared with the caller's sync cycle driver
    # (typically the tray's autosync loop). When the ransomware
    # detector trips, ``_on_tripped`` routes the pause through
    # ``pause_binding(..., cancellation=registry)`` so an in-flight
    # cycle observes the bail signal at its next checkpoint instead
    # of bleeding tombstones to completion.
    cancellation_registry: BindingCancellationRegistry | None = None
    bindings: dict[str, _BindingRuntime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _ransomware_callback: Callable[[str], None] | None = None

    def start_for_active_bindings(self) -> int:
        """Start observers for every binding currently in ``state="bound"``.

        Idempotent: already-started bindings are skipped. Returns the
        count of newly-started observers.

        Also drives a TTL-based sweep of orphan SO-3 batched-upload
        stubs (14-day default). Per-path and per-binding reapers cover
        the common cases; this catches stubs that escaped them — e.g.
        a file the user deleted *and* whose binding lost its pending-op
        row through some path that didn't run a per-path reap.
        """
        started = 0
        with self._lock:
            for binding in self.store.list_bindings(vault_id=self.vault_id):
                if binding.state != "bound" or binding.sync_mode == "paused":
                    continue
                if binding.binding_id in self.bindings:
                    continue
                runtime = self._start_one(binding)
                if runtime is not None:
                    self.bindings[binding.binding_id] = runtime
                    started += 1
        if started:
            log.info(
                "vault.sync.watchers_started vault=%s count=%d",
                self.vault_id, started,
            )
        try:
            from ..upload import (
                default_upload_resume_dir,
                reap_expired_sessions,
                reap_expired_stubs,
            )
            cache_dir = default_upload_resume_dir()
            reaped = reap_expired_stubs(cache_dir)
            if reaped:
                log.info(
                    "vault.sync.batch_stubs_ttl_reaped vault=%s count=%d",
                    self.vault_id, reaped,
                )
            session_reaped = reap_expired_sessions(cache_dir)
            if session_reaped:
                log.info(
                    "vault.sync.session_ttl_reaped vault=%s count=%d",
                    self.vault_id, session_reaped,
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.batch_stubs_ttl_reap_failed vault=%s",
                self.vault_id,
            )
        return started

    def _start_one(self, binding) -> _BindingRuntime | None:
        local_root = Path(binding.local_path)
        if not local_root.is_dir():
            log.warning(
                "vault.sync.watcher_skip_missing_root binding=%s path=%s",
                binding.binding_id, local_root,
            )
            return None
        try:
            sweep_orphan_temp_files(local_root)
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.atomic.sweep_failed binding=%s",
                binding.binding_id,
            )
        detector = RansomwareDetector(binding_id=binding.binding_id)
        coordinator = WatcherCoordinator(
            binding_id=binding.binding_id,
            local_root=local_root,
            store=self.store,
            previously_synced=lambda path,
                bid=binding.binding_id: self._previously_synced(bid, path),
        )
        wrapped_observe = coordinator.observe

        def observe_with_detector(
            relative_path: str,
            *,
            kind,
            now: float | None = None,
        ) -> None:
            # Review §3.H1: record the event in the detector FIRST, then
            # decide whether to forward to wrapped_observe. The pre-fix
            # ordering enqueued the delete/upload op BEFORE the detector
            # had a chance to trip, so the 200th malicious delete (or
            # any straw-that-breaks-the-camel's-back event) made it
            # into the pending-ops queue and was published as a
            # tombstone before pause_binding could stop the cycle.
            detector_kind = _to_detector_kind(kind)
            verdict = None
            if detector_kind is not None:
                verdict = detector.record(
                    kind=detector_kind,
                    path=relative_path,
                    now=now if now is not None else time.time(),
                )
                if verdict.tripped:
                    self._on_tripped(binding.binding_id)
                    # Do NOT forward — this event would have been the
                    # one that pushed us past the threshold; letting
                    # it into the queue defeats the trip.
                    return
            wrapped_observe(relative_path, kind=kind, now=now)

        coordinator.observe = observe_with_detector  # type: ignore[assignment]
        handle = start_watchdog_observer(coordinator)
        return _BindingRuntime(
            binding_id=binding.binding_id,
            coordinator=coordinator,
            detector=detector,
            observer_handle=handle,
        )

    def _previously_synced(self, binding_id: str, relative_path: str) -> bool:
        try:
            entry = self.store.get_local_entry(binding_id, relative_path)
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.previously_synced_check_failed binding=%s path=%s",
                binding_id, relative_path,
            )
            return False
        return bool(entry and entry.content_fingerprint)

    def _on_tripped(self, binding_id: str) -> None:
        with self._lock:
            runtime = self.bindings.get(binding_id)
            if runtime is None or runtime.paused_for_ransomware:
                return
            runtime.paused_for_ransomware = True
        try:
            # Review §3.C2: thread the cancellation registry into
            # pause_binding so an in-flight backup-only / two-way cycle
            # observes the bail signal before its next chunk/op checkpoint
            # and stops bleeding deletes/tombstones. pause_binding itself
            # calls registry.cancel(binding_id) prior to the DB state flip,
            # so the cycle's should_continue sees the trip the moment the
            # current chunk finishes.
            pause_binding(
                self.store,
                binding_id,
                cancellation=self.cancellation_registry,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.ransomware_pause_failed binding=%s", binding_id,
            )
            return
        log.warning(
            "vault.sync.ransomware_pause_triggered binding=%s title=%r body=%r",
            binding_id, BANNER_TITLE, BANNER_BODY,
        )
        cb = self._ransomware_callback
        if cb is not None:
            try:
                cb(binding_id)
            except Exception:  # noqa: BLE001
                log.exception(
                    "vault.sync.ransomware_callback_failed binding=%s",
                    binding_id,
                )

    def set_ransomware_callback(
        self, callback: Callable[[str], None] | None,
    ) -> None:
        """Wire a UI callback that fires once when a binding trips."""
        self._ransomware_callback = callback

    def stop_all(self) -> None:
        """Stop every observer thread; safe to call many times."""
        with self._lock:
            for runtime in list(self.bindings.values()):
                handle = runtime.observer_handle
                if handle is not None and hasattr(handle, "stop"):
                    try:
                        handle.stop()
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "vault.sync.watcher_stop_failed binding=%s",
                            runtime.binding_id,
                        )
            self.bindings.clear()

    def tick_all(self) -> None:
        """Drain stability timers for every coordinator (used by Sync now)."""
        with self._lock:
            for runtime in self.bindings.values():
                try:
                    runtime.coordinator.tick()
                except Exception:  # noqa: BLE001
                    log.exception(
                        "vault.sync.watcher_tick_failed binding=%s",
                        runtime.binding_id,
                    )


def _to_detector_kind(event_kind: str) -> str | None:
    """Map watcher event kinds to detector event kinds."""
    if event_kind == "modified":
        return "modify"
    if event_kind == "deleted":
        return "delete"
    if event_kind == "moved":
        return "rename"
    return None


__all__ = ["VaultWatcherRuntime"]
