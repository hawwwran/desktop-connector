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

from .vault_bindings import (
    VaultBinding,
    VaultBindingsStore,
    VaultLocalEntry,
    VaultPendingOperation,
)
from .vault_manifest import find_file_entry, normalize_manifest_path, tombstone_file_entry
from .vault_relay_errors import VaultCASConflictError, VaultQuotaExceededError
from .vault_upload import UploadResult, UploadSpecialFileSkipped, upload_file


log = logging.getLogger(__name__)


@dataclass
class SyncOpOutcome:
    op_id: int
    op_type: str
    relative_path: str
    status: str        # "uploaded" | "deleted" | "skipped" | "failed"
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
) -> "SyncCycleResult":
    """Manual "Sync now" entrypoint (T10.6).

    Drains any in-flight watcher events into the pending-ops queue,
    then dispatches on ``binding.sync_mode``: two-way runs
    :func:`vault_binding_twoway.run_two_way_cycle`; backup-only and
    download-only fall through to the local-drain cycle. Paused
    bindings are a no-op (logged).
    """
    if watcher_coordinator is not None:
        try:
            watcher_coordinator.tick()
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.watcher_flush_failed binding=%s",
                binding.binding_id,
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
        from .vault_binding_twoway import run_two_way_cycle
        return run_two_way_cycle(
            vault=vault, relay=relay, store=store,
            binding=binding, author_device_id=author_device_id,
            device_name=device_name or "this device",
            chunk_cache_dir=chunk_cache_dir, progress=progress,
        )
    return run_backup_only_cycle(
        vault=vault, relay=relay, store=store,
        binding=binding, author_device_id=author_device_id,
        chunk_cache_dir=chunk_cache_dir, progress=progress,
    )


