"""Backup-only sync loop for a single binding (T10.5).

One cycle drains the binding's :class:`vault_pending_operations` queue:

- ``upload`` op  → encrypt + PUT chunks + CAS-publish a new manifest
  revision via :func:`vault_upload.upload_file`. On success, the
  binding's :class:`vault_local_entries` row is refreshed with the
  new ``content_fingerprint`` + ``last_synced_revision``.
- ``delete`` op  → fetch head, tombstone the entry, publish. The
  local entry row is removed (the row only mirrors local state).
- A path that vanished between watcher-enqueue and sync-cycle is
  promoted to a ``delete`` op (the watcher's "atomic rename overwrite"
  case in T10.4 maps cleanly here too).

Per §gaps §20: backup-only never applies remote changes to local. The
cycle still fetches the head manifest at the end so the binding's
``last_synced_revision`` advances — that's how the UI knows we're
"caught up" even when other devices are publishing concurrently.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


# Atomic-rename grace window: editors like KeePass / Vim / SQLite-WAL
# perform "write tmp ; unlink target ; rename tmp target" — the unlink
# briefly publishes "file gone" before the rename completes. Without a
# grace window, F-Y03 publishes a remote tombstone for the just-saved
# file. 5 retries × 200 ms = 1 s ceiling: long enough to absorb every
# atomic-rename writer we know of, short enough to feel snappy if the
# file genuinely vanished.
_ATOMIC_RENAME_GRACE_RETRIES = 5
_ATOMIC_RENAME_GRACE_DELAY_S = 0.2


def _file_present_with_atomic_rename_grace(absolute: Path) -> bool:
    """Return True if ``absolute`` is a regular file, retrying briefly
    to absorb atomic-rename windows.
    """
    for attempt in range(_ATOMIC_RENAME_GRACE_RETRIES):
        if absolute.is_file():
            return True
        if attempt == _ATOMIC_RENAME_GRACE_RETRIES - 1:
            break
        time.sleep(_ATOMIC_RENAME_GRACE_DELAY_S)
    return False


def _previously_synced_via_store(
    store: "VaultBindingsStore",
    binding_id: str,
    relative_path: str,
) -> bool:
    """Whether the path has a non-empty content fingerprint in local entries.

    A non-empty ``content_fingerprint`` means the path participated in
    at least one prior successful sync (baseline or upload). Used to
    enforce §A17 — never publish a tombstone for a never-synced path.
    """
    try:
        entry = store.get_local_entry(binding_id, relative_path)
    except Exception:  # noqa: BLE001
        log.exception(
            "vault.sync.previously_synced_check_failed binding=%s path=%s",
            binding_id, relative_path,
        )
        # Fail closed: we don't *know* it was previously synced.
        return False
    return bool(entry and entry.content_fingerprint)

from .lifecycle import SyncCancelledError
from .bindings import (
    VaultBinding,
    VaultBindingsStore,
    VaultLocalEntry,
    VaultPendingOperation,
)
from ..manifest import (
    add_or_append_file_version,
    find_file_entry,
    normalize_manifest_path,
    normalize_manifest_plaintext,
    tombstone_file_entry,
)
from ..relay_errors import VaultCASConflictError, VaultQuotaExceededError
from ..ui.browser_model import decrypt_manifest as decrypt_manifest_envelope
from ..upload import (
    CAS_MAX_RETRIES,
    PreparedUpload,
    UploadFileTooLargeError,
    UploadResult,
    UploadSpecialFileSkipped,
    clear_stub,
    prepare_upload_for_batch,
    upload_file,
)


log = logging.getLogger(__name__)


# SO-3 — Batched manifest publish.
#
# ``run_backup_only_cycle`` collapses up to ``PUBLISH_BATCH_SIZE``
# per-op manifest publishes into a single CAS publish, dropping the
# initial-bind manifest round-trips from O(N) to O(N/K). 50 is the
# plan's tuned default: small enough that a kill-mid-batch only re-
# uploads ~50 files worth of chunks (and the chunk dedupe makes the
# re-upload mostly a HEAD-and-skip), large enough that the
# manifest-ship-per-op cliff in suite 0004 (B7) flattens out.
#
# Steady-state single-file edits still publish promptly: each cycle
# call only drains the pending-ops queue once, and the final partial
# batch flushes at cycle end. A user dropping one file in waits for
# one publish, same as today.
PUBLISH_BATCH_SIZE = 50


@dataclass
class _BatchEntry:
    """One op's contribution to a SO-3 batched manifest mutation.

    For uploads, ``prepared`` carries the version payload built by
    ``prepare_upload_for_batch`` (chunks already PUT, version_id
    fixed). For deletes (and upload-promoted-to-delete on a vanished
    path), ``deleted_at`` carries the tombstone timestamp.

    ``post_publish_outcome`` is the outcome the cycle records once the
    batch publishes successfully; ``local_entry_for_upsert`` and
    ``op_for_dequeue`` capture the pending-ops + local-entries
    bookkeeping the batch flush owes the store.
    """

    op: VaultPendingOperation
    kind: str  # "upload" | "delete"
    # Upload-specific:
    prepared: PreparedUpload | None = None
    absolute_path: Path | None = None
    # Delete-specific:
    deleted_at: str | None = None
    # Whether this entry was a watcher-queued upload that got promoted
    # to a delete (file vanished before sync). The outcome's op_type
    # stays "upload" so the ledger view correlates with the watcher
    # event, but the manifest mutation is a tombstone.
    promoted_from_upload: bool = False


@dataclass
class SyncOpOutcome:
    op_id: int
    op_type: str
    relative_path: str
    status: str        # "uploaded" | "deleted" | "skipped" | "failed" | "cancelled"
    error: str | None = None
    bytes_uploaded: int = 0
    chunks_uploaded: int = 0


@dataclass
class SyncCycleResult:
    binding_id: str
    started_at_revision: int
    ended_at_revision: int
    outcomes: list[SyncOpOutcome] = field(default_factory=list)
    binding: VaultBinding | None = None
    cancelled: bool = False  # F-Y08: should_continue returned False mid-cycle

    @property
    def succeeded_count(self) -> int:
        return sum(
            1 for o in self.outcomes
            if o.status in ("uploaded", "deleted", "skipped")
        )

    @property
    def failed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


class SyncVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_manifest(self, relay, *, local_index=None) -> dict[str, Any]: ...

    def publish_manifest(
        self, relay, manifest, *, local_index=None,
    ) -> dict[str, Any]: ...


def flush_and_sync_binding(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    author_device_id: str,
    device_name: str | None = None,
    watcher_coordinator: Any = None,
    chunk_cache_dir: Path | None = None,
    progress: Callable[["SyncOpOutcome"], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> "SyncCycleResult":
    """Manual "Sync now" entrypoint (T10.6).

    Drains any in-flight watcher events into the pending-ops queue,
    then dispatches on ``binding.sync_mode``: two-way runs
    :func:`vault_binding_twoway.run_two_way_cycle`; backup-only and
    download-only fall through to the local-drain cycle. Paused
    bindings are a no-op (logged).

    F-Y08: ``should_continue`` is forwarded to the dispatched cycle
    driver (and downward into ``upload_file``) so a Pause / Disconnect
    landing mid-cycle stops the chunk loop within ~1 chunk.
    """
    if watcher_coordinator is not None:
        try:
            watcher_coordinator.tick()
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.watcher_flush_failed binding=%s",
                binding.binding_id,
            )
    # Catch-up filesystem scan: handles changes that landed while no
    # watcher was up (settings subprocess just created the binding,
    # daemon restart, etc.) so "Sync now" can actually find them.
    # No-op when disk == local-entries cache.
    if binding.sync_mode != "paused":
        try:
            from .scan import scan_for_local_changes
            scan_for_local_changes(store=store, binding=binding)
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.scan_failed binding=%s", binding.binding_id,
            )
    if binding.sync_mode == "paused":
        log.info(
            "vault.sync.flush_skipped_paused binding=%s",
            binding.binding_id,
        )
        return SyncCycleResult(
            binding_id=binding.binding_id,
            started_at_revision=int(binding.last_synced_revision or 0),
            ended_at_revision=int(binding.last_synced_revision or 0),
            outcomes=[],
        )
    if binding.sync_mode == "two-way":
        # Imported lazily to avoid an import cycle (twoway imports from sync).
        from .twoway import run_two_way_cycle
        return run_two_way_cycle(
            vault=vault, relay=relay, store=store,
            binding=binding, author_device_id=author_device_id,
            device_name=device_name or "this device",
            chunk_cache_dir=chunk_cache_dir, progress=progress,
            should_continue=should_continue,
        )
    return run_backup_only_cycle(
        vault=vault, relay=relay, store=store,
        binding=binding, author_device_id=author_device_id,
        chunk_cache_dir=chunk_cache_dir, progress=progress,
        should_continue=should_continue,
    )


def format_sync_outcome_toast(result: "SyncCycleResult") -> str:
    """Render a one-line user-facing summary of a sync cycle result."""
    if not result.outcomes:
        if result.cancelled:
            return "Sync now: cancelled."
        if result.ended_at_revision == result.started_at_revision:
            return "Sync now: nothing to do."
        return (
            f"Sync now: caught up at revision {result.ended_at_revision}."
        )
    uploaded = sum(1 for o in result.outcomes if o.status == "uploaded")
    deleted = sum(1 for o in result.outcomes if o.status == "deleted")
    skipped = sum(1 for o in result.outcomes if o.status == "skipped")
    cancelled_n = sum(1 for o in result.outcomes if o.status == "cancelled")
    failed = result.failed_count
    parts: list[str] = []
    if uploaded:
        parts.append(f"{uploaded} uploaded")
    if deleted:
        parts.append(f"{deleted} deleted")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} failed")
    if cancelled_n or result.cancelled:
        parts.append("cancelled")
    summary = ", ".join(parts) if parts else "no changes"
    return f"Sync now: {summary}."


def run_backup_only_cycle(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    author_device_id: str,
    manifest: dict[str, Any] | None = None,
    chunk_cache_dir: Path | None = None,
    progress: Callable[[SyncOpOutcome], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
    batch_size: int = PUBLISH_BATCH_SIZE,
) -> SyncCycleResult:
    """Drain ``binding``'s pending ops once. Returns a summary.

    The caller may pass an already-fetched ``manifest`` to avoid an
    extra round-trip; otherwise we fetch the head fresh.

    F-Y08: ``should_continue`` is checked before each op and passed
    down into the chunk-PUT phase. When it returns ``False`` mid-cycle
    the loop exits early; the in-flight op's outcome is recorded
    (status ``"cancelled"`` if the chunk loop bailed, else the op
    was never started) and the result's ``cancelled`` flag is set.
    Remaining queue rows are left untouched and pick up on the next
    cycle.

    SO-3: per-op chunk PUTs are grouped into batches of ``batch_size``
    (default :data:`PUBLISH_BATCH_SIZE`); each batch closes with one
    CAS-published manifest revision instead of N. Skipped ops
    (identical bytes, special files, never-synced vanish) and failed
    ops (quota, CAS exhaustion, non-CAS errors) do not enter the
    batch — they flush immediately so the cycle keeps draining. On
    kill mid-batch the chunks already PUT are idempotent at the
    relay; the next cycle re-encrypts the same files, HEAD-and-skips
    those chunks, and rebuilds the batch.
    """
    if binding.state != "bound":
        raise ValueError(
            f"binding {binding.binding_id} is in state {binding.state!r}; "
            "expected 'bound' before running a sync cycle"
        )
    if binding.sync_mode == "paused":
        raise ValueError(
            f"binding {binding.binding_id} sync_mode is 'paused'"
        )
    if binding.sync_mode not in ("backup-only", "two-way"):
        raise ValueError(
            f"binding {binding.binding_id} sync_mode={binding.sync_mode!r} "
            "is not supported by this loop (T10.5 covers backup-only)"
        )
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    head = manifest or vault.fetch_manifest(relay)
    started_at_revision = int(head.get("revision", 0))

    local_root = Path(binding.local_path)
    pending = store.list_pending_ops(binding.binding_id)
    outcomes: list[SyncOpOutcome] = []
    cancelled = False

    current_manifest: dict[str, Any] = head
    batch: list[_BatchEntry] = []

    def _emit(outcome: SyncOpOutcome) -> None:
        outcomes.append(outcome)
        if progress is not None:
            try:
                progress(outcome)
            except Exception:  # noqa: BLE001
                log.exception("vault.sync.progress_callback_failed")

    for op in pending:
        # F-Y08: bail before starting another op when cancellation
        # fired between ops. Anything already batched flushes below.
        if should_continue is not None and not should_continue():
            log.info(
                "vault.sync.cycle_cancelled_between_ops binding=%s remaining=%d",
                binding.binding_id,
                len(pending) - len(outcomes) - len(batch),
            )
            cancelled = True
            break

        immediate_outcome, batch_entry, current_manifest = _prepare_op_for_batch(
            vault=vault,
            relay=relay,
            store=store,
            binding=binding,
            local_root=local_root,
            op=op,
            manifest=current_manifest,
            author_device_id=author_device_id,
            chunk_cache_dir=chunk_cache_dir,
            should_continue=should_continue,
        )

        if immediate_outcome is not None:
            _emit(immediate_outcome)
            if immediate_outcome.status == "cancelled":
                cancelled = True
                break
            if immediate_outcome.status == "failed":
                # F-Y07: a non-batched failure (quota, special-file
                # error, unsupported op type) is rare enough that we
                # take the GET to refresh head — keeps semantics in
                # line with the pre-SO-3 single-op path.
                try:
                    current_manifest = vault.fetch_manifest(relay)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "vault.sync.refetch_after_failure_failed binding=%s",
                        binding.binding_id, exc_info=True,
                    )
            continue

        if batch_entry is None:
            # Defensive — prep returned no outcome and no entry. Treat
            # as a soft-skip so the cycle stays robust.
            continue
        batch.append(batch_entry)

        if len(batch) >= batch_size:
            batch_outcomes, current_manifest, batch_failed = _flush_batch(
                vault=vault,
                relay=relay,
                store=store,
                binding=binding,
                local_root=local_root,
                current_manifest=current_manifest,
                batch=batch,
                author_device_id=author_device_id,
            )
            for outcome in batch_outcomes:
                _emit(outcome)
            batch = []
            if batch_failed:
                try:
                    current_manifest = vault.fetch_manifest(relay)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "vault.sync.refetch_after_batch_failure_failed binding=%s",
                        binding.binding_id, exc_info=True,
                    )

    # Cycle-end flush: any partial batch still pending publishes now.
    # This handles two cases:
    #   1. Steady-state "one new file" — a single op landed, K-1 didn't
    #      arrive, so we publish what we have rather than sitting on it.
    #   2. Cancellation between ops — when ``should_continue`` flipped
    #      false after some ops had already finished their chunk PUTs,
    #      we still publish the ready batch so the work isn't wasted
    #      (F-Y08's "bail within ~1 chunk" is enforced inside
    #      ``prepare_upload_for_batch``; an op that bailed mid-chunk
    #      surfaces a "cancelled" outcome and never enters the batch).
    if batch:
        batch_outcomes, current_manifest, batch_failed = _flush_batch(
            vault=vault,
            relay=relay,
            store=store,
            binding=binding,
            local_root=local_root,
            current_manifest=current_manifest,
            batch=batch,
            author_device_id=author_device_id,
        )
        for outcome in batch_outcomes:
            _emit(outcome)
        batch = []
        if batch_failed and not cancelled:
            try:
                current_manifest = vault.fetch_manifest(relay)
            except Exception:  # noqa: BLE001
                log.warning(
                    "vault.sync.refetch_after_batch_failure_failed binding=%s",
                    binding.binding_id, exc_info=True,
                )

    ended_at_revision = int(current_manifest.get("revision", started_at_revision))
    store.update_binding_state(
        binding.binding_id,
        last_synced_revision=ended_at_revision,
    )
    rebound = store.get_binding(binding.binding_id) or binding

    return SyncCycleResult(
        binding_id=binding.binding_id,
        started_at_revision=started_at_revision,
        ended_at_revision=ended_at_revision,
        outcomes=outcomes,
        binding=rebound,
        cancelled=cancelled,
    )


def _prepare_op_for_batch(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    local_root: Path,
    op: VaultPendingOperation,
    manifest: dict[str, Any],
    author_device_id: str,
    chunk_cache_dir: Path | None,
    should_continue: Callable[[], bool] | None,
) -> tuple[SyncOpOutcome | None, _BatchEntry | None, dict[str, Any]]:
    """SO-3: prepare one op for inclusion in a batched manifest publish.

    Returns ``(immediate_outcome, batch_entry, manifest_after_prep)``.

    Exactly one of ``immediate_outcome`` and ``batch_entry`` is
    non-None when the op was handled (the caller emits the outcome or
    appends the entry); both may be ``None`` if the op was a defensive
    soft-skip. ``manifest_after_prep`` is the input ``manifest``
    unchanged — prep never publishes, so the cycle's view of the head
    is stable until the next batch flush.

    The immediate-outcome path covers ops that don't contribute to a
    batch: identical-bytes skip (no manifest mutation), special-file
    skip, never-synced vanish, quota / CAS / unexpected errors, and
    cancellation. Everything else (real uploads, deletes, and
    upload-promoted-to-delete on a previously-synced path) becomes a
    ``_BatchEntry`` that ``_flush_batch`` consumes.
    """
    if op.op_type == "upload":
        return _prepare_upload_for_batch(
            vault=vault,
            relay=relay,
            store=store,
            binding=binding,
            local_root=local_root,
            op=op,
            manifest=manifest,
            author_device_id=author_device_id,
            chunk_cache_dir=chunk_cache_dir,
            should_continue=should_continue,
        )
    if op.op_type == "delete":
        return _prepare_delete_for_batch(
            store=store, binding=binding, op=op, manifest=manifest,
        )
    # T10.5 doesn't carry rename ops in backup-only.
    store.mark_op_failed(op.op_id, f"unsupported op_type: {op.op_type}")
    return (
        SyncOpOutcome(
            op_id=op.op_id,
            op_type=op.op_type,
            relative_path=op.relative_path,
            status="failed",
            error=f"unsupported op_type: {op.op_type}",
        ),
        None,
        manifest,
    )


def _prepare_upload_for_batch(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    local_root: Path,
    op: VaultPendingOperation,
    manifest: dict[str, Any],
    author_device_id: str,
    chunk_cache_dir: Path | None,
    should_continue: Callable[[], bool] | None,
) -> tuple[SyncOpOutcome | None, _BatchEntry | None, dict[str, Any]]:
    """Prep an upload op for batching: run chunk PUTs, return a batch entry.

    Identical-bytes shortcut: when the file's keyed fingerprint already
    matches the latest live version in ``manifest``, no chunks are PUT
    and no batch entry is produced — the cycle records "skipped" and
    stamps the local-entry row at the manifest's current revision.

    Path-vanished cases:

    - Never previously synced: dequeue silently, immediate "skipped"
      outcome (§A17 — never publish a tombstone for a never-synced
      path).
    - Previously synced: produce a *tombstone* batch entry so the
      vanish flows through as a delete in the next publish. The
      outcome's ``op_type`` stays ``"upload"`` so the ledger view
      still correlates with the watcher's original event.
    """
    relative_path = op.relative_path
    absolute = local_root / relative_path
    if not _file_present_with_atomic_rename_grace(absolute):
        if not _previously_synced_via_store(store, binding.binding_id, relative_path):
            log.info(
                "vault.sync.upload_path_vanished_silent "
                "binding=%s path=%s",
                binding.binding_id, relative_path,
            )
            store.delete_pending_op(op.op_id)
            return (
                SyncOpOutcome(
                    op_id=op.op_id, op_type="upload",
                    relative_path=relative_path, status="skipped",
                    error="path_vanished_never_synced",
                ),
                None,
                manifest,
            )
        log.info(
            "vault.sync.upload_path_vanished_promoted_to_delete "
            "binding=%s path=%s",
            binding.binding_id, relative_path,
        )
        normalized = normalize_manifest_path(relative_path)
        entry = find_file_entry(manifest, binding.remote_folder_id, normalized)
        if entry is None or bool(entry.get("deleted")):
            # Already gone remotely too — just reap local state.
            store.delete_local_entry(binding.binding_id, relative_path)
            store.delete_pending_op(op.op_id)
            return (
                SyncOpOutcome(
                    op_id=op.op_id, op_type="upload",
                    relative_path=relative_path, status="skipped",
                ),
                None,
                manifest,
            )
        from datetime import datetime, timezone
        deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return (
            None,
            _BatchEntry(
                op=op, kind="delete",
                deleted_at=deleted_at,
                promoted_from_upload=True,
            ),
            manifest,
        )

    try:
        prepared = prepare_upload_for_batch(
            vault=vault,
            relay=relay,
            manifest=manifest,
            local_path=absolute,
            remote_folder_id=binding.remote_folder_id,
            remote_path=relative_path,
            author_device_id=author_device_id,
            should_continue=should_continue,
            batch_cache_dir=chunk_cache_dir,
        )
    except SyncCancelledError as exc:
        log.info(
            "vault.sync.upload_cancelled_op binding=%s path=%s",
            binding.binding_id, relative_path,
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="cancelled",
                error=str(exc),
            ),
            None,
            manifest,
        )
    except UploadSpecialFileSkipped:
        store.delete_pending_op(op.op_id)
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="skipped",
                error="special_file",
            ),
            None,
            manifest,
        )
    except UploadFileTooLargeError as exc:
        store.mark_op_failed(op.op_id, str(exc))
        log.warning(
            "vault.sync.upload_too_large binding=%s path=%s error=%s",
            binding.binding_id, relative_path, exc,
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="failed",
                error=str(exc),
            ),
            None,
            manifest,
        )
    except VaultQuotaExceededError as exc:
        log.warning(
            "vault.sync.upload_quota_exceeded binding=%s path=%s used=%d quota=%d",
            binding.binding_id, relative_path,
            getattr(exc, "used_bytes", 0),
            getattr(exc, "quota_bytes", 0),
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="failed",
                error="quota_exceeded",
            ),
            None,
            manifest,
        )
    except VaultCASConflictError as exc:
        # ``prepare_upload_for_batch`` does not publish, so a CAS
        # conflict here would only fire from a HEAD probe — treat as
        # transient and let the next cycle re-try.
        store.mark_op_failed(op.op_id, f"cas_conflict: {exc}")
        log.warning(
            "vault.sync.upload_cas_conflict binding=%s path=%s",
            binding.binding_id, relative_path,
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="failed",
                error="cas_conflict",
            ),
            None,
            manifest,
        )
    except Exception as exc:  # noqa: BLE001
        store.mark_op_failed(op.op_id, str(exc))
        log.warning(
            "vault.sync.upload_failed binding=%s path=%s error=%s",
            binding.binding_id, relative_path, exc,
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="failed",
                error=str(exc),
            ),
            None,
            manifest,
        )

    if prepared.skipped_identical:
        # File's bytes already match the latest live version. Stamp the
        # local-entry row at the current manifest revision and drop the
        # pending op — no batch entry needed.
        try:
            stat = absolute.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
        except OSError:
            size, mtime_ns = int(prepared.logical_size), 0
        current_revision = int(manifest.get("revision", 0))
        store.upsert_local_entry(VaultLocalEntry(
            binding_id=binding.binding_id,
            relative_path=relative_path,
            content_fingerprint=prepared.content_fingerprint,
            size_bytes=size,
            mtime_ns=mtime_ns,
            last_synced_revision=current_revision,
        ))
        store.delete_pending_op(op.op_id)
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="skipped",
                bytes_uploaded=int(prepared.bytes_uploaded),
                chunks_uploaded=int(prepared.chunks_uploaded),
            ),
            None,
            manifest,
        )

    return (
        None,
        _BatchEntry(
            op=op, kind="upload",
            prepared=prepared,
            absolute_path=absolute,
        ),
        manifest,
    )


def _prepare_delete_for_batch(
    *,
    store: VaultBindingsStore,
    binding: VaultBinding,
    op: VaultPendingOperation,
    manifest: dict[str, Any],
) -> tuple[SyncOpOutcome | None, _BatchEntry | None, dict[str, Any]]:
    """Prep a delete op for batching: build a tombstone entry, or
    short-circuit when the remote entry is already gone."""
    relative_path = op.relative_path
    normalized = normalize_manifest_path(relative_path)
    entry = find_file_entry(manifest, binding.remote_folder_id, normalized)
    if entry is None or bool(entry.get("deleted")):
        store.delete_local_entry(binding.binding_id, relative_path)
        store.delete_pending_op(op.op_id)
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="delete",
                relative_path=relative_path, status="skipped",
            ),
            None,
            manifest,
        )
    from datetime import datetime, timezone
    deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return (
        None,
        _BatchEntry(op=op, kind="delete", deleted_at=deleted_at),
        manifest,
    )


def _apply_batch_to_manifest(
    parent_manifest: dict[str, Any],
    batch: list[_BatchEntry],
    *,
    remote_folder_id: str,
    author_device_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Fold every batched mutation onto ``parent_manifest`` and bump revisions.

    Both helpers are idempotent: ``add_or_append_file_version`` is a
    no-op when the same ``version_id`` is already the latest; the
    tombstone helper refreshes ``deleted_at`` on an already-deleted
    entry. So replaying the same batch against a freshly-fetched head
    on a CAS conflict converges without duplicating work.

    Tombstones for already-gone entries are tolerated — that case
    arises naturally when a CAS conflict refetch shows the same path
    was already tombstoned by another writer.
    """
    parent_n = normalize_manifest_plaintext(parent_manifest)
    parent_revision = int(parent_n.get("revision", 0))
    next_revision = parent_revision + 1

    candidate = parent_n
    for entry in batch:
        if entry.kind == "upload":
            assert entry.prepared is not None
            candidate = add_or_append_file_version(
                candidate,
                remote_folder_id=remote_folder_id,
                path=entry.prepared.normalized_remote_path,
                version=entry.prepared.version_payload,
                entry_id=entry.prepared.entry_id,
            )
        elif entry.kind == "delete":
            normalized = normalize_manifest_path(entry.op.relative_path)
            try:
                candidate = tombstone_file_entry(
                    candidate,
                    remote_folder_id=remote_folder_id,
                    path=normalized,
                    deleted_at=str(entry.deleted_at or created_at),
                    author_device_id=author_device_id,
                )
            except KeyError:
                # Entry already gone — fine for a batched tombstone
                # (likely a CAS-conflict replay landing after another
                # writer tombstoned the same path).
                pass

    candidate["revision"] = next_revision
    candidate["parent_revision"] = parent_revision
    candidate["created_at"] = created_at
    candidate["author_device_id"] = str(author_device_id)
    return candidate


