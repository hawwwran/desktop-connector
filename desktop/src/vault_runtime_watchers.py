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

from .vault_atomic import sweep_orphan_temp_files
from .vault_binding_lifecycle import pause_binding
from .vault_bindings import VaultBindingsStore
from .vault_filesystem_watcher import (
    WatcherCoordinator,
    start_watchdog_observer,
)
from .vault_ransomware_detector import (
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
    bindings: dict[str, _BindingRuntime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _ransomware_callback: Callable[[str], None] | None = None

    def start_for_active_bindings(self) -> int:
        """Start observers for every binding currently in ``state="bound"``.

        Idempotent: already-started bindings are skipped. Returns the
        count of newly-started observers.
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
            wrapped_observe(relative_path, kind=kind, now=now)
            detector_kind = _to_detector_kind(kind)
            if detector_kind is None:
                return
            verdict = detector.record(
                kind=detector_kind,
                path=relative_path,
                now=now if now is not None else time.time(),
            )
            if verdict.tripped:
                self._on_tripped(binding.binding_id)

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
            pause_binding(self.store, binding_id)
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
