"""Vault eviction pass (T7.5) — §D2 strict-order space reclaim.

The pass walks four stages until either the requested byte target is
freed or the vault has no more historical material to drop:

1. Hard-purge expired tombstones (``recoverable_until < now``)
2. Hard-purge unexpired tombstones, oldest ``deleted_at`` first
3. Hard-purge oldest historical version of each multi-version live file
4. No candidates remain → caller surfaces the §D2 step-4 banner

Each stage is a transaction sequence:

- pure helper picks candidate chunk_ids + per-folder shard mutations
- relay ``gc_plan`` resolves which of those the server is willing to drop
  (e.g. shared chunks may still be referenced by another folder)
- relay ``gc_execute`` actually deletes the ciphertext
- per affected folder: build the post-purge shard, CAS-publish via
  ``publish_shard_with_root`` so other devices see the cleaned state.
  Phase H step 7d: the pre-port path published one vault-wide manifest
  revision per stage; the sharded path publishes one shard revision
  per affected folder. Stage atomicity downgrades from vault-wide to
  per-folder — a crash between two folders' publishes leaves the
  earlier one purged and the later one still expired; the next
  eviction run picks up the residue.

Activity-log events are emitted via standard ``logging``: every stage
that does work logs ``vault.eviction.<event>`` so the diagnostics
catalog stays in one place.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from ..binding.lifecycle import SyncCancelledError
from ..manifest import (
    assemble_unified_manifest,
    compute_recoverable_until,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
    DEFAULT_RETENTION_POLICY,
)
from ..relay_errors import VaultCASConflictError
from ..upload.folder_state import (
    FolderState,
    fetch_folder_state,
    find_root_folder_pointer,
)


log = logging.getLogger(__name__)

CAS_MAX_RETRIES = 5


class EvictionVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_unified_manifest(self, relay, *, local_index=None) -> dict: ...

    def fetch_root_manifest(self, relay, *, local_index=None) -> dict: ...

    def fetch_folder_shard(
        self, relay, remote_folder_id: str, *,
        expected_shard_hash: str | None = None,
    ) -> dict: ...

    def publish_shard_with_root(
        self, relay, remote_folder_id: str,
        shard: dict, root: dict,
    ) -> tuple[dict, dict]: ...

    def decrypt_root_envelope(self, envelope_bytes: bytes) -> dict: ...

    def decrypt_shard_envelope(
        self, envelope_bytes: bytes, remote_folder_id: str,
    ) -> dict: ...


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

    Phase H step 7d: ``manifest`` is accepted as the caller's view of
    state (e.g. the assembled unified manifest the wizard was
    rendering) but is re-read fresh per stage from the sharded relay
    surface. Each stage publishes one shard revision per affected
    folder via ``publish_shard_with_root``.

    F-U03: ``should_continue`` is checked between every stage. Each
    stage's per-folder publishes are independent CAS units, so a
    Cancel between folders leaves the already-purged shards purged
    and skips the rest; the next eviction run picks up where we left
    off.
    """
    current_manifest = manifest
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

    # Stage 2 — unexpired tombstones, oldest deleted_at first.
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

    # Stage 4 — nothing left to drop.
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
    # Per-folder shard mutations: folder_id → callable that takes the
    # current shard plaintext and returns the post-purge shard plaintext.
    per_folder_mutations: dict[str, Callable[[dict[str, Any], set[str]], dict[str, Any]]]


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

    # Use the unified manifest's revision as the plan-revision marker.
    # The server tracks gc plans by an opaque manifest_revision; for
    # the sharded path this is just a freshness anchor.
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
        bytes_freed = sum(batch.chunk_sizes.get(c, 0) for c in safe_to_delete)
    if deleted_count == 0:
        deleted_count = len(safe_to_delete)

    purged = set(safe_to_delete)

    # Phase H step 7d: per affected folder, fetch state + apply mutation +
    # publish_shard_with_root. Stage atomicity downgrades from vault-wide
    # to per-folder; gc_execute already deleted the chunks so the
    # shard mutations can lag if a crash interrupts mid-stage.
    for folder_id, mutate in batch.per_folder_mutations.items():
        _publish_folder_purge_with_retry(
            vault=vault,
            relay=relay,
            remote_folder_id=folder_id,
            mutate=mutate,
            purged=purged,
            author_device_id=author_device_id,
        )

    # Re-assemble a unified manifest from the post-publish state so the
    # caller's next stage_fn sees the post-purge view.
    post_manifest = vault.fetch_unified_manifest(relay, local_index=local_index)

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
        post_manifest,
    )