def _publish_batch_with_cas_retry(
    *,
    vault: SyncVault,
    relay: Any,
    parent_manifest: dict[str, Any],
    batch: list[_BatchEntry],
    remote_folder_id: str,
    author_device_id: str,
    max_retries: int = CAS_MAX_RETRIES,
) -> dict[str, Any]:
    """Publish the batch with §D4 CAS retry semantics.

    On 409, decrypt the inline server-head envelope, re-apply the
    batch's mutations on top of the new head, retry. ``last_attempt``
    is replaced each iteration (rather than being merged forward) so
    we don't carry stale revision stamps across attempts.
    """
    from datetime import datetime, timezone

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    candidate = _apply_batch_to_manifest(
        parent_manifest, batch,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        created_at=created_at,
    )
    for attempt in range(max_retries):
        try:
            return vault.publish_manifest(relay, candidate)
        except VaultCASConflictError as exc:
            envelope = exc.current_manifest_ciphertext_bytes()
            if not envelope:
                raise
            server_head = decrypt_manifest_envelope(vault, envelope)
            log.info(
                "vault.sync.batch_cas_retry attempt=%d/%d batch_size=%d",
                attempt + 1, max_retries, len(batch),
            )
            candidate = _apply_batch_to_manifest(
                server_head, batch,
                remote_folder_id=remote_folder_id,
                author_device_id=author_device_id,
                created_at=created_at,
            )
    try:
        return vault.publish_manifest(relay, candidate)
    except VaultCASConflictError:
        log.warning(
            "vault.sync.batch_cas_exhausted batch_size=%d retries=%d",
            len(batch), max_retries,
        )
        raise


