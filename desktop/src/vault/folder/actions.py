"""Per-row lifecycle dispatchers for the Vault settings Folders tab (F-Y15).

GTK-free helpers so the click handlers in the ``vault_folders``
package can delegate the synchronous "do the thing + format a toast"
path here and keep their own surface area limited to widget plumbing.

Each dispatcher:

- accepts a fresh ``VaultBindingsStore`` (callers always re-open it
  inside the worker thread so SQLite connections don't cross threads),
- forwards the optional :class:`BindingCancellationRegistry` so an
  in-flight cycle aborts at its next checkpoint (F-Y08),
- returns a ``(toast, error)`` tuple ready for the GTK status label —
  exactly one of those two is ``None``,
- does NOT touch GTK / GLib / threading; the caller wraps in a worker.

The "Disconnect" path is destructive (pending ops are dropped per
T12.5). The dispatcher itself only runs after the caller confirms — a
``confirm`` callable is required and must return ``True`` for the
work to proceed.
"""

from __future__ import annotations

from typing import Any, Callable

from ..binding.lifecycle import (
    BindingCancellationRegistry,
    DisconnectResult,
    PauseResult,
    ResumeResult,
    VaultDisconnectHasPendingOpsError,
    disconnect_binding,
    pause_binding,
    resume_binding,
)
from ..binding.bindings import VaultBinding, VaultBindingsStore
from ..error_messages import humanize


def dispatch_pause(
    *,
    store: VaultBindingsStore,
    binding_id: str,
    cancellation: BindingCancellationRegistry | None = None,
) -> tuple[str | None, str | None]:
    """Pause a binding; return ``(toast, error)`` for the status label."""
    try:
        result: PauseResult = pause_binding(
            store, binding_id, cancellation=cancellation,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"Pause failed: {humanize(exc)}"
    pending = result.pending_ops_preserved
    if pending:
        return f"Paused. {pending} pending op(s) preserved for resume.", None
    return "Paused.", None


def dispatch_resume(
    *,
    store: VaultBindingsStore,
    binding_id: str,
    flush: Callable[[VaultBinding], Any] | None = None,
) -> tuple[str | None, str | None]:
    """Resume a paused binding; optionally run ``flush`` on the bound row.

    The caller passes a closure that wraps :func:`flush_and_sync_binding`
    against the now-bound binding (or ``None`` to skip the flush — used
    by the AT-SPI smoke tests). Errors raised by ``flush`` are humanized
    and surfaced as the error half of the tuple.
    """
    try:
        result: ResumeResult = resume_binding(
            store, binding_id, flush=flush,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"Resume failed: {humanize(exc)}"
    if result.flushed is None:
        return "Resumed.", None
    # ``flushed`` is a SyncCycleResult; render with the existing toast
    # formatter to keep parity with the Sync-now toast.
    from ..binding.sync import format_sync_outcome_toast
    return f"Resumed. {format_sync_outcome_toast(result.flushed)}", None


def dispatch_disconnect(
    *,
    store: VaultBindingsStore,
    binding_id: str,
    confirm: Callable[[], bool],
    cancellation: BindingCancellationRegistry | None = None,
    confirm_force_drop: Callable[[int], bool] | None = None,
) -> tuple[str | None, str | None]:
    """Disconnect (unbind) a binding after the caller's ``confirm`` gate.

    ``confirm`` is a no-arg callable that returns ``True`` only if the
    user has confirmed the destructive action. The GTK side passes a
    closure that runs ``Adw.AlertDialog`` modally; tests pass
    ``lambda: True`` / ``lambda: False`` directly.

    Review §3.M5 — ``confirm_force_drop`` is the second-stage gate that
    fires when the binding still has pending ops the user might want
    flushed first. ``disconnect_binding`` raises
    :class:`VaultDisconnectHasPendingOpsError` on that case; we catch,
    invoke ``confirm_force_drop(pending_count)``, and re-call with
    ``force=True`` only when the user confirms. If the kwarg is not
    provided, the dispatch returns an error tuple naming the count so
    the caller can decide what to do.
    """
    if not confirm():
        return "Disconnect cancelled.", None
    try:
        result: DisconnectResult = disconnect_binding(
            store, binding_id, cancellation=cancellation,
        )
    except VaultDisconnectHasPendingOpsError as pending_exc:
        if confirm_force_drop is None or not confirm_force_drop(
            pending_exc.pending_count,
        ):
            return (
                None,
                f"Disconnect cancelled: {pending_exc.pending_count} "
                "pending change(s) would be dropped. Flush first or "
                "confirm dropping them.",
            )
        try:
            result = disconnect_binding(
                store, binding_id,
                cancellation=cancellation, force=True,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"Disconnect failed: {humanize(exc)}"
    except Exception as exc:  # noqa: BLE001
        return None, f"Disconnect failed: {humanize(exc)}"
    bits: list[str] = []
    if result.local_entries_preserved:
        bits.append(
            f"{result.local_entries_preserved} local entry rows preserved"
        )
    if result.pending_ops_dropped:
        bits.append(f"{result.pending_ops_dropped} pending op(s) dropped")
    suffix = " " + ", ".join(bits) + "." if bits else ""
    return f"Disconnected.{suffix}", None


__all__ = [
    "dispatch_disconnect",
    "dispatch_pause",
    "dispatch_resume",
]