def _publish_folder_purge_with_retry(
    *,
    vault: EvictionVault,
    relay: EvictionRelay,
    remote_folder_id: str,
    mutate: Callable[[dict[str, Any], set[str]], dict[str, Any]],
    purged: set[str],
    author_device_id: str,
    max_retries: int = CAS_MAX_RETRIES,
) -> FolderState:
    """Fetch one folder's state, apply the purge mutation, publish via
    ``publish_shard_with_root``. CAS retries decrypt the conflict
    envelope and re-apply ``mutate`` on the rebased shard."""
    state = fetch_folder_state(vault, relay, remote_folder_id, author_device_id)
    for attempt in range(max_retries):
        candidate_shard, candidate_root = _build_candidate(
            state, remote_folder_id, author_device_id, mutate, purged,
        )
        try:
            shard_out, root_out = vault.publish_shard_with_root(
                relay, remote_folder_id, candidate_shard, candidate_root,
            )
            return FolderState(root=root_out, shard=shard_out)
        except VaultCASConflictError as exc:
            shard_envelope = exc.current_shard_ciphertext_bytes()
            root_envelope = exc.current_root_ciphertext_bytes()
            if not shard_envelope and not root_envelope:
                raise
            new_shard = (
                vault.decrypt_shard_envelope(shard_envelope, remote_folder_id)
                if shard_envelope else state.shard
            )
            new_root = (
                vault.decrypt_root_envelope(root_envelope)
                if root_envelope else state.root
            )
            log.info(
                "vault.eviction.cas_retry attempt=%d/%d folder=%s "
                "shard_conflict=%s root_conflict=%s",
                attempt + 1, max_retries, remote_folder_id,
                bool(shard_envelope), bool(root_envelope),
            )
            state = FolderState(root=new_root, shard=new_shard)
    # F-D25: one final attempt; exhaustion-tag if still 409.
    candidate_shard, candidate_root = _build_candidate(
        state, remote_folder_id, author_device_id, mutate, purged,
    )
    try:
        shard_out, root_out = vault.publish_shard_with_root(
            relay, remote_folder_id, candidate_shard, candidate_root,
        )
        return FolderState(root=root_out, shard=shard_out)
    except VaultCASConflictError:
        log.warning(
            "vault.eviction.cas_exhausted vault=%s folder=%s retries=%d",
            getattr(vault, "vault_id", "?"), remote_folder_id, max_retries,
        )
        raise


def _build_candidate(
    state: FolderState,
    remote_folder_id: str,
    author_device_id: str,
    mutate: Callable[[dict[str, Any], set[str]], dict[str, Any]],
    purged: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    created_at = _now_rfc3339()
    parent_n = normalize_shard_plaintext(state.shard)
    parent_revision = int(parent_n.get("shard_revision", 0))
    mutated = mutate(parent_n, purged)
    mutated["shard_revision"] = parent_revision + 1
    mutated["parent_shard_revision"] = parent_revision
    mutated["created_at"] = created_at
    mutated["author_device_id"] = str(author_device_id)
    mutated["remote_folder_id"] = remote_folder_id

    root_n = normalize_root_manifest_plaintext(state.root)
    parent_root_revision = int(root_n.get("root_revision", 0))
    candidate_root = dict(root_n)
    candidate_root["root_revision"] = parent_root_revision + 1
    candidate_root["parent_root_revision"] = parent_root_revision
    candidate_root["created_at"] = created_at
    candidate_root["author_device_id"] = str(author_device_id)
    return mutated, candidate_root


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
    # Per-folder targets: folder_id -> set of (entry_path,) tuples to drop.
    targets_by_folder: dict[str, set[str]] = {}

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
            targets_by_folder.setdefault(folder_id, set()).add(str(entry.get("path", "")))
            paths.append(str(entry.get("path", "")))

    if not chunk_ids:
        return None

    per_folder_mutations = {
        folder_id: _make_drop_tombstoned_mutation(target_paths)
        for folder_id, target_paths in targets_by_folder.items()
    }

    return _StageBatch(
        chunk_ids=chunk_ids,
        affected_paths=paths,
        chunk_sizes=sizes,
        per_folder_mutations=per_folder_mutations,
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

    return _StageBatch(
        chunk_ids=chunk_ids,
        affected_paths=[path],
        chunk_sizes=sizes,
        per_folder_mutations={folder_id: _make_drop_tombstoned_mutation({path})},
    )


def _oldest_version_candidates(manifest: dict[str, Any]) -> _StageBatch | None:
    """Pick the single oldest non-current version among all live files."""
    best: tuple[str, str, str, str, dict[str, Any], dict[str, Any]] | None = None

    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or bool(entry.get("deleted")):
                continue
            versions = [v for v in entry.get("versions", []) or [] if isinstance(v, dict)]
            if len(versions) < 2:
                continue
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

    return _StageBatch(
        chunk_ids=chunk_ids,
        affected_paths=[path],
        chunk_sizes=sizes,
        per_folder_mutations={
            folder_id: _make_drop_version_mutation(path, version_id),
        },
    )


# ---------------------------------------------------------------------------
# Shard mutation factories
# ---------------------------------------------------------------------------


def _make_drop_tombstoned_mutation(
    target_paths: set[str],
) -> Callable[[dict[str, Any], set[str]], dict[str, Any]]:
    """Build a shard-mutation closure that drops tombstoned entries at
    ``target_paths`` whose chunks were purged.

    The ``purged`` set is supplied at call time so the closure can be
    re-applied with a fresh server head on CAS retry.
    """
    def mutate(shard: dict[str, Any], _purged: set[str]) -> dict[str, Any]:
        out = normalize_shard_plaintext(shard)
        kept_entries = []
        for entry in out.get("entries", []) or []:
            if not isinstance(entry, dict):
                kept_entries.append(entry)
                continue
            entry_path = str(entry.get("path", ""))
            if entry_path in target_paths and bool(entry.get("deleted")):
                continue
            kept_entries.append(entry)
        out["entries"] = kept_entries
        return out
    return mutate


def _make_drop_version_mutation(
    path: str, version_id: str,
) -> Callable[[dict[str, Any], set[str]], dict[str, Any]]:
    """Build a shard-mutation closure that drops one historical version
    from the live entry at ``path``."""
    def mutate(shard: dict[str, Any], _purged: set[str]) -> dict[str, Any]:
        out = normalize_shard_plaintext(shard)
        for entry in out.get("entries", []) or []:
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
    return mutate


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