def _flush_batch(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    local_root: Path,
    current_manifest: dict[str, Any],
    batch: list[_BatchEntry],
    author_device_id: str,
) -> tuple[list[SyncOpOutcome], dict[str, Any], bool]:
    """Publish the accumulated batch and run post-publish bookkeeping.

    Returns ``(outcomes, manifest_after_publish, batch_failed)``. On
    a successful publish, every batched op is marked "uploaded" or
    "deleted" and its local-entry / pending-op rows are reconciled.
    On CAS exhaustion (or any other publish error), every batched op
    is marked "failed" so the cycle's failed_count reflects reality;
    pending-op rows survive for the next cycle.
    """
    if not batch:
        return [], current_manifest, False

    try:
        published = _publish_batch_with_cas_retry(
            vault=vault,
            relay=relay,
            parent_manifest=current_manifest,
            batch=batch,
            remote_folder_id=binding.remote_folder_id,
            author_device_id=author_device_id,
        )
    except VaultCASConflictError as exc:
        log.warning(
            "vault.sync.batch_publish_cas_conflict binding=%s batch_size=%d",
            binding.binding_id, len(batch),
        )
        outcomes: list[SyncOpOutcome] = []
        for entry in batch:
            store.mark_op_failed(entry.op.op_id, f"cas_conflict: {exc}")
            outcomes.append(_failed_outcome(entry, "cas_conflict"))
        return outcomes, current_manifest, True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.sync.batch_publish_failed binding=%s batch_size=%d error=%s",
            binding.binding_id, len(batch), exc,
        )
        outcomes = []
        for entry in batch:
            store.mark_op_failed(entry.op.op_id, str(exc))
            outcomes.append(_failed_outcome(entry, str(exc)))
        return outcomes, current_manifest, True

    new_revision = int(published.get("revision", current_manifest.get("revision", 0)))
    outcomes = []
    for entry in batch:
        if entry.kind == "upload":
            assert entry.prepared is not None
            assert entry.absolute_path is not None
            try:
                stat = entry.absolute_path.stat()
                size = int(stat.st_size)
                mtime_ns = int(stat.st_mtime_ns)
            except OSError:
                size, mtime_ns = int(entry.prepared.logical_size), 0
            store.upsert_local_entry(VaultLocalEntry(
                binding_id=binding.binding_id,
                relative_path=entry.op.relative_path,
                content_fingerprint=entry.prepared.content_fingerprint,
                size_bytes=size,
                mtime_ns=mtime_ns,
                last_synced_revision=new_revision,
            ))
            store.delete_pending_op(entry.op.op_id)
            # SO-3 dedupe stub no longer needed — the publish landed,
            # the chunk_ids are permanent in the manifest, and we
            # don't want the next cycle's prep to short-circuit on it
            # if the file is later edited and re-queued.
            if (
                entry.prepared.stub_session_id is not None
                and entry.prepared.stub_cache_dir is not None
            ):
                clear_stub(
                    entry.prepared.stub_session_id,
                    Path(entry.prepared.stub_cache_dir),
                )
            outcomes.append(SyncOpOutcome(
                op_id=entry.op.op_id,
                op_type="upload",
                relative_path=entry.op.relative_path,
                status="uploaded",
                bytes_uploaded=int(entry.prepared.bytes_uploaded),
                chunks_uploaded=int(entry.prepared.chunks_uploaded),
            ))
        else:  # delete (or upload-promoted-to-delete)
            store.delete_local_entry(binding.binding_id, entry.op.relative_path)
            store.delete_pending_op(entry.op.op_id)
            outcomes.append(SyncOpOutcome(
                op_id=entry.op.op_id,
                op_type=entry.op.op_type,  # preserves "upload" for promoted entries
                relative_path=entry.op.relative_path,
                status="deleted",
            ))

    log.info(
        "vault.sync.batch_published binding=%s batch_size=%d new_revision=%d",
        binding.binding_id, len(batch), new_revision,
    )
    return outcomes, published, False


