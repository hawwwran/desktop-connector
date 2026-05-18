"""Vault eviction pass — v1 age-ordered auto-purge + alarm cleanup.

The pass runs two tracks:

1. **Stage 1 (housekeeping)** — expired tombstones
   (``recoverable_until < now``). Always safe, auto-runs on every sync
   pass. Event: ``vault.eviction.tombstone_purged_expired``.
2. **Destructive purge** — unexpired tombstones and oldest historical
   versions of multi-version live files combined into one oldest-first
   iterator. Fires only when ``target_bytes_to_free > 0``. Bound is
   per-call: the loop stops as soon as the target is met. Two modes
   distinguish the audit signal:

   - ``mode="auto"`` (default): silent transparent purge to fit a
     pending upload. Event: ``vault.eviction.auto_purged_oldest``.
   - ``mode="alarm"``: passphrase-gated cleanup after the relay reports
     ``used > quota`` (quota-shrink / tamper signal). Event:
     ``vault.eviction.alarm_purged_oldest``.

   No more candidates → caller surfaces the "vault full, no backup
   history remains" banner.

Each loop iteration is a transaction sequence:

- pure helper picks the single oldest destructive candidate (across
  tombstones + versions) + the per-folder shard mutation to drop it
- relay ``gc_plan`` resolves which chunks the server is willing to drop
  (e.g. shared chunks may still be referenced by another folder)
- relay ``gc_execute`` actually deletes the ciphertext
- per affected folder: build the post-purge shard, CAS-publish via
  ``publish_shard_with_root`` so other devices see the cleaned state.
  Stage atomicity is per-folder — a crash between folders' publishes
  leaves the earlier one purged and the later one still expired; the
  next eviction run picks up the residue.

Activity-log events are emitted via standard ``logging``: every stage
that does work logs ``vault.eviction.<event>`` so the diagnostics
catalog stays in one place.

ADR: see ``docs/architecture-decisions.md`` ``2026-05-18 — Eviction
policy: age-ordered auto-purge with quota-shrink passphrase gate``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol

from ..binding.lifecycle import SyncCancelledError
from ..manifest import (
    compute_recoverable_until,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
    DEFAULT_RETENTION_POLICY,
)
from ..relay_errors import VaultCASConflictError
from ..upload.folder_state import (
    FolderState,
    fetch_folder_state,
)


log = logging.getLogger(__name__)

CAS_MAX_RETRIES = 5

EvictionMode = Literal["auto", "alarm"]

_DESTRUCTIVE_EVENT: dict[str, str] = {
    "auto": "vault.eviction.auto_purged_oldest",
    "alarm": "vault.eviction.alarm_purged_oldest",
}


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
        purpose: str = "sync",
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
    mode: EvictionMode = "auto",
    now_iso: str | None = None,
    local_index: Any = None,
    should_continue: Callable[[], bool] | None = None,
) -> EvictionResult:
    """Run the v1 eviction pipeline until ``target_bytes_to_free`` is reached.

    ``target_bytes_to_free=0`` runs the housekeeping subset (stage 1
    only — expired tombstones) per §A16's sync-driven flow. A positive
    target switches on the destructive age-ordered loop, which
    interleaves unexpired tombstones (sorted by ``deleted_at``) and
    oldest non-current versions of multi-version live files (sorted by
    ``created_at``), purging one at a time until the target is met or
    the manifest has no more destructive candidates.

    ``mode`` selects the destructive event vocabulary:

    - ``"auto"`` (default): silent retry path for a 507 where
      ``used + size > quota`` but ``used ≤ quota``. Emits
      ``vault.eviction.auto_purged_oldest``.
    - ``"alarm"``: passphrase-gated cleanup after the relay reports
      ``used > quota`` (a quota-shrink or tamper signal). Emits
      ``vault.eviction.alarm_purged_oldest`` so audit logs can
      distinguish "auto-purged for fitting an upload" from "alarm
      cleanup after detected shrink".

    Phase H step 7d: ``manifest`` is accepted as the caller's view of
    state (e.g. the assembled unified manifest the wizard was
    rendering) but is re-read fresh per iteration from the sharded
    relay surface. Each iteration publishes one shard revision per
    affected folder via ``publish_shard_with_root``.

    F-U03: ``should_continue`` is checked between every iteration.
    Each iteration's per-folder publishes are independent CAS units,
    so a Cancel between iterations leaves the already-purged shards
    purged and skips the rest; the next eviction run picks up where
    we left off.
    """
    current_manifest = manifest
    bytes_freed = 0
    chunks_freed = 0
    stages: list[EvictionStageResult] = []
    now = now_iso or _now_rfc3339()
    destructive_event = _DESTRUCTIVE_EVENT[mode]

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
        # Review §4.H4: when stage 1 finishes cleanup-only (the
        # shard had stale references but no expired tombstones to
        # actually evict), surface that to the operator BEFORE we
        # cascade into the destructive purge. The user clicked
        # "Free X bytes" — they should know the housekeeping pass
        # freed nothing and we're about to destroy unexpired
        # tombstones / old versions to make space.
        if (
            stage_1.bytes_freed == 0
            and stage_1.chunks_freed == 0
            and target_bytes_to_free > 0
        ):
            log.warning(
                "vault.eviction.cleanup_only_cascade_to_force "
                "target=%d freed_bytes=0 — stage_1 was cleanup-only, "
                "escalating to destructive purge",
                target_bytes_to_free,
            )

    # Sync-driven housekeeping stops after stage 1 — no destructive purge.
    if target_bytes_to_free <= 0:
        return EvictionResult(
            manifest=current_manifest,
            bytes_freed=bytes_freed,
            chunks_freed=chunks_freed,
            stages=stages,
            no_more_candidates=False,
        )

    if bytes_freed >= target_bytes_to_free:
        return EvictionResult(
            manifest=current_manifest,
            bytes_freed=bytes_freed,
            chunks_freed=chunks_freed,
            stages=stages,
            no_more_candidates=False,
        )

    # Destructive age-ordered loop. Each iteration picks the single
    # oldest candidate (across unexpired tombstones + non-latest
    # versions of multi-version live files), runs the
    # admin-gated gc_plan + gc_execute, and publishes the shard
    # mutation. Loop stops as soon as the target is met — "no
    # batching, no slack" per the v1 design so a compromised path
    # can only free as much as one upload reserves.
    while bytes_freed < target_bytes_to_free:
        _check_cancel("destructive_purge")
        stage_n, current_manifest = _run_stage(
            vault=vault,
            relay=relay,
            manifest=current_manifest,
            author_device_id=author_device_id,
            candidates_fn=_next_destructive_candidate,
            event=destructive_event,
            local_index=local_index,
            purpose="forced_eviction",
        )
        if stage_n is None:
            # Iterator exhausted (or the candidate's chunks were all
            # still-referenced and nothing changed) — fall through
            # to the no-more-candidates terminal.
            break
        stages.append(stage_n)
        bytes_freed += stage_n.bytes_freed
        chunks_freed += stage_n.chunks_freed

    if bytes_freed >= target_bytes_to_free:
        return EvictionResult(
            manifest=current_manifest,
            bytes_freed=bytes_freed,
            chunks_freed=chunks_freed,
            stages=stages,
            no_more_candidates=False,
        )

    # No destructive material left — terminal banner.
    log.info(
        "vault.eviction.no_more_candidates target=%d freed=%d",
        target_bytes_to_free, bytes_freed,
    )
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
    purpose: str = "sync",
) -> tuple[EvictionStageResult | None, dict[str, Any]]:
    """Execute one eviction stage; return result + (possibly) updated manifest.

    ``purpose='forced_eviction'`` flags stages 2/3 as hard-purges so the
    relay can gate them behind role=admin (review §3.C1); stage 1
    expired-tombstone housekeeping keeps the default sync role.
    """
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
        purpose=purpose,
    )
    safe_to_delete = list(plan.get("safe_to_delete") or [])
    # Phase H step 7d crash-recovery: ``already_deleted_chunk_ids`` is the
    # server's "chunks you asked about don't exist anymore" signal — used
    # when a prior eviction ran ``gc_execute`` but crashed before
    # publishing the shard cleanup. The current run's per-folder mutation
    # uses ``purged = safe_to_delete ∪ already_deleted`` so stale shard
    # entries pointing at deleted chunks are cleaned without re-running
    # ``gc_execute``.
    already_deleted = list(plan.get("already_deleted_chunk_ids") or [])
    if not safe_to_delete and not already_deleted:
        return None, manifest

    cleanup_only = not safe_to_delete and bool(already_deleted)

    if cleanup_only:
        bytes_freed = 0
        deleted_count = 0
    else:
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

    purged = set(safe_to_delete) | set(already_deleted)

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

    if cleanup_only:
        log.info(
            "vault.eviction.shard_cleanup_only event=%s stale_chunk_refs=%d paths=%d",
            event, len(already_deleted), len(batch.affected_paths),
        )
    else:
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
            is_last = attempt == max_retries - 1
            if is_last:
                log.warning(
                    "vault.eviction.cas_exhausted vault=%s folder=%s attempts=%d",
                    getattr(vault, "vault_id", "?"), remote_folder_id, max_retries,
                )
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
    raise AssertionError("unreachable: loop exits via return or raise")


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


def _next_destructive_candidate(manifest: dict[str, Any]) -> _StageBatch | None:
    """Pick the single oldest destructive candidate across the vault.

    Interleaves two candidate sources, sorted oldest-first by a unified
    timestamp key:

    - **Unexpired tombstones** (``entry.deleted=True`` and still inside
      the recoverable grace window), keyed by ``deleted_at``. Returns
      every chunk across every version of the entry; the matching
      shard mutation drops the entry outright.
    - **Non-current versions** of multi-version live files
      (``entry.deleted=False`` and ``len(versions) >= 2``), keyed by
      that version's ``modified_at`` / ``created_at``. Returns the
      version's chunks only; the matching shard mutation drops the
      single version from the entry.

    Matches the v1 design's "drop the stalest data first" iterator —
    a 6-month-old v1 of a still-live file is purged before a 3-day-old
    Trash entry, preserving recently-deleted-but-recoverable files
    longer.
    """
    best: tuple[
        str,                                # sort_key
        str,                                # kind: "tombstone" | "version"
        str,                                # folder_id
        str,                                # path
        dict[str, Any] | None,              # entry (tombstone kind)
        dict[str, Any] | None,              # version (version kind)
        str,                                # version_id (version kind)
    ] | None = None

    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", ""))
            if bool(entry.get("deleted")):
                deleted_at = str(entry.get("deleted_at") or "")
                if not deleted_at:
                    continue
                if best is None or deleted_at < best[0]:
                    best = (
                        deleted_at, "tombstone", folder_id, path,
                        entry, None, "",
                    )
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
                    sort_key, "version", folder_id, path,
                    None, oldest, str(oldest.get("version_id", "")),
                )

    if best is None:
        return None

    _, kind, folder_id, path, entry, version, version_id = best

    if kind == "tombstone":
        assert entry is not None
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

    # kind == "version"
    assert version is not None
    chunk_ids = []
    sizes = {}
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
    "EvictionMode",
    "EvictionResult",
    "EvictionStageResult",
    "eviction_pass",
]
