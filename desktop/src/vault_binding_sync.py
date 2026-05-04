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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .vault_bindings import (
    VaultBinding,
    VaultBindingsStore,
    VaultLocalEntry,
    VaultPendingOperation,
)
from .vault_manifest import find_file_entry, normalize_manifest_path, tombstone_file_entry
from .vault_relay_errors import VaultCASConflictError
from .vault_upload import UploadResult, upload_file


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
    watcher_coordinator: Any = None,
    chunk_cache_dir: Path | None = None,
    progress: Callable[["SyncOpOutcome"], None] | None = None,
) -> "SyncCycleResult":
    """Manual "Sync now" entrypoint (T10.6).

    Drains any in-flight watcher events into the pending-ops queue,
    then runs one backup-only cycle. Used by the per-binding "Sync
    now" button so the UI doesn't need to wait for the next watcher
    tick before pushing a fresh batch of edits.
    """
    if watcher_coordinator is not None:
        try:
            watcher_coordinator.tick()
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.watcher_flush_failed binding=%s",
                binding.binding_id,
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
        # Refresh the manifest if this op published a new revision.
        if outcome.status in ("uploaded", "deleted"):
            try:
                current_manifest = vault.fetch_manifest(relay)
            except Exception:  # noqa: BLE001
                log.exception(
                    "vault.sync.refetch_after_publish_failed binding=%s",
                    binding.binding_id,
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
    if not absolute.is_file():
        # The watcher may have queued the upload right before the file
        # was renamed/removed; treat this like a delete op.
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
    parent_revision = int(manifest.get("revision", 0))
    next_revision = parent_revision + 1
    next_manifest = tombstone_file_entry(
        manifest,
        remote_folder_id=binding.remote_folder_id,
        path=normalized,
        deleted_at=deleted_at,
        author_device_id=author_device_id,
    )
    next_manifest["revision"] = next_revision
    next_manifest["parent_revision"] = parent_revision
    next_manifest["created_at"] = deleted_at
    next_manifest["author_device_id"] = str(author_device_id)
    try:
        vault.publish_manifest(relay, next_manifest)
    except VaultCASConflictError as exc:
        store.mark_op_failed(op.op_id, f"cas_conflict: {exc}")
        log.warning(
            "vault.sync.delete_cas_conflict binding=%s path=%s",
            binding.binding_id, relative_path,
        )
        return SyncOpOutcome(
            op_id=op.op_id, op_type="delete",
            relative_path=relative_path, status="failed",
            error="cas_conflict",
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
