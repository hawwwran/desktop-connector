"""Vault eviction pass (T7.5) — §D2 strict-order space reclaim.

The pass walks four stages until either the requested byte target is
freed or the vault has no more historical material to drop:

1. Hard-purge expired tombstones (``recoverable_until < now``)
2. Hard-purge unexpired tombstones, oldest ``deleted_at`` first
3. Hard-purge oldest historical version of each multi-version live file
4. No candidates remain → caller surfaces the §D2 step-4 banner

Each stage is a self-contained transaction:

- pure helper picks candidate chunk_ids + corresponding manifest mutation
- relay ``gc_plan`` resolves which of those the server is willing to drop
  (e.g. shared chunks may still be referenced by another folder)
- relay ``gc_execute`` actually deletes the ciphertext
- the local manifest is mutated (chunks rewritten, versions/entries
  pruned) and CAS-published so other devices see the cleaned state

Activity-log events are emitted via standard ``logging``: every stage
that does work logs ``vault.eviction.<event>`` so the diagnostics
catalog stays in one place.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from .vault_binding_lifecycle import SyncCancelledError
from .vault_browser_model import decrypt_manifest as decrypt_manifest_envelope
from .vault_manifest import (
    compute_recoverable_until,
    normalize_manifest_plaintext,
    DEFAULT_RETENTION_POLICY,
)
from .vault_relay_errors import VaultCASConflictError


log = logging.getLogger(__name__)

CAS_MAX_RETRIES = 5


class EvictionVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_manifest(self, relay, *, local_index=None) -> dict: ...

    def publish_manifest(self, relay, manifest, *, local_index=None) -> dict: ...


class EvictionRelay(Protocol):
    def gc_plan(
        self,
        vault_id: str,
        vault_access_secret: str,
        *,
        manifest_revision: int,
        candidate_chunk_ids: list[str],
    ) -> dict[str, Any]: ...

    def gc_execute(
        self,
        vault_id: str,
        vault_access_secret: str,
        *,
        plan_id: str,
        purge_secret: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class EvictionStageResult:
    event: str
    chunks_freed: int
    bytes_freed: int
    affected_paths: list[str] = field(default_factory=list)


@dataclass
class EvictionResult:
    manifest: dict[str, Any]
    bytes_freed: int
    chunks_freed: int
    stages: list[EvictionStageResult] = field(default_factory=list)
    no_more_candidates: bool = False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def eviction_pass(
    *,
    vault: EvictionVault,
    relay: EvictionRelay,
    manifest: dict[str, Any],
    author_device_id: str,
    target_bytes_to_free: int = 0,
    now_iso: str | None = None,
    local_index: Any = None,
    should_continue: Callable[[], bool] | None = None,
) -> EvictionResult:
    """Run the §D2 eviction pipeline until ``target_bytes_to_free`` is reached.

    ``target_bytes_to_free=0`` runs the housekeeping subset (stage 1
    only — expired tombstones) per §A16's sync-driven flow. A positive
    target is the eviction-driven flow that backs T6.6's 507 prompt;
    stages 2 and 3 only fire when stage 1 didn't free enough.

    The caller decides whether to prompt the user before invoking; this
    function performs the work and returns what was freed.

    F-U03: ``should_continue`` is checked between every stage. Each
    stage publishes a single manifest revision atomically, so a Cancel
    between stages leaves the vault in a coherent state — anything
    freed by completed stages stays freed; remaining stages don't run.
    Mid-stage cancel is intentionally not supported (it would require
    unwinding a partial gc_execute + manifest publish).
    """
    current_manifest = normalize_manifest_plaintext(manifest)
    bytes_freed = 0
    chunks_freed = 0
    stages: list[EvictionStageResult] = []
    now = now_iso or _now_rfc3339()

    def _check_cancel(stage_label: str) -> None:
        if should_continue is not None and not should_continue():
            log.info(
                "vault.eviction.cancelled vault=%s before_stage=%s freed_bytes=%d",
                vault.vault_id, stage_label, bytes_freed,
            )
            raise SyncCancelledError(
                f"eviction cancelled before {stage_label} (freed {bytes_freed} bytes)"
            )

    _check_cancel("stage_1")

    # Stage 1 — expired tombstones (always safe).
    stage_1, current_manifest = _run_stage(
        vault=vault,
        relay=relay,
        manifest=current_manifest,
        author_device_id=author_device_id,
        candidates_fn=lambda m: _expired_tombstone_candidates(m, now_iso=now),
        event="vault.eviction.tombstone_purged_expired",
        local_index=local_index,
    )
    if stage_1 is not None:
        stages.append(stage_1)
        bytes_freed += stage_1.bytes_freed
        chunks_freed += stage_1.chunks_freed

    if target_bytes_to_free > 0 and bytes_freed >= target_bytes_to_free:
        return EvictionResult(
            manifest=current_manifest,
            bytes_freed=bytes_freed,
            chunks_freed=chunks_freed,
            stages=stages,
            no_more_candidates=False,
        )

    # Sync-driven housekeeping stops after stage 1 — no force-purge.
    if target_bytes_to_free <= 0:
        return EvictionResult(
            manifest=current_manifest,
            bytes_freed=bytes_freed,
            chunks_freed=chunks_freed,
            stages=stages,
            no_more_candidates=False,
        )

    _check_cancel("stage_2")

    # Stage 2 — unexpired tombstones, oldest deleted_at first. User has
    # been warned via the §D2 100% banner; we surface the per-purge
    # event in the activity log per spec.
    stage_2, current_manifest = _run_stage(
        vault=vault,
        relay=relay,
        manifest=current_manifest,
        author_device_id=author_device_id,
        candidates_fn=_unexpired_tombstone_candidates,
        event="vault.eviction.tombstone_purged_early",
        local_index=local_index,
    )
    if stage_2 is not None:
        stages.append(stage_2)
        bytes_freed += stage_2.bytes_freed
        chunks_freed += stage_2.chunks_freed

    if bytes_freed >= target_bytes_to_free:
        return EvictionResult(
            manifest=current_manifest,
            bytes_freed=bytes_freed,
            chunks_freed=chunks_freed,
            stages=stages,
            no_more_candidates=False,
        )

    _check_cancel("stage_3")

    # Stage 3 — oldest historical version of multi-version live files.
    stage_3, current_manifest = _run_stage(
        vault=vault,
        relay=relay,
        manifest=current_manifest,
        author_device_id=author_device_id,
        candidates_fn=_oldest_version_candidates,
        event="vault.eviction.version_purged",
        local_index=local_index,
    )
    if stage_3 is not None:
        stages.append(stage_3)
        bytes_freed += stage_3.bytes_freed
        chunks_freed += stage_3.chunks_freed

    if bytes_freed >= target_bytes_to_free:
        return EvictionResult(
            manifest=current_manifest,
            bytes_freed=bytes_freed,
            chunks_freed=chunks_freed,
            stages=stages,
            no_more_candidates=False,
        )

    # Stage 4 — nothing left to drop. Caller surfaces the sync-stop banner.
    log.info("vault.eviction.no_more_candidates target=%d freed=%d", target_bytes_to_free, bytes_freed)
    return EvictionResult(
        manifest=current_manifest,
        bytes_freed=bytes_freed,
        chunks_freed=chunks_freed,
        stages=stages,
        no_more_candidates=True,
    )


# ---------------------------------------------------------------------------
# Stage runner + helpers
# ---------------------------------------------------------------------------


@dataclass
class _StageBatch:
    chunk_ids: list[str]
    affected_paths: list[str]
    chunk_sizes: dict[str, int]  # ciphertext_size by chunk_id (for accounting)
    apply_purge: Callable[[dict, set[str]], dict]


CandidatesFn = Callable[[dict[str, Any]], _StageBatch | None]


def _run_stage(
    *,
    vault: EvictionVault,
    relay: EvictionRelay,
    manifest: dict[str, Any],
    author_device_id: str,
    candidates_fn: CandidatesFn,
    event: str,
    local_index: Any,
) -> tuple[EvictionStageResult | None, dict[str, Any]]:
    """Execute one eviction stage; return result + (possibly) updated manifest."""
    batch = candidates_fn(manifest)
    if batch is None or not batch.chunk_ids:
        return None, manifest

    revision = int(manifest.get("revision", 0))
    plan = relay.gc_plan(
        vault.vault_id,
        vault.vault_access_secret,
        manifest_revision=revision,
        candidate_chunk_ids=list(batch.chunk_ids),
    )
    safe_to_delete = list(plan.get("safe_to_delete") or [])
    if not safe_to_delete:
        return None, manifest

    plan_id = str(plan.get("plan_id") or "")
    execute_result = relay.gc_execute(
        vault.vault_id,
        vault.vault_access_secret,
        plan_id=plan_id,
    )
    bytes_freed = int(execute_result.get("freed_ciphertext_bytes") or 0)
    deleted_count = int(execute_result.get("deleted_count") or 0)
    if bytes_freed == 0 and batch.chunk_sizes:
        # Server didn't echo bytes? Compute from our local size table for
        # accounting completeness.
        bytes_freed = sum(batch.chunk_sizes.get(c, 0) for c in safe_to_delete)
    if deleted_count == 0:
        deleted_count = len(safe_to_delete)

    purged = set(safe_to_delete)
    mutated_manifest = batch.apply_purge(manifest, purged)
    published = _publish_with_retry(
        vault=vault,
        relay=relay,
        parent_manifest=manifest,
        op=lambda parent: _bump_and_apply(parent, batch.apply_purge, purged, author_device_id),
        local_index=local_index,
    )

    log.info(
        "%s freed_bytes=%d freed_chunks=%d paths=%d",
        event,
        bytes_freed,
        deleted_count,
        len(batch.affected_paths),
    )

    return (
        EvictionStageResult(
            event=event,
            chunks_freed=deleted_count,
            bytes_freed=bytes_freed,
            affected_paths=list(batch.affected_paths),
        ),
        published,
    )


def _bump_and_apply(
    parent: dict[str, Any],
    apply_purge: Callable[[dict, set[str]], dict],
    purged: set[str],
    author_device_id: str,
) -> dict[str, Any]:
    parent_n = normalize_manifest_plaintext(parent)
    parent_revision = int(parent_n.get("revision", 0))
    out = dict(parent_n)
    out["revision"] = parent_revision + 1
    out["parent_revision"] = parent_revision
    out["created_at"] = _now_rfc3339()
    out["author_device_id"] = str(author_device_id)
    return apply_purge(out, purged)


def _publish_with_retry(
    *,
    vault: EvictionVault,
    relay: EvictionRelay,
    parent_manifest: dict[str, Any],
    op: Callable[[dict[str, Any]], dict[str, Any]],
    local_index: Any,
    max_retries: int = CAS_MAX_RETRIES,
) -> dict[str, Any]:
    candidate = op(parent_manifest)
    for _ in range(max_retries):
        try:
            return vault.publish_manifest(relay, candidate, local_index=local_index)
        except VaultCASConflictError as exc:
            envelope = exc.current_manifest_ciphertext_bytes()
            if not envelope:
                raise
            server_head = decrypt_manifest_envelope(vault, envelope)
            candidate = op(server_head)
    return vault.publish_manifest(relay, candidate, local_index=local_index)


# ---------------------------------------------------------------------------
# Stage candidate functions
# ---------------------------------------------------------------------------


def _expired_tombstone_candidates(
    manifest: dict[str, Any],
    *,
    now_iso: str,
) -> _StageBatch | None:
    """Return chunks for tombstones whose ``recoverable_until`` < now."""
    chunk_ids: list[str] = []
    sizes: dict[str, int] = {}
    paths: list[str] = []
    targets: set[tuple[str, str]] = set()  # (folder_id, entry_path)

    now_dt = _parse_iso(now_iso)
    if now_dt is None:
        return None

    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        keep_days = _keep_days(folder)
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or not bool(entry.get("deleted")):
                continue
            recoverable = str(
                entry.get("recoverable_until")
                or compute_recoverable_until(str(entry.get("deleted_at", "")), keep_days)
            )
            recoverable_dt = _parse_iso(recoverable)
            if recoverable_dt is None or recoverable_dt > now_dt:
                continue
            for cid, size in _entry_chunks(entry):
                chunk_ids.append(cid)
                sizes[cid] = size
            targets.add((folder_id, str(entry.get("path", ""))))
            paths.append(str(entry.get("path", "")))

    if not chunk_ids:
        return None

    def apply_purge(current: dict, purged: set[str]) -> dict:
        return _drop_tombstoned_entries(current, targets, purged)

    return _StageBatch(
        chunk_ids=chunk_ids,
        affected_paths=paths,
        chunk_sizes=sizes,
        apply_purge=apply_purge,
    )


def _unexpired_tombstone_candidates(manifest: dict[str, Any]) -> _StageBatch | None:
    """Pick the oldest unexpired tombstone (by ``deleted_at``) and return its chunks."""
    candidates: list[tuple[str, str, str, dict[str, Any]]] = []
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or not bool(entry.get("deleted")):
                continue
            deleted_at = str(entry.get("deleted_at") or "")
            candidates.append((deleted_at, folder_id, str(entry.get("path", "")), entry))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    deleted_at, folder_id, path, entry = candidates[0]
    chunk_ids: list[str] = []
    sizes: dict[str, int] = {}
    for cid, size in _entry_chunks(entry):
        chunk_ids.append(cid)
        sizes[cid] = size

    if not chunk_ids:
        return None

    targets = {(folder_id, path)}

    def apply_purge(current: dict, purged: set[str]) -> dict:
        return _drop_tombstoned_entries(current, targets, purged)

    return _StageBatch(
        chunk_ids=chunk_ids,
        affected_paths=[path],
        chunk_sizes=sizes,
        apply_purge=apply_purge,
    )


def _oldest_version_candidates(manifest: dict[str, Any]) -> _StageBatch | None:
    """Pick the single oldest non-current version among all live files."""
    best: tuple[str, str, str, str, dict[str, Any], dict[str, Any]] | None = None
    # (created_at_or_modified, folder_id, entry_path, version_id, entry, version)

    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or bool(entry.get("deleted")):
                continue
            versions = [v for v in entry.get("versions", []) or [] if isinstance(v, dict)]
            if len(versions) < 2:
                continue  # never drop the only / latest
            latest_id = str(entry.get("latest_version_id") or "")
            history = [v for v in versions if str(v.get("version_id", "")) != latest_id]
            if not history:
                continue
            history.sort(
                key=lambda v: str(v.get("modified_at") or v.get("created_at") or "")
            )
            oldest = history[0]
            sort_key = str(oldest.get("modified_at") or oldest.get("created_at") or "")
            if best is None or sort_key < best[0]:
                best = (
                    sort_key,
                    folder_id,
                    str(entry.get("path", "")),
                    str(oldest.get("version_id", "")),
                    entry,
                    oldest,
                )

    if best is None:
        return None
    _, folder_id, path, version_id, _entry, version = best

    chunk_ids: list[str] = []
    sizes: dict[str, int] = {}
    for chunk in version.get("chunks", []) or []:
        if not isinstance(chunk, dict):
            continue
        cid = str(chunk.get("chunk_id") or "")
        if not cid:
            continue
        chunk_ids.append(cid)
        sizes[cid] = int(chunk.get("ciphertext_size", 0))

    if not chunk_ids:
        return None

    target = (folder_id, path, version_id)

    def apply_purge(current: dict, purged: set[str]) -> dict:
        return _drop_old_version(current, target, purged)

    return _StageBatch(
        chunk_ids=chunk_ids,
        affected_paths=[path],
        chunk_sizes=sizes,
        apply_purge=apply_purge,
    )


# ---------------------------------------------------------------------------
# Manifest mutation helpers
# ---------------------------------------------------------------------------


def _drop_tombstoned_entries(
    manifest: dict[str, Any],
    targets: Iterable[tuple[str, str]],
    purged_chunk_ids: set[str],
) -> dict[str, Any]:
    """Remove tombstoned entries whose chunks are now physically purged."""
    out = normalize_manifest_plaintext(manifest)
    target_set = {(str(f), str(p)) for f, p in targets}
    for folder in out.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        kept_entries = []
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                kept_entries.append(entry)
                continue
            entry_path = str(entry.get("path", ""))
            if (folder_id, entry_path) in target_set and bool(entry.get("deleted")):
                # Drop the entry entirely once the server confirmed the chunks gone.
                continue
            kept_entries.append(entry)
        folder["entries"] = kept_entries
    return out


def _drop_old_version(
    manifest: dict[str, Any],
    target: tuple[str, str, str],
    purged_chunk_ids: set[str],
) -> dict[str, Any]:
    """Drop one historical version from a live entry."""
    out = normalize_manifest_plaintext(manifest)
    folder_id, path, version_id = target
    for folder in out.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        if str(folder.get("remote_folder_id", "")) != folder_id:
            continue
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("path", "")) != path:
                continue
            kept_versions = [
                v for v in entry.get("versions", []) or []
                if isinstance(v, dict) and str(v.get("version_id", "")) != version_id
            ]
            entry["versions"] = kept_versions
    return out


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _entry_chunks(entry: dict[str, Any]) -> list[tuple[str, int]]:
    """All distinct chunk_ids referenced by any version of ``entry``."""
    seen: dict[str, int] = {}
    for version in entry.get("versions", []) or []:
        if not isinstance(version, dict):
            continue
        for chunk in version.get("chunks", []) or []:
            if not isinstance(chunk, dict):
                continue
            cid = str(chunk.get("chunk_id") or "")
            if cid and cid not in seen:
                seen[cid] = int(chunk.get("ciphertext_size", 0))
    return list(seen.items())


def _keep_days(folder: dict[str, Any]) -> int:
    policy = folder.get("retention_policy")
    if isinstance(policy, dict):
        try:
            return int(policy.get("keep_deleted_days", DEFAULT_RETENTION_POLICY["keep_deleted_days"]))
        except (TypeError, ValueError):
            pass
    return int(DEFAULT_RETENTION_POLICY["keep_deleted_days"])


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        when = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "EvictionResult",
    "EvictionStageResult",
    "eviction_pass",
]
