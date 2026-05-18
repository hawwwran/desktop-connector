"""Pause / Resume / Disconnect transitions for a binding (T12.4 + T12.5).

A binding's *state* and *sync mode* are independent axes (§A12):

- ``state`` is one of ``needs-preflight``, ``bound``, ``paused``,
  ``unbound``. The sync cycle refuses to run unless ``state == "bound"``.
- ``sync_mode`` is one of ``backup-only``, ``two-way``, ``download-only``,
  ``paused``. ``state="paused"`` keeps the sync_mode unchanged so resume
  can restore the same direction without asking the user.

T12.4 — :func:`pause_binding` flips ``state`` to ``paused`` while
preserving ``sync_mode`` and the pending-ops queue. The watcher should
keep accumulating ops while paused so resume picks up where the user
left off. :func:`resume_binding` flips back to ``bound`` and
delegates the actual flush to the configured cycle runner.

T12.5 — :func:`disconnect_binding` drops the ``vault_bindings`` row
but **leaves ``vault_local_entries`` intact** so subsequent restore /
re-connect can use those rows for fast change detection. The local
filesystem is left untouched; the remote vault is left untouched.
The folder remains browsable via the Browser mode.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .bindings import VaultBinding, VaultBindingsStore


log = logging.getLogger(__name__)


# F-Y30: cap per-op audit lines emitted on disconnect to keep the
# rotating local log readable when a binding has thousands of pending
# ops queued. The cap is high enough that real-world queues
# (watcher-driven, 5-minute window) emit every op verbatim.
DISCONNECT_AUDIT_LOG_CAP = 200


# ---------------------------------------------------------------------------
# F-Y08 — cooperative cancellation primitives
# ---------------------------------------------------------------------------


class SyncCancelledError(RuntimeError):
    """Raised inside a sync cycle (cycle driver or chunk PUT loop) when
    a registered cancellation event fires.

    Cycle drivers translate this into a ``status="cancelled"`` outcome
    on the in-flight op (the op stays queued for the next cycle) and
    flag the cycle's :class:`SyncCycleResult.cancelled` so callers can
    distinguish a cooperative bail from a true completion.
    """


class VaultDisconnectHasPendingOpsError(RuntimeError):
    """Raised by :func:`disconnect_binding` when the binding has unsynced
    pending ops and ``force=False``.

    Review §3.M5 — pre-fix disconnect silently dropped every pending op
    (with per-op audit logs, but no user-facing surface). A user
    disconnecting a binding mid-burst would lose the queued uploads
    without any indication. The UI now catches this typed error, shows
    the pending count, and offers "Sync first" / "Disconnect anyway"
    so the drop is deliberate.

    Carries ``pending_count`` so the UI can render
    "N changes will be dropped" without re-querying the store.
    """

    def __init__(self, *, binding_id: str, pending_count: int) -> None:
        self.binding_id = str(binding_id)
        self.pending_count = int(pending_count)
        super().__init__(
            f"binding {self.binding_id!r} has {self.pending_count} pending "
            "ops; pass force=True to disconnect and drop them, or flush "
            "the sync queue first"
        )


class BindingCancellationRegistry:
    """Thread-safe map of ``binding_id -> threading.Event`` used to
    coordinate Pause / Disconnect with in-flight sync cycles (F-Y08).

    The runtime owns one registry. Cycle drivers ``register()`` on
    entry, derive ``should_continue = lambda: not event.is_set()``,
    and ``clear()`` on exit. Lifecycle calls (``pause_binding`` /
    ``disconnect_binding``) call ``cancel(binding_id)`` before
    flipping state — any in-flight cycle observes the event and
    returns early instead of running the queue to completion against
    a binding the user just asked to stop.

    A registry is optional; passing ``should_continue`` directly is
    equally supported (and is what most tests do).
    """

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register(self, binding_id: str) -> threading.Event:
        """Create or replace the cancellation event for ``binding_id``.

        Returning a freshly-cleared event lets back-to-back cycles
        re-use the same registry without re-cancelling on the second
        run; the caller should pair every ``register`` with a
        ``clear`` once the cycle ends (typically in ``finally``).
        """
        event = threading.Event()
        with self._lock:
            self._events[binding_id] = event
        return event

    def cancel(self, binding_id: str) -> bool:
        """Set the event registered for ``binding_id`` if any.

        Returns ``True`` when a registered event was tripped, ``False``
        when no in-flight cycle is registered (the caller may still
        proceed with the lifecycle transition; this is just a signal
        for whether anything was actively running).
        """
        with self._lock:
            event = self._events.get(binding_id)
        if event is None:
            return False
        event.set()
        return True

    def clear(self, binding_id: str) -> None:
        """Drop the cancellation event for ``binding_id``."""
        with self._lock:
            self._events.pop(binding_id, None)

    def is_registered(self, binding_id: str) -> bool:
        with self._lock:
            return binding_id in self._events


@dataclass
class PauseResult:
    binding: VaultBinding
    pending_ops_preserved: int


@dataclass
class ResumeResult:
    binding: VaultBinding
    flushed: Any  # SyncCycleResult | None — the cycle's own summary


@dataclass
class DisconnectResult:
    binding_id: str
    local_entries_preserved: int
    pending_ops_dropped: int


def pause_binding(
    store: VaultBindingsStore,
    binding_id: str,
    *,
    cancellation: BindingCancellationRegistry | None = None,
) -> PauseResult:
    """Transition a bound binding into ``state="paused"``.

    Pending ops are preserved verbatim (§A12). The sync_mode field
    is unchanged so resume restores the same direction without asking
    the user. No-op if already paused.

    F-Y08: if a ``cancellation`` registry is supplied, any in-flight
    cycle for this binding is signalled to abort *before* the state
    flip, so the cycle observes the bail signal at its next chunk /
    op checkpoint. The state transition itself does not block on the
    cycle exiting; the next checkpoint cleans up and the worker
    thread joins on its own schedule.
    """
    binding = _require_binding(store, binding_id)
    if binding.state == "paused":
        # F-Y24: event name already encodes the noop; drop the redundant
        # already_paused suffix so the field set matches the catalog.
        log.info(
            "vault.sync.binding_pause_noop binding=%s",
            binding_id,
        )
        if cancellation is not None:
            cancellation.cancel(binding_id)
        return PauseResult(
            binding=binding,
            pending_ops_preserved=len(store.list_pending_ops(binding_id)),
        )
    if binding.state != "bound":
        raise ValueError(
            f"binding {binding_id!r} is in state {binding.state!r}; "
            "pause requires state=='bound'"
        )

    if cancellation is not None:
        cancelled = cancellation.cancel(binding_id)
        if cancelled:
            log.info(
                "vault.sync.binding_pause_cancelled_inflight_cycle binding=%s",
                binding_id,
            )
    store.update_binding_state(binding_id, state="paused")
    paused = store.get_binding(binding_id) or binding
    pending = len(store.list_pending_ops(binding_id))
    log.info(
        "vault.sync.binding_paused binding=%s sync_mode=%s pending_ops=%d",
        binding_id, paused.sync_mode, pending,
    )
    return PauseResult(binding=paused, pending_ops_preserved=pending)


def resume_binding(
    store: VaultBindingsStore,
    binding_id: str,
    *,
    flush: Callable[[VaultBinding], Any] | None = None,
) -> ResumeResult:
    """Transition a paused binding back to ``state="bound"`` and flush.

    ``flush`` runs after the state transition with the now-bound
    binding. Production passes a closure over the right cycle (
    backup-only / two-way) keyed off ``binding.sync_mode``. Tests can
    pass ``None`` to skip the actual flush and just observe the
    state transition.
    """
    binding = _require_binding(store, binding_id)
    if binding.state == "bound":
        # F-Y24: event name already encodes the noop.
        log.info(
            "vault.sync.binding_resume_noop binding=%s",
            binding_id,
        )
        flushed = flush(binding) if flush is not None else None
        return ResumeResult(binding=binding, flushed=flushed)
    if binding.state != "paused":
        raise ValueError(
            f"binding {binding_id!r} is in state {binding.state!r}; "
            "resume requires state=='paused'"
        )
    if binding.sync_mode == "paused":
        # Resume keeps sync_mode set per §A12; if a caller previously
        # set sync_mode to "paused" via the older API path, resume to
        # the default direction so we don't loop right back.
        raise ValueError(
            f"binding {binding_id!r} sync_mode=='paused'; "
            "set a real direction (backup-only / two-way) before resume"
        )

    store.update_binding_state(binding_id, state="bound")
    bound = store.get_binding(binding_id) or binding
    log.info(
        "vault.sync.binding_resumed binding=%s sync_mode=%s pending_ops=%d",
        binding_id, bound.sync_mode, len(store.list_pending_ops(binding_id)),
    )

    flushed = flush(bound) if flush is not None else None
    return ResumeResult(binding=bound, flushed=flushed)


def disconnect_binding(
    store: VaultBindingsStore,
    binding_id: str,
    *,
    cancellation: BindingCancellationRegistry | None = None,
    force: bool = False,
) -> DisconnectResult:
    """Flip ``state="unbound"``; preserve ``vault_local_entries`` (T12.5).

    Per §gaps §20 disconnect leaves the local filesystem untouched and
    the remote vault untouched — the user may re-connect later, and
    the preserved ``vault_local_entries`` rows speed up the next
    preflight. The schema's ON DELETE CASCADE would wipe local_entries
    if the binding row was hard-deleted, so we keep the row and just
    mark it ``unbound``. Subsequent sync cycles refuse (state != bound),
    so no traffic leaves the device. Pending ops are dropped — without
    an active binding nothing will flush them, and stale entries would
    re-fire on a future re-connect.

    Review §3.M5: refuse to disconnect when pending ops exist UNLESS
    the caller passes ``force=True``. Pre-fix any disconnect silently
    dropped the queue; a user disconnecting mid-burst would lose
    queued uploads with no indication. Now the typed
    :class:`VaultDisconnectHasPendingOpsError` is raised; the UI
    catches it, shows the count, and either flushes the queue first
    or re-calls with ``force=True`` after explicit user confirmation.
    The audit-log + stub-reap shape is unchanged when force is True.

    F-Y08: ``cancellation`` (if provided) signals any in-flight cycle
    on this binding to stop before the state flip lands; otherwise the
    cycle would happily run a chunk loop against a freshly-unbound
    binding for up to one whole pass.

    The user GC path (a future "Forget local index" button, §gaps §20)
    is what physically removes the row; until then the local_entries
    rows survive for fast change detection on reconnect.
    """
    binding = _require_binding(store, binding_id)
    if binding.state == "unbound":
        # F-Y24: event name already encodes the noop.
        log.info(
            "vault.sync.binding_disconnect_noop binding=%s",
            binding_id,
        )
        if cancellation is not None:
            cancellation.cancel(binding_id)
        return DisconnectResult(
            binding_id=binding_id,
            local_entries_preserved=len(store.list_local_entries(binding_id)),
            pending_ops_dropped=0,
        )
    if cancellation is not None:
        if cancellation.cancel(binding_id):
            log.info(
                "vault.sync.binding_disconnect_cancelled_inflight_cycle binding=%s",
                binding_id,
            )
    local_count = len(store.list_local_entries(binding_id))
    pending = store.list_pending_ops(binding_id)
    if pending and not force:
        # Review §3.M5 — refuse silently dropping the queue. Caller
        # must explicitly opt in via ``force=True`` so the data loss
        # is deliberate.
        log.info(
            "vault.sync.binding_disconnect_refused_pending binding=%s "
            "pending=%d",
            binding_id, len(pending),
        )
        raise VaultDisconnectHasPendingOpsError(
            binding_id=binding_id, pending_count=len(pending),
        )
    # F-Y30: per-op audit trail for what disconnect drops. With a 200-op
    # queue this prints 200 lines into the rotating local log — that's
    # acceptable; the alternative is a "dropped 200 ops" summary that
    # gives the user no recourse if a specific op was important. Capped
    # at DISCONNECT_AUDIT_LOG_CAP to keep log volume sane in pathological
    # cases; the summary line below records the full count regardless.
    for op in pending[:DISCONNECT_AUDIT_LOG_CAP]:
        log.info(
            "vault.sync.binding_disconnect_dropping_op binding=%s "
            "op_type=%s path=%s attempts=%d",
            binding_id, op.op_type, op.relative_path, op.attempts,
        )
    if len(pending) > DISCONNECT_AUDIT_LOG_CAP:
        log.info(
            "vault.sync.binding_disconnect_dropping_op_truncated binding=%s "
            "logged=%d total=%d",
            binding_id, DISCONNECT_AUDIT_LOG_CAP, len(pending),
        )
    for op in pending:
        store.delete_pending_op(op.op_id)
    # SO-3 dedupe stubs for these dropped ops are orphans now — no one
    # will retry them, so reap them here to keep ``<cache_dir>/batched/``
    # from growing monotonically across disconnect+reconnect churn.
    # Per-path reaping is bounded by the pending-ops queue and avoids
    # over-reaping when the same vault has another active binding (a
    # rare multi-folder setup). Stubs for paths the user just deleted
    # but never queued land in the next prep's stale-fingerprint reap
    # already; we don't need to chase them here.
    if pending:
        from ..upload import default_upload_resume_dir, reap_stubs_for_path

        stub_dir = default_upload_resume_dir()
        reaped_stubs = 0
        for op in pending:
            try:
                reaped_stubs += reap_stubs_for_path(
                    vault_id=binding.vault_id,
                    remote_path=op.relative_path,
                    cache_dir=stub_dir,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "vault.sync.binding_disconnect_stub_reap_failed "
                    "binding=%s path=%s",
                    binding_id, op.relative_path,
                )
        if reaped_stubs:
            log.info(
                "vault.sync.binding_disconnect_reaped_batch_stubs "
                "binding=%s count=%d",
                binding_id, reaped_stubs,
            )
    store.update_binding_state(binding_id, state="unbound")
    log.info(
        "vault.sync.binding_disconnected binding=%s sync_mode=%s "
        "local_entries_preserved=%d pending_ops_dropped=%d",
        binding_id, binding.sync_mode, local_count, len(pending),
    )
    return DisconnectResult(
        binding_id=binding_id,
        local_entries_preserved=local_count,
        pending_ops_dropped=len(pending),
    )


def _require_binding(
    store: VaultBindingsStore, binding_id: str,
) -> VaultBinding:
    binding = store.get_binding(binding_id)
    if binding is None:
        raise KeyError(f"unknown binding: {binding_id!r}")
    return binding


__all__ = [
    "BindingCancellationRegistry",
    "DisconnectResult",
    "PauseResult",
    "ResumeResult",
    "SyncCancelledError",
    "disconnect_binding",
    "pause_binding",
    "resume_binding",
]
