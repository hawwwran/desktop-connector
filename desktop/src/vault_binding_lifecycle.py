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
from dataclasses import dataclass
from typing import Any, Callable

from .vault_bindings import VaultBinding, VaultBindingsStore


log = logging.getLogger(__name__)


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
    store: VaultBindingsStore, binding_id: str,
) -> PauseResult:
    """Transition a bound binding into ``state="paused"``.

    Pending ops are preserved verbatim (§A12). The sync_mode field
    is unchanged so resume restores the same direction without asking
    the user. No-op if already paused.
    """
    binding = _require_binding(store, binding_id)
    if binding.state == "paused":
        log.info(
            "vault.sync.binding_pause_noop binding=%s already_paused",
            binding_id,
        )
        return PauseResult(
            binding=binding,
            pending_ops_preserved=len(store.list_pending_ops(binding_id)),
        )
    if binding.state != "bound":
        raise ValueError(
            f"binding {binding_id!r} is in state {binding.state!r}; "
            "pause requires state=='bound'"
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
        log.info(
            "vault.sync.binding_resume_noop binding=%s already_bound",
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
    store: VaultBindingsStore, binding_id: str,
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

    The user GC path (a future "Forget local index" button, §gaps §20)
    is what physically removes the row; until then the local_entries
    rows survive for fast change detection on reconnect.
    """
    binding = _require_binding(store, binding_id)
    if binding.state == "unbound":
        log.info(
            "vault.sync.binding_disconnect_noop binding=%s already_unbound",
            binding_id,
        )
        return DisconnectResult(
            binding_id=binding_id,
            local_entries_preserved=len(store.list_local_entries(binding_id)),
            pending_ops_dropped=0,
        )
    local_count = len(store.list_local_entries(binding_id))
    pending = store.list_pending_ops(binding_id)
    for op in pending:
        store.delete_pending_op(op.op_id)
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
    "DisconnectResult",
    "PauseResult",
    "ResumeResult",
    "disconnect_binding",
    "pause_binding",
    "resume_binding",
]