def _failed_outcome(entry: _BatchEntry, error: str) -> SyncOpOutcome:
    """Build the SyncOpOutcome row for a batched op that didn't publish."""
    op_type = "upload" if entry.kind == "upload" else entry.op.op_type
    status_label = "failed"
    return SyncOpOutcome(
        op_id=entry.op.op_id,
        op_type=op_type,
        relative_path=entry.op.relative_path,
        status=status_label,
        error=error,
    )


def _execute_op(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    local_root: Path,
    op: VaultPendingOperation,
    manifest: dict[str, Any],
    author_device_id: str,
    chunk_cache_dir: Path | None,
    should_continue: Callable[[], bool] | None = None,
) -> tuple[SyncOpOutcome, dict[str, Any]]:
    """Execute one pending op end-to-end. Errors stay scoped per-op.

    SO-2: returns ``(outcome, manifest_after_op)``. ``manifest_after_op``
    is the newly-published revision when a publish happened (uploaded,
    deleted, or upload-promoted-to-delete on a previously-synced path);
    otherwise it is the input ``manifest`` unchanged (skipped, failed
    before publish, cancelled). Callers that need a fresh view after a
    CAS-conflict failure must re-fetch explicitly — this helper does
    not, so success paths skip the redundant per-op GET.

    SO-3 retained this single-op path for ``twoway.py`` — the two-way
    conflict-rename detector in Phase A inspects the manifest per file
    and is left on the single-publish shape for now. Backup-only uses
    the batched primitives ``_prepare_op_for_batch`` / ``_flush_batch``
    via :func:`run_backup_only_cycle`.
    """
    relative_path = op.relative_path
    if op.op_type == "upload":
        return _execute_upload(
            vault=vault, relay=relay, store=store,
            binding=binding, local_root=local_root, op=op,
            manifest=manifest, author_device_id=author_device_id,
            chunk_cache_dir=chunk_cache_dir,
            should_continue=should_continue,
        )
    if op.op_type == "delete":
        return _execute_delete(
            vault=vault, relay=relay, store=store,
            binding=binding, op=op, manifest=manifest,
            author_device_id=author_device_id,
        )
    # Rename ops aren't part of T10.5's backup-only contract.
    store.mark_op_failed(op.op_id, f"unsupported op_type: {op.op_type}")
    return (
        SyncOpOutcome(
            op_id=op.op_id,
            op_type=op.op_type,
            relative_path=relative_path,
            status="failed",
            error=f"unsupported op_type: {op.op_type}",
        ),
        manifest,
    )