def format_sync_outcome_toast(result: "SyncCycleResult") -> str:
    """Render a one-line user-facing summary of a sync cycle result."""
    if not result.outcomes:
        if result.ended_at_revision == result.started_at_revision:
            return "Sync now: nothing to do."
        return (
            f"Sync now: caught up at revision {result.ended_at_revision}."
        )
    uploaded = sum(1 for o in result.outcomes if o.status == "uploaded")
    deleted = sum(1 for o in result.outcomes if o.status == "deleted")
    skipped = sum(1 for o in result.outcomes if o.status == "skipped")
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
) -> SyncCycleResult:
    """Drain ``binding``'s pending ops once. Returns a summary.

    The caller may pass an already-fetched ``manifest`` to avoid an
    extra round-trip; otherwise we fetch the head fresh.
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

    head = manifest or vault.fetch_manifest(relay)
    started_at_revision = int(head.get("revision", 0))

    local_root = Path(binding.local_path)
    pending = store.list_pending_ops(binding.binding_id)
    outcomes: list[SyncOpOutcome] = []

    current_manifest: dict[str, Any] = head
    for op in pending:
        outcome = _execute_op(
            vault=vault,
            relay=relay,
            store=store,
            binding=binding,
            local_root=local_root,
            op=op,
            manifest=current_manifest,
            author_device_id=author_device_id,
            chunk_cache_dir=chunk_cache_dir,
        )
        outcomes.append(outcome)
        if progress is not None:
            try:
                progress(outcome)
            except Exception:  # noqa: BLE001
                log.exception("vault.sync.progress_callback_failed")
        # Refresh the manifest if this op published a new revision OR
        # failed (likely a CAS conflict — refresh so the next op
        # sees the new world). F-Y07.
        if outcome.status in ("uploaded", "deleted", "failed"):
            try:
                current_manifest = vault.fetch_manifest(relay)
            except Exception:  # noqa: BLE001
                log.warning(
                    "vault.sync.refetch_after_publish_failed binding=%s",
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
) -> SyncOpOutcome:
    """Execute one pending op end-to-end. Errors stay scoped per-op."""
    relative_path = op.relative_path
    if op.op_type == "upload":
        return _execute_upload(
            vault=vault, relay=relay, store=store,
            binding=binding, local_root=local_root, op=op,
            manifest=manifest, author_device_id=author_device_id,
            chunk_cache_dir=chunk_cache_dir,
        )
    if op.op_type == "delete":
        return _execute_delete(
            vault=vault, relay=relay, store=store,
            binding=binding, op=op, manifest=manifest,
            author_device_id=author_device_id,
        )
    # Rename ops aren't part of T10.5's backup-only contract.
    store.mark_op_failed(op.op_id, f"unsupported op_type: {op.op_type}")
    return SyncOpOutcome(
        op_id=op.op_id,
        op_type=op.op_type,
        relative_path=relative_path,
        status="failed",
        error=f"unsupported op_type: {op.op_type}",
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
) -> SyncOpOutcome:
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
            return SyncOpOutcome(
                op_id=op.op_id, op_type="upload",
                relative_path=relative_path, status="skipped",
                error="path_vanished_never_synced",
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
        )
    except UploadSpecialFileSkipped:
        # F-Y17: never propagate a tombstone for a symlink/FIFO. Drop
        # the op; the file simply isn't part of the vault.
        store.delete_pending_op(op.op_id)
        return SyncOpOutcome(
            op_id=op.op_id, op_type="upload",
            relative_path=relative_path, status="skipped",
            error="special_file",
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
        return SyncOpOutcome(
            op_id=op.op_id, op_type="upload",
            relative_path=relative_path, status="failed",
            error="quota_exceeded",
        )
    except VaultCASConflictError as exc:
        # T6.3 owns CAS retry; if it bubbles here it means the inner
        # retry budget was exhausted. Mark and try again next cycle.
        store.mark_op_failed(op.op_id, f"cas_conflict: {exc}")
        log.warning(
            "vault.sync.upload_cas_conflict binding=%s path=%s",
            binding.binding_id, relative_path,
        )
        return SyncOpOutcome(
            op_id=op.op_id, op_type="upload",
            relative_path=relative_path, status="failed",
            error="cas_conflict",
        )
    except Exception as exc:  # noqa: BLE001
        store.mark_op_failed(op.op_id, str(exc))
        log.warning(
            "vault.sync.upload_failed binding=%s path=%s error=%s",
            binding.binding_id, relative_path, exc,
        )
        return SyncOpOutcome(
            op_id=op.op_id, op_type="upload",
            relative_path=relative_path, status="failed",
            error=str(exc),
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
    return SyncOpOutcome(
        op_id=op.op_id,
        op_type="upload",
        relative_path=relative_path,
        status="skipped" if result.skipped_identical else "uploaded",
        bytes_uploaded=int(result.bytes_uploaded),
        chunks_uploaded=int(result.chunks_uploaded),
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
) -> SyncOpOutcome:
    relative_path = op.relative_path
    normalized = normalize_manifest_path(relative_path)
    entry = find_file_entry(manifest, binding.remote_folder_id, normalized)
    if entry is None or bool(entry.get("deleted")):
        # Already gone (or never existed) — clear local + queue rows.
        store.delete_local_entry(binding.binding_id, relative_path)
        store.delete_pending_op(op.op_id)
        return SyncOpOutcome(
            op_id=op.op_id, op_type="delete",
            relative_path=relative_path, status="skipped",
        )

    from datetime import datetime, timezone
    deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # F-Y06: §D4 rebase loop — re-fetch + re-tombstone on CAS conflict.
    # Without this a tombstone publish on a hot multi-device vault
    # never converges; the spec retry budget here matches upload's 5.
    DELETE_CAS_MAX_RETRIES = 5
    current_manifest = manifest
    last_exc: Exception | None = None
    for attempt in range(DELETE_CAS_MAX_RETRIES + 1):
        normalized_path = normalize_manifest_path(relative_path)
        entry_now = find_file_entry(
            current_manifest, binding.remote_folder_id, normalized_path,
        )
        if entry_now is None or bool(entry_now.get("deleted")):
            store.delete_local_entry(binding.binding_id, relative_path)
            store.delete_pending_op(op.op_id)
            return SyncOpOutcome(
                op_id=op.op_id, op_type="delete",
                relative_path=relative_path, status="skipped",
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
            vault.publish_manifest(relay, next_manifest)
            break
        except VaultCASConflictError as exc:
            last_exc = exc
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
                return SyncOpOutcome(
                    op_id=op.op_id, op_type="delete",
                    relative_path=relative_path, status="failed",
                    error="cas_conflict",
                )
            try:
                current_manifest = vault.fetch_manifest(relay)
            except Exception as fetch_exc:  # noqa: BLE001
                store.mark_op_failed(op.op_id, str(fetch_exc))
                log.warning(
                    "vault.sync.delete_refetch_failed binding=%s error=%s",
                    binding.binding_id, fetch_exc,
                )
                return SyncOpOutcome(
                    op_id=op.op_id, op_type="delete",
                    relative_path=relative_path, status="failed",
                    error=f"refetch_failed: {fetch_exc}",
                )
        except Exception as exc:  # noqa: BLE001
            store.mark_op_failed(op.op_id, str(exc))
            log.warning(
                "vault.sync.delete_failed binding=%s path=%s error=%s",
                binding.binding_id, relative_path, exc,
            )
            return SyncOpOutcome(
                op_id=op.op_id, op_type="delete",
                relative_path=relative_path, status="failed",
                error=str(exc),
            )

    store.delete_local_entry(binding.binding_id, relative_path)
    store.delete_pending_op(op.op_id)
    return SyncOpOutcome(
        op_id=op.op_id, op_type="delete",
        relative_path=relative_path, status="deleted",
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
) -> SyncOpOutcome:
    log.info(
        "vault.sync.upload_path_vanished_promoted_to_delete "
        "binding=%s path=%s",
        binding.binding_id, op.relative_path,
    )
    inner = _execute_delete(
        vault=vault, relay=relay, store=store, binding=binding,
        op=op, manifest=manifest, author_device_id=author_device_id,
    )
    # Ledger view: the op was an "upload" but the outcome was a tombstone.
    # Preserve the original op_type so callers can correlate with the
    # watcher-emitted op log.
    return SyncOpOutcome(
        op_id=inner.op_id,
        op_type=op.op_type,
        relative_path=inner.relative_path,
        status=inner.status,
        error=inner.error,
        bytes_uploaded=inner.bytes_uploaded,
        chunks_uploaded=inner.chunks_uploaded,
    )


__all__ = [
    "SyncCycleResult",
    "SyncOpOutcome",
    "flush_and_sync_binding",
    "format_sync_outcome_toast",
    "run_backup_only_cycle",
]
