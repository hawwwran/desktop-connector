"""Two-way sync cycle for a bound binding (T12.1).

Extends the T10.5 backup-only path with a *remote-changes-applied*
phase. One cycle is:

1. Fetch head manifest.
2. For each remote entry in the binding's folder, decide:
   - **Tombstone reaches local**: if the local file is still in
     baseline shape (its fingerprint matches the row in
     ``vault_local_entries``), trash it via :func:`vault_trash.trash_path`
     and clear the local-entry row. If the local file was modified
     since last sync (fingerprint differs), keep it — §local-delete-
     vs-remote-modify says "keep local modified copy, keep remote
     tombstone, create conflict notice" — and enqueue an upload op so
     the modification flows back to remote on the next pass.
   - **New / modified remote version**: if our local file already has
     the right keyed fingerprint, skip. If the local file has
     different bytes than both the row's baseline AND the remote
     version, we treat it as concurrent local edit (§D4 keep-both):
     the local copy is preserved at a §A20 conflict path before the
     download lands at the original path.
3. Drain ``vault_pending_operations`` via the existing T10.5
   primitives (uploads + delete-tombstones).
4. Repeat 1–3 until quiet (no new remote revision since the previous
   pass and the queue is empty), or the iteration cap kicks in.

The watcher will still enqueue an upload op for any file we just
downloaded (it sees a freshly-modified file). That op falls through
T6.1's fingerprint shortcut on the next cycle — no bytes uploaded
— so the harmless echo is bounded.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .vault_atomic import fsync_dir
from .vault_binding_lifecycle import SyncCancelledError
from .vault_binding_sync import (
    SyncCycleResult,
    SyncOpOutcome,
    SyncVault,
    _execute_op,
)
from .vault_bindings import VaultBinding, VaultBindingsStore, VaultLocalEntry
from .vault_conflict_naming import make_conflict_path
from .vault_download import default_vault_download_cache_dir, download_latest_file
from .vault_trash import trash_path


log = logging.getLogger(__name__)


MAX_TWO_WAY_ITERATIONS = 4  # bound the loop; one full pass is the common case


def run_two_way_cycle(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    author_device_id: str,
    device_name: str,
    chunk_cache_dir: Path | None = None,
    progress: Callable[[SyncOpOutcome], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> SyncCycleResult:
    """Run one two-way sync cycle for ``binding``.

    Combines :func:`vault_binding_sync.run_backup_only_cycle`'s pending-
    ops drain with a remote-changes-applied phase that downloads new
    remote versions and tombstones-with-trash according to §A20 / §D4.

    F-Y08: ``should_continue`` is consulted between Phase A and Phase
    B, between every Phase B op (and inside ``upload_file`` between
    chunks), and between full iterations. The result's ``cancelled``
    flag is set when a Pause / Disconnect lands mid-cycle.
    """
    if binding.state != "bound":
        raise ValueError(
            f"binding {binding.binding_id} is in state {binding.state!r}; "
            "expected 'bound' before running a two-way cycle"
        )
    if binding.sync_mode != "two-way":
        raise ValueError(
            f"binding {binding.binding_id} sync_mode={binding.sync_mode!r}; "
            "expected 'two-way' for run_two_way_cycle"
        )

    cache_dir = chunk_cache_dir or default_vault_download_cache_dir()
    local_root = Path(binding.local_path)
    local_root.mkdir(parents=True, exist_ok=True)

    head = vault.fetch_manifest(relay)
    started_revision = int(head.get("revision", 0))
    outcomes: list[SyncOpOutcome] = []
    cancelled = False

    last_revision = started_revision - 1  # force first iter
    for _ in range(MAX_TWO_WAY_ITERATIONS):
        if should_continue is not None and not should_continue():
            log.info(
                "vault.sync.twoway_cancelled_pre_iteration binding=%s",
                binding.binding_id,
            )
            cancelled = True
            break
        revision_at_start = int(head.get("revision", 0))
        # Phase A: apply remote → local.
        remote_outcomes = _apply_remote_to_local(
            vault=vault,
            relay=relay,
            store=store,
            binding=binding,
            manifest=head,
            local_root=local_root,
            cache_dir=cache_dir,
            device_name=device_name,
            progress=progress,
            should_continue=should_continue,
        )
        outcomes.extend(remote_outcomes)
        if remote_outcomes and remote_outcomes[-1].status == "cancelled":
            cancelled = True
            break

        if should_continue is not None and not should_continue():
            log.info(
                "vault.sync.twoway_cancelled_between_phases binding=%s",
                binding.binding_id,
            )
            cancelled = True
            break

        # Phase B: drain pending uploads/deletes.
        pending = store.list_pending_ops(binding.binding_id)
        b_outcomes_before = len(outcomes)
        for op in pending:
            if should_continue is not None and not should_continue():
                log.info(
                    "vault.sync.twoway_cancelled_between_ops binding=%s remaining=%d",
                    binding.binding_id, len(pending) - (len(outcomes) - b_outcomes_before),
                )
                cancelled = True
                break
            outcome = _execute_op(
                vault=vault,
                relay=relay,
                store=store,
                binding=binding,
                local_root=local_root,
                op=op,
                manifest=head,
                author_device_id=author_device_id,
                chunk_cache_dir=cache_dir,
                should_continue=should_continue,
            )
            outcomes.append(outcome)
            if progress is not None:
                try:
                    progress(outcome)
                except Exception:  # noqa: BLE001
                    log.exception("vault.sync.progress_callback_failed")
            if outcome.status == "cancelled":
                cancelled = True
                break
            if outcome.status in ("uploaded", "deleted", "failed"):
                # F-Y07: a failed publish (CAS conflict) is a strong
                # "the world changed" signal — re-fetch head so the
                # next op in this iteration sees the new revision.
                try:
                    head = vault.fetch_manifest(relay)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "vault.sync.refetch_after_publish_failed binding=%s",
                        binding.binding_id, exc_info=True,
                    )
        if cancelled:
            break

        # Convergence check: no progress made in this iteration. F-Y26.
        # "Progress" means either the revision advanced OR at least one
        # op was processed successfully. A loop where every op fails
        # would never converge under the old (queue-empty) rule.
        new_revision = int(head.get("revision", revision_at_start))
        any_progress = any(
            o.status in ("uploaded", "deleted", "skipped")
            for o in outcomes[-len(pending) :]
        ) if pending else False
        if (
            new_revision == revision_at_start
            and not any_progress
        ):
            break
        last_revision = new_revision
        # Re-fetch for next iteration so we observe any remote work
        # that landed while we were uploading.
        try:
            head = vault.fetch_manifest(relay)
        except Exception:  # noqa: BLE001
            log.warning(
                "vault.sync.refetch_for_next_iter_failed binding=%s",
                binding.binding_id, exc_info=True,
            )

    ended_revision = int(head.get("revision", started_revision))
    store.update_binding_state(
        binding.binding_id,
        last_synced_revision=ended_revision,
    )
    rebound = store.get_binding(binding.binding_id) or binding
    return SyncCycleResult(
        binding_id=binding.binding_id,
        started_at_revision=started_revision,
        ended_at_revision=ended_revision,
        outcomes=outcomes,
        binding=rebound,
        cancelled=cancelled,
    )


# ---------------------------------------------------------------------------
# Phase A: apply remote → local
# ---------------------------------------------------------------------------


def _apply_remote_to_local(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    manifest: dict[str, Any],
    local_root: Path,
    cache_dir: Path,
    device_name: str,
    progress: Callable[[SyncOpOutcome], None] | None,
    should_continue: Callable[[], bool] | None = None,
) -> list[SyncOpOutcome]:
    folder = _find_folder(manifest, binding.remote_folder_id)
    if folder is None:
        return []

    folder_display_name = str(folder.get("display_name_enc", ""))
    if not folder_display_name:
        log.warning(
            "vault.sync.twoway_folder_no_display_name binding=%s folder=%s",
            binding.binding_id, binding.remote_folder_id,
        )
        return []

    revision = int(manifest.get("revision", 0))
    fingerprint_key = _content_fingerprint_key(vault)
    outcomes: list[SyncOpOutcome] = []

    for entry in folder.get("entries", []) or []:
        # F-Y08: bail between remote entries so a Pause / Disconnect
        # lands within one entry's worth of work even on a folder with
        # hundreds of files. We append a sentinel "cancelled" outcome
        # so the caller can flip the cycle's cancelled flag.
        if should_continue is not None and not should_continue():
            log.info(
                "vault.sync.twoway_phase_a_cancelled binding=%s processed=%d",
                binding.binding_id, len(outcomes),
            )
            outcomes.append(SyncOpOutcome(
                op_id=0, op_type="apply_remote",
                relative_path="", status="cancelled",
                error="cancelled_phase_a",
            ))
            return outcomes
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        relative = str(entry.get("path") or "").strip()
        if not relative:
            continue
        relative = relative.replace("\\", "/")
        if relative.startswith("/") or ".." in relative.split("/"):
            log.warning("vault.sync.twoway_skip_unsafe_path path=%s", relative)
            continue

        target = local_root / relative
        local_entry = store.get_local_entry(binding.binding_id, relative)
        deleted = bool(entry.get("deleted"))

        if deleted:
            outcome = _apply_remote_delete(
                store=store, binding=binding,
                relative=relative, target=target,
                local_entry=local_entry,
                fingerprint_key=fingerprint_key,
            )
            if outcome is not None:
                outcomes.append(outcome)
                if progress is not None:
                    try:
                        progress(outcome)
                    except Exception:  # noqa: BLE001
                        log.exception("vault.sync.progress_callback_failed")
            continue

        version = _latest_version(entry)
        if version is None:
            continue
        remote_fp = str(version.get("content_fingerprint", ""))
        if not remote_fp:
            continue

        # Fast path: local-entries row already says we have this fingerprint.
        # Trust it as long as the file is on disk.
        if (
            local_entry is not None
            and local_entry.content_fingerprint == remote_fp
            and target.is_file()
        ):
            continue

        outcome = _apply_remote_upsert(
            vault=vault, relay=relay, store=store,
            binding=binding, manifest=manifest,
            relative=relative, target=target,
            local_entry=local_entry,
            folder_display_name=folder_display_name,
            cache_dir=cache_dir,
            device_name=device_name,
            remote_fingerprint=remote_fp,
            remote_logical_size=int(version.get("logical_size") or 0),
            revision=revision,
            fingerprint_key=fingerprint_key,
        )
        if outcome is not None:
            outcomes.append(outcome)
            if progress is not None:
                try:
                    progress(outcome)
                except Exception:  # noqa: BLE001
                    log.exception("vault.sync.progress_callback_failed")

    return outcomes


def _apply_remote_delete(
    *,
    store: VaultBindingsStore,
    binding: VaultBinding,
    relative: str,
    target: Path,
    local_entry: VaultLocalEntry | None,
    fingerprint_key: bytes | None,
) -> SyncOpOutcome | None:
    """Trash the local file when remote tombstones an entry we already had.

    §local-delete-vs-remote-modify (sync-engine §08) wants the local
    modified copy preserved if the user changed it since last sync.
    Detection: the on-disk fingerprint matches the local-entry row's
    fingerprint ⇒ unmodified ⇒ safe to trash. Anything else ⇒ keep
    locally and enqueue an upload so the modification flows back.
    """
    if local_entry is None:
        # We never knew about it — leave the local filesystem alone.
        return None
    if not target.is_file():
        # Local file already gone; just clear the entry row.
        store.delete_local_entry(binding.binding_id, relative)
        return SyncOpOutcome(
            op_id=0, op_type="remote-delete",
            relative_path=relative, status="deleted",
        )

    local_fp = _file_keyed_fingerprint(target, fingerprint_key)
    if local_fp is None:
        # Read error or no master key. We do NOT enqueue an upload —
        # that would inverse the remote's stated intent (delete) and
        # silently revive a tombstoned file. Defer to the next cycle.
        log.warning(
            "vault.sync.twoway_remote_tombstone_unreadable "
            "binding=%s path=%s",
            binding.binding_id, relative,
        )
        return SyncOpOutcome(
            op_id=0, op_type="remote-delete",
            relative_path=relative, status="skipped",
            error="local_fingerprint_unreadable",
        )
    if local_fp == local_entry.content_fingerprint:
        # Unmodified: safe to trash.
        ok = trash_path(target)
        if ok:
            store.delete_local_entry(binding.binding_id, relative)
            log.info(
                "vault.sync.twoway_remote_tombstone_applied binding=%s path=%s",
                binding.binding_id, relative,
            )
            return SyncOpOutcome(
                op_id=0, op_type="remote-delete",
                relative_path=relative, status="deleted",
            )
        log.warning(
            "vault.sync.twoway_trash_failed binding=%s path=%s",
            binding.binding_id, relative,
        )
        return SyncOpOutcome(
            op_id=0, op_type="remote-delete",
            relative_path=relative, status="failed",
            error="trash_failed",
        )

    # Local was modified after last sync: keep it; push the modification.
    log.info(
        "vault.sync.twoway_remote_tombstone_kept_local_modified "
        "binding=%s path=%s",
        binding.binding_id, relative,
    )
    store.coalesce_op(
        binding_id=binding.binding_id,
        op_type="upload", relative_path=relative,
    )
    return SyncOpOutcome(
        op_id=0, op_type="remote-delete",
        relative_path=relative, status="skipped",
        error="local_modified_after_remote_tombstone",
    )


def _apply_remote_upsert(
    *,
    vault: SyncVault,
    relay: Any,
    store: VaultBindingsStore,
    binding: VaultBinding,
    manifest: dict[str, Any],
    relative: str,
    target: Path,
    local_entry: VaultLocalEntry | None,
    folder_display_name: str,
    cache_dir: Path,
    device_name: str,
    remote_fingerprint: str,
    remote_logical_size: int,
    revision: int,
    fingerprint_key: bytes | None,
) -> SyncOpOutcome | None:
    """Bring the local file into agreement with the remote latest version."""
    display_path = f"{folder_display_name}/{relative}"

    # If the local file already has the right bytes (e.g. another tool
    # produced an identical copy, or we crashed mid-cycle), just stamp
    # the local-entry row and move on.
    if target.is_file():
        local_fp = _file_keyed_fingerprint(target, fingerprint_key)
        if local_fp is not None and local_fp == remote_fingerprint:
            _stamp_local_entry(
                store=store, binding=binding, relative=relative,
                target=target, fingerprint=remote_fingerprint,
                revision=revision,
            )
            return SyncOpOutcome(
                op_id=0, op_type="remote-upsert",
                relative_path=relative, status="skipped",
            )

        # Local has different bytes than both the previous baseline AND
        # the remote latest version → §D4 keep-both: rename the local
        # copy aside before the download lands. When the keyed
        # fingerprint can't be computed (read error, no master key) we
        # fail closed and treat the file as modified — better to keep
        # both than to silently overwrite the user's local edit.
        baseline_fp = local_entry.content_fingerprint if local_entry else ""
        local_modified = (
            local_fp is None
            or local_fp != baseline_fp
        )
        if local_fp is None:
            log.warning(
                "vault.sync.twoway_local_fingerprint_unreadable "
                "binding=%s path=%s",
                binding.binding_id, relative,
            )
        if local_modified:
            conflict_relative = _unique_conflict_path(
                # F-Y29: callers always pass binding.local_path; the
                # earlier `target.parent.parent if False else …` was
                # leftover dead code.
                local_root=Path(binding.local_path),
                relative_path=relative, device_name=device_name,
            )
            conflict_target = Path(binding.local_path) / conflict_relative
            conflict_target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(target), str(conflict_target))
                fsync_dir(conflict_target.parent)
            except OSError as exc:
                log.warning(
                    "vault.sync.twoway_conflict_move_failed binding=%s "
                    "src=%s dst=%s error=%s",
                    binding.binding_id, target, conflict_target, exc,
                )
                return SyncOpOutcome(
                    op_id=0, op_type="remote-upsert",
                    relative_path=relative, status="failed",
                    error=f"conflict_move: {exc}",
                )
            # Register the conflict copy as an unsynced extra so the
            # watcher's next upload pass pushes it back to remote.
            try:
                stat = conflict_target.stat()
                conflict_size = int(stat.st_size)
                conflict_mtime = int(stat.st_mtime_ns)
            except OSError:
                conflict_size, conflict_mtime = 0, 0
            store.upsert_local_entry(VaultLocalEntry(
                binding_id=binding.binding_id,
                relative_path=conflict_relative,
                content_fingerprint="",
                size_bytes=conflict_size,
                mtime_ns=conflict_mtime,
                last_synced_revision=0,
            ))
            store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload", relative_path=conflict_relative,
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        download_latest_file(
            vault=vault,
            relay=relay,
            manifest=manifest,
            path=display_path,
            destination=target,
            existing_policy="overwrite",
            chunk_cache_dir=cache_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.sync.twoway_download_failed binding=%s path=%s error=%s",
            binding.binding_id, relative, exc,
        )
        return SyncOpOutcome(
            op_id=0, op_type="remote-upsert",
            relative_path=relative, status="failed",
            error=str(exc),
        )

    _stamp_local_entry(
        store=store, binding=binding, relative=relative,
        target=target, fingerprint=remote_fingerprint, revision=revision,
    )
    return SyncOpOutcome(
        op_id=0, op_type="remote-upsert",
        relative_path=relative, status="uploaded",
        bytes_uploaded=remote_logical_size,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_folder(
    manifest: dict[str, Any], remote_folder_id: str,
) -> dict[str, Any] | None:
    for folder in manifest.get("remote_folders", []) or []:
        if (
            isinstance(folder, dict)
            and folder.get("remote_folder_id") == remote_folder_id
        ):
            return folder
    return None


def _latest_version(entry: dict[str, Any]) -> dict[str, Any] | None:
    versions = [v for v in entry.get("versions", []) or [] if isinstance(v, dict)]
    latest_id = str(entry.get("latest_version_id") or "")
    if latest_id:
        for v in versions:
            if str(v.get("version_id", "")) == latest_id:
                return v
    return versions[-1] if versions else None


def _content_fingerprint_key(vault: SyncVault) -> bytes | None:
    try:
        from .vault_crypto import derive_content_fingerprint_key
    except ImportError:
        return None
    master = vault.master_key
    if not master:
        return None
    try:
        return derive_content_fingerprint_key(master)
    except Exception:  # noqa: BLE001
        return None


def _file_keyed_fingerprint(
    path: Path, fingerprint_key: bytes | None,
) -> str | None:
    if fingerprint_key is None:
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.digest()
    except OSError:
        return None
    try:
        from .vault_crypto import make_content_fingerprint
        return make_content_fingerprint(fingerprint_key, sha)
    except Exception:  # noqa: BLE001
        return None


def _stamp_local_entry(
    *,
    store: VaultBindingsStore,
    binding: VaultBinding,
    relative: str,
    target: Path,
    fingerprint: str,
    revision: int,
) -> None:
    try:
        stat = target.stat()
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        size, mtime_ns = 0, 0
    store.upsert_local_entry(VaultLocalEntry(
        binding_id=binding.binding_id,
        relative_path=relative,
        content_fingerprint=fingerprint,
        size_bytes=size,
        mtime_ns=mtime_ns,
        last_synced_revision=int(revision),
    ))


def _unique_conflict_path(
    *,
    local_root: Path,
    relative_path: str,
    device_name: str,
) -> str:
    """Pick an §A20 conflict path that doesn't already exist under ``local_root``."""
    when = datetime.now(timezone.utc)
    candidate = make_conflict_path(
        original_path=relative_path,
        kind="synced",
        device_name=device_name,
        when=when,
    )
    while (local_root / candidate).exists():
        candidate = make_conflict_path(
            original_path=candidate,
            kind="synced",
            device_name=device_name,
            when=when,
        )
    return candidate


__all__ = [
    "MAX_TWO_WAY_ITERATIONS",
    "run_two_way_cycle",
]