def _execute_upload(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    local_root: Path,
    op: VaultPendingOperation,
    manifest: dict[str, Any],
    author_device_id: str,
    chunk_cache_dir: Path | None,
    should_continue: Callable[[], bool] | None = None,
) -> tuple[SyncOpOutcome, dict[str, Any]]:
    relative_path = op.relative_path
    absolute = local_root / relative_path
    if not _file_present_with_atomic_rename_grace(absolute):
        # The watcher may have queued the upload right before the file
        # was renamed/removed; treat this like a delete op only if the
        # path was previously synced (else the §A17 invariant would be
        # violated by publishing a tombstone for a never-synced file).
        if not _previously_synced_via_store(store, binding.binding_id, relative_path):
            log.info(
                "vault.sync.upload_path_vanished_silent "
                "binding=%s path=%s",
                binding.binding_id, relative_path,
            )
            store.delete_pending_op(op.op_id)
            return (
                SyncOpOutcome(
                    op_id=op.op_id, op_type="upload",
                    relative_path=relative_path, status="skipped",
                    error="path_vanished_never_synced",
                ),
                manifest,
            )
        return _promote_to_delete(
            vault=vault, relay=relay, store=store,
            binding=binding, op=op, manifest=manifest,
            author_device_id=author_device_id,
        )

    try:
        result: UploadResult = upload_file(
            vault=vault,
            relay=relay,
            manifest=manifest,
            local_path=absolute,
            remote_folder_id=binding.remote_folder_id,
            remote_path=relative_path,
            author_device_id=author_device_id,
            mode="new_file_or_version",
            resume_cache_dir=chunk_cache_dir,
            should_continue=should_continue,
        )
    except SyncCancelledError as exc:
        # F-Y08: chunk-level bail. The op stays queued; the partial
        # upload session was saved per chunk so a future cycle picks
        # up via resume_upload (or just re-PUTs idempotent chunks).
        log.info(
            "vault.sync.upload_cancelled_op binding=%s path=%s",
            binding.binding_id, relative_path,
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="cancelled",
                error=str(exc),
            ),
            manifest,
        )
    except UploadSpecialFileSkipped:
        # F-Y17: never propagate a tombstone for a symlink/FIFO. Drop
        # the op; the file simply isn't part of the vault.
        store.delete_pending_op(op.op_id)
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="skipped",
                error="special_file",
            ),
            manifest,
        )
    except VaultQuotaExceededError as exc:
        # F-D03: leave the op pending, surface a typed failure so the
        # UI can render the §D2 eviction prompt. Don't increment
        # attempts as this is a transient blocked state, not a bug.
        log.warning(
            "vault.sync.upload_quota_exceeded binding=%s path=%s used=%d quota=%d",
            binding.binding_id, relative_path,
            getattr(exc, "used_bytes", 0),
            getattr(exc, "quota_bytes", 0),
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="failed",
                error="quota_exceeded",
            ),
            manifest,
        )
    except VaultCASConflictError as exc:
        # T6.3 owns CAS retry; if it bubbles here it means the inner
        # retry budget was exhausted. Mark and try again next cycle.
        store.mark_op_failed(op.op_id, f"cas_conflict: {exc}")
        log.warning(
            "vault.sync.upload_cas_conflict binding=%s path=%s",
            binding.binding_id, relative_path,
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="failed",
                error="cas_conflict",
            ),
            manifest,
        )
    except Exception as exc:  # noqa: BLE001
        store.mark_op_failed(op.op_id, str(exc))
        log.warning(
            "vault.sync.upload_failed binding=%s path=%s error=%s",
            binding.binding_id, relative_path, exc,
        )
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="failed",
                error=str(exc),
            ),
            manifest,
        )

    new_revision = int(result.manifest.get("revision", manifest.get("revision", 0)))
    try:
        stat = absolute.stat()
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        size, mtime_ns = int(result.logical_size), 0
    store.upsert_local_entry(VaultLocalEntry(
        binding_id=binding.binding_id,
        relative_path=relative_path,
        content_fingerprint=result.content_fingerprint,
        size_bytes=size,
        mtime_ns=mtime_ns,
        last_synced_revision=new_revision,
    ))
    store.delete_pending_op(op.op_id)
    return (
        SyncOpOutcome(
            op_id=op.op_id,
            op_type="upload",
            relative_path=relative_path,
            status="skipped" if result.skipped_identical else "uploaded",
            bytes_uploaded=int(result.bytes_uploaded),
            chunks_uploaded=int(result.chunks_uploaded),
        ),
        result.manifest,
    )


def _execute_delete(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    op: VaultPendingOperation,
    manifest: dict[str, Any],
    author_device_id: str,
) -> tuple[SyncOpOutcome, dict[str, Any]]:
    relative_path = op.relative_path
    normalized = normalize_manifest_path(relative_path)
    entry = find_file_entry(manifest, binding.remote_folder_id, normalized)
    if entry is None or bool(entry.get("deleted")):
        # Already gone (or never existed) — clear local + queue rows.
        store.delete_local_entry(binding.binding_id, relative_path)
        store.delete_pending_op(op.op_id)
        return (
            SyncOpOutcome(
                op_id=op.op_id, op_type="delete",
                relative_path=relative_path, status="skipped",
            ),
            manifest,
        )

    from datetime import datetime, timezone
    deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # F-Y06: §D4 rebase loop — re-fetch + re-tombstone on CAS conflict.
    # Without this a tombstone publish on a hot multi-device vault
    # never converges; the spec retry budget here matches upload's 5.
    DELETE_CAS_MAX_RETRIES = 5
    current_manifest = manifest
    published: dict[str, Any] | None = None
    for attempt in range(DELETE_CAS_MAX_RETRIES + 1):
        normalized_path = normalize_manifest_path(relative_path)
        entry_now = find_file_entry(
            current_manifest, binding.remote_folder_id, normalized_path,
        )
        if entry_now is None or bool(entry_now.get("deleted")):
            store.delete_local_entry(binding.binding_id, relative_path)
            store.delete_pending_op(op.op_id)
            return (
                SyncOpOutcome(
                    op_id=op.op_id, op_type="delete",
                    relative_path=relative_path, status="skipped",
                ),
                current_manifest,
            )
        parent_revision = int(current_manifest.get("revision", 0))
        next_revision = parent_revision + 1
        next_manifest = tombstone_file_entry(
            current_manifest,
            remote_folder_id=binding.remote_folder_id,
            path=normalized_path,
            deleted_at=deleted_at,
            author_device_id=author_device_id,
        )
        next_manifest["revision"] = next_revision
        next_manifest["parent_revision"] = parent_revision
        next_manifest["created_at"] = deleted_at
        next_manifest["author_device_id"] = str(author_device_id)
        try:
            # SO-2: capture the post-publish manifest so the caller can
            # thread it into the next op without a separate GET.
            published = vault.publish_manifest(relay, next_manifest)
            break
        except VaultCASConflictError as exc:
            log.info(
                "vault.sync.delete_cas_retry attempt=%d/%d binding=%s path=%s",
                attempt + 1, DELETE_CAS_MAX_RETRIES,
                binding.binding_id, relative_path,
            )
            if attempt == DELETE_CAS_MAX_RETRIES:
                store.mark_op_failed(op.op_id, f"cas_conflict: {exc}")
                log.warning(
                    "vault.sync.delete_cas_exhausted binding=%s path=%s",
                    binding.binding_id, relative_path,
                )
                return (
                    SyncOpOutcome(
                        op_id=op.op_id, op_type="delete",
                        relative_path=relative_path, status="failed",
                        error="cas_conflict",
                    ),
                    current_manifest,
                )
            try:
                current_manifest = vault.fetch_manifest(relay)
            except Exception as fetch_exc:  # noqa: BLE001
                store.mark_op_failed(op.op_id, str(fetch_exc))
                log.warning(
                    "vault.sync.delete_refetch_failed binding=%s error=%s",
                    binding.binding_id, fetch_exc,
                )
                return (
                    SyncOpOutcome(
                        op_id=op.op_id, op_type="delete",
                        relative_path=relative_path, status="failed",
                        error=f"refetch_failed: {fetch_exc}",
                    ),
                    current_manifest,
                )
        except Exception as exc:  # noqa: BLE001
            store.mark_op_failed(op.op_id, str(exc))
            log.warning(
                "vault.sync.delete_failed binding=%s path=%s error=%s",
                binding.binding_id, relative_path, exc,
            )
            return (
                SyncOpOutcome(
                    op_id=op.op_id, op_type="delete",
                    relative_path=relative_path, status="failed",
                    error=str(exc),
                ),
                current_manifest,
            )

    store.delete_local_entry(binding.binding_id, relative_path)
    store.delete_pending_op(op.op_id)
    return (
        SyncOpOutcome(
            op_id=op.op_id, op_type="delete",
            relative_path=relative_path, status="deleted",
        ),
        published if published is not None else current_manifest,
    )


def _promote_to_delete(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    op: VaultPendingOperation,
    manifest: dict[str, Any],
    author_device_id: str,
) -> tuple[SyncOpOutcome, dict[str, Any]]:
    log.info(
        "vault.sync.upload_path_vanished_promoted_to_delete "
        "binding=%s path=%s",
        binding.binding_id, op.relative_path,
    )
    inner, manifest_after = _execute_delete(
        vault=vault, relay=relay, store=store, binding=binding,
        op=op, manifest=manifest, author_device_id=author_device_id,
    )
    # Ledger view: the op was an "upload" but the outcome was a tombstone.
    # Preserve the original op_type so callers can correlate with the
    # watcher-emitted op log.
    return (
        SyncOpOutcome(
            op_id=inner.op_id,
            op_type=op.op_type,
            relative_path=inner.relative_path,
            status=inner.status,
            error=inner.error,
            bytes_uploaded=inner.bytes_uploaded,
            chunks_uploaded=inner.chunks_uploaded,
        ),
        manifest_after,
    )


__all__ = [
    "SyncCycleResult",
    "SyncOpOutcome",
    "flush_and_sync_binding",
    "format_sync_outcome_toast",
    "run_backup_only_cycle",
]
