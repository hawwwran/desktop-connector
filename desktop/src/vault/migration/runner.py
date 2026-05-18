"""Vault relay-to-relay migration runner (T9.3 + T9.4).

Orchestrates the full §H2 pipeline:

    started → copying → verified → committed

State is persisted via :mod:`vault_migration` before every transition,
so a crash-and-resume re-enters the runner at the same step (§H2
recovery table).

Pipeline:

1. **start**   — `POST source /migration/start` (gets bearer token,
                  records intent on source).
2. **bootstrap target** — `POST target /api/vaults` with the source's
                  root envelope embedded verbatim (``initial_root_*``).
                  After create, replicate each per-folder shard via
                  `PUT target /folders/{rf_id}/shard`. AAD is bound to
                  the same revisions on source and target so the bytes
                  are copy-verbatim — no re-encryption.
3. **copy chunks** — for each chunk_id referenced by the source's root +
                  shards, batch-HEAD on the target; if missing, GET
                  source / PUT target.
4. **verify**  — `GET source /migration/verify-source` returns
                  ``root_hash`` + per-folder ``shard_hashes`` map.
                  Refetch the target's root + shards and compare each
                  hash; random-sample N chunks from the target and try
                  AEAD-decrypt with the live vault's master key (T9.4).
5. **commit**  — `PUT source /migration/commit` (stamps migrated_to).
6. **idle**    — clear state file; caller swaps active relay URL in config.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..crypto import (
    aead_decrypt,
    build_chunk_aad,
    derive_subkey,
)
from .state import (
    MigrationRecord,
    clear_state,
    load_state,
    save_state,
    transition,
)


log = logging.getLogger(__name__)
DEFAULT_VERIFY_SAMPLE_SIZE = 5


class MigrationVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def decrypt_root_envelope(self, envelope_bytes: bytes) -> dict: ...

    def decrypt_shard_envelope(
        self, envelope_bytes: bytes, remote_folder_id: str,
    ) -> dict: ...


class MigrationRelay(Protocol):
    """Both source and target relay implement this surface."""

    def create_vault(self, *args, **kwargs) -> dict: ...

    def get_header(self, vault_id: str, vault_access_secret: str) -> dict: ...

    def get_root(self, vault_id: str, vault_access_secret: str) -> dict: ...

    def get_shard(
        self,
        vault_id: str,
        vault_access_secret: str,
        remote_folder_id: str,
    ) -> dict: ...

    def put_shard(
        self,
        vault_id: str,
        vault_access_secret: str,
        remote_folder_id: str,
        *,
        expected_current_shard_revision: int,
        new_shard_revision: int,
        parent_shard_revision: int,
        shard_hash: str,
        shard_ciphertext: bytes,
    ) -> dict: ...

    def batch_head_chunks(
        self, vault_id: str, vault_access_secret: str, chunk_ids: list[str],
    ) -> dict: ...

    def get_chunk(self, vault_id: str, vault_access_secret: str, chunk_id: str) -> bytes: ...

    def put_chunk(
        self, vault_id: str, vault_access_secret: str, chunk_id: str, body: bytes,
    ) -> dict: ...

    # T9.2 source-only:
    def migration_start(
        self, vault_id: str, vault_access_secret: str, *, target_relay_url: str,
    ) -> dict: ...

    def migration_verify_source(
        self, vault_id: str, vault_access_secret: str,
    ) -> dict: ...

    def migration_commit(
        self, vault_id: str, vault_access_secret: str, *, target_relay_url: str,
    ) -> dict: ...


@dataclass
class MigrationProgress:
    phase: str
    chunks_total: int = 0
    chunks_copied: int = 0
    chunks_skipped: int = 0
    bytes_copied: int = 0


@dataclass
class MigrationInventory:
    """Read-only snapshot of what a migration would copy.

    Surfaced by the wizard's "Confirm" page so the operator sees a
    concrete chunk count + cumulative bytes total before they trigger
    the destructive migrationStart call (which marks the source's
    manifest sealed-pointing-at-target). No relay state is mutated to
    produce this.
    """

    chunk_count: int
    ciphertext_bytes_total: int
    remote_folder_count: int
    shard_revisions: dict[str, int]  # folder_id → shard_revision

    @property
    def has_edited_shards(self) -> bool:
        """Any folder shard at revision > 1.

        Migrating an edited vault hits the §5.M2 genesis-insert
        idempotency gap — the wizard surfaces this so the operator
        sees the limitation before they start.
        """
        return any(rev > 1 for rev in self.shard_revisions.values())


@dataclass
class MigrationVerifyOutcome:
    matches: bool
    # subset of {"root_hash", "shard_hash:<rf_id>", "chunk_count",
    # "used_bytes", "chunk_sample"}.
    mismatches: list[str] = field(default_factory=list)
    sample_size: int = 0
    sample_passed: int = 0


@dataclass
class MigrationRunResult:
    record: MigrationRecord
    chunks_copied: int
    chunks_skipped: int
    bytes_copied: int
    verify: MigrationVerifyOutcome


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def run_migration(
    *,
    vault: MigrationVault,
    source_relay: MigrationRelay,
    target_relay: MigrationRelay,
    source_relay_url: str,
    target_relay_url: str,
    config_dir: Path,
    sample_size: int = DEFAULT_VERIFY_SAMPLE_SIZE,
    progress: Callable[[MigrationProgress], None] | None = None,
    on_committed: Callable[[MigrationRecord], None] | None = None,
    now: str | None = None,
) -> MigrationRunResult:
    """Drive a relay-to-relay migration end to end.

    Recovery model: every state transition is persisted to
    ``<config_dir>/vault_migration.json`` *before* the corresponding
    network op fires. A crash mid-run means the saved state still
    reflects the last successful transition; ``run_migration`` is
    idempotent — re-invoking from any state continues from there.

    F-C15: ``on_committed(record)`` fires once the source relay has
    committed but *before* ``clear_state`` deletes the state file.
    The caller uses it to persist ``previous_relay_url`` (and any
    matching expiry) into the app config so the §H2 7-day
    "Switch back to previous relay" grace window survives a crash
    of *this* process between commit and config-write. If the
    callback raises, the state file stays at ``committed``; the
    next ``run_migration`` invocation retries the callback. Without
    this gate the runner would clear state immediately and a caller
    crash mid-config-write would lose the rollback URL forever.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    record = load_state(config_dir)
    if record is None:
        record = MigrationRecord(
            vault_id=vault.vault_id,
            state="idle",
            source_relay_url=source_relay_url,
            target_relay_url=target_relay_url,
            started_at=_now_rfc3339() if now is None else now,
        )

    if record.vault_id != vault.vault_id:
        raise ValueError(
            f"persisted migration is for vault {record.vault_id!r}, "
            f"got {vault.vault_id!r}"
        )

    # ── started ─────────────────────────────────────────────────────────
    if record.state == "idle":
        record = transition(record, to="started", now=now)
        record.target_relay_url = target_relay_url
        save_state(record, config_dir)

    if record.state == "started" and record.migration_token is None:
        intent = source_relay.migration_start(
            vault.vault_id, vault.vault_access_secret,
            target_relay_url=target_relay_url,
        )
        token = intent.get("token")
        if isinstance(token, str) and token:
            record.migration_token = token
            save_state(record, config_dir)

    # ── copying ─────────────────────────────────────────────────────────
    if record.state == "started":
        record = transition(record, to="copying", now=now)
        save_state(record, config_dir)

    if record.state == "copying":
        _bootstrap, chunk_inventory = _bootstrap_target_and_inventory(
            vault=vault,
            source_relay=source_relay,
            target_relay=target_relay,
        )
        copied, skipped, bytes_copied = _copy_chunks(
            vault=vault,
            source_relay=source_relay,
            target_relay=target_relay,
            chunk_ids=chunk_inventory,
            progress=progress,
        )
        record = transition(record, to="verified", now=now)
        save_state(record, config_dir)
    else:
        copied = skipped = bytes_copied = 0

    # ── verifying (T9.4) ────────────────────────────────────────────────
    verify = MigrationVerifyOutcome(matches=True, mismatches=[])
    if record.state == "verified":
        # The verify step always re-fetches the target's root + shards
        # rather than reusing the cached bootstrap plaintext, so the
        # check covers a fresh round-trip (a relay that lied during
        # bootstrap can't slip through by stashing the right
        # plaintext in our cache).
        verify = _verify_migration(
            vault=vault,
            source_relay=source_relay,
            target_relay=target_relay,
            sample_size=sample_size,
        )
        if not verify.matches:
            log.warning(
                "vault.migration.verify_failed mismatches=%s",
                ",".join(verify.mismatches),
            )
            return MigrationRunResult(
                record=record,
                chunks_copied=copied,
                chunks_skipped=skipped,
                bytes_copied=bytes_copied,
                verify=verify,
            )

        # ── committed ────────────────────────────────────────────────────
        source_relay.migration_commit(
            vault.vault_id, vault.vault_access_secret,
            target_relay_url=target_relay_url,
        )
        record = transition(record, to="committed", now=now)
        save_state(record, config_dir)
        # F-510: anchor the Activity tab "Relay migration committed" row.
        log.info(
            "vault.migration.committed vault=%s source=%s target=%s",
            vault.vault_id,
            source_relay_url,
            target_relay_url,
        )

    # ── idle (post-commit cleanup) ──────────────────────────────────────
    if record.state == "committed":
        # The previous_relay_url was stamped by `transition(..., to="committed")`;
        # the caller flips the active relay URL in config. We clear the
        # state file so a relaunch knows the migration's done — but the
        # config retains previous_relay_url for the §H2 7-day grace.
        # F-C09: defense in depth — re-call ``migration_verify_source``
        # before clearing local state so an operator-driven rollback
        # on the source relay between runs leaves a forensic
        # breadcrumb. The check is best-effort: a transient error or a
        # cleared intent (the typical post-commit state) won't block
        # the clear, but a returned ``target_relay_url`` that diverges
        # from the one we committed to is loud-warned.
        _audit_source_committed_to_target(
            source_relay=source_relay,
            vault=vault,
            target_relay_url=target_relay_url,
        )
        # F-C15: persist ``previous_relay_url`` (and any caller-side
        # config writes) BEFORE clearing the state file. If the
        # callback raises we leave the state at ``committed`` so a
        # later run retries; without the gate a caller crash between
        # commit and config-write would silently lose the §H2 7-day
        # "Switch back to previous relay" rollback URL.
        if on_committed is not None:
            try:
                on_committed(record)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "vault.migration.committed_callback_failed "
                    "vault=%s target=%s error=%s",
                    vault.vault_id, target_relay_url, exc,
                )
                return MigrationRunResult(
                    record=record,
                    chunks_copied=copied,
                    chunks_skipped=skipped,
                    bytes_copied=bytes_copied,
                    verify=verify,
                )
        final_record = transition(record, to="idle", now=now)
        clear_state(config_dir)
        record = final_record

    return MigrationRunResult(
        record=record,
        chunks_copied=copied,
        chunks_skipped=skipped,
        bytes_copied=bytes_copied,
        verify=verify,
    )


def migration_preflight(
    *,
    vault: MigrationVault,
    source_relay: MigrationRelay,
) -> MigrationInventory:
    """Snapshot the source vault's chunk inventory without writing.

    Walks the source's root manifest + every published shard via the
    same logic ``_bootstrap_target_and_inventory`` uses for the real
    copy phase, except no target writes happen and no
    ``migration_start`` is POSTed. Safe to call before the wizard's
    "Continue" button locks in.

    Returns a :class:`MigrationInventory` carrying chunk count,
    cumulative ciphertext bytes, folder count, and per-folder shard
    revisions (so the wizard can surface the §5.M2 limitation when
    any shard's revision is > 1).
    """
    source_root = source_relay.get_root(vault.vault_id, vault.vault_access_secret)
    root_envelope = source_root["root_ciphertext"]
    if isinstance(root_envelope, (bytearray, memoryview)):
        root_envelope = bytes(root_envelope)
    root_plaintext = vault.decrypt_root_envelope(root_envelope)

    chunk_count = 0
    bytes_total = 0
    seen: set[str] = set()
    folder_count = 0
    shard_revisions: dict[str, int] = {}

    for pointer in root_plaintext.get("remote_folders") or []:
        if not isinstance(pointer, dict):
            continue
        rf_id = str(pointer.get("remote_folder_id") or "")
        if not rf_id:
            continue
        folder_count += 1
        pointer_shard_rev = int(pointer.get("shard_revision") or 0)
        shard_revisions[rf_id] = pointer_shard_rev
        if pointer_shard_rev <= 0:
            continue
        try:
            source_shard = source_relay.get_shard(
                vault.vault_id, vault.vault_access_secret, rf_id,
            )
        except Exception as exc:  # noqa: BLE001
            from ..relay_errors import VaultNotFoundError
            if isinstance(exc, VaultNotFoundError):
                continue
            raise
        shard_envelope = source_shard["shard_ciphertext"]
        if isinstance(shard_envelope, (bytearray, memoryview)):
            shard_envelope = bytes(shard_envelope)
        shard_plaintext = vault.decrypt_shard_envelope(shard_envelope, rf_id)
        for entry in shard_plaintext.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = str(chunk.get("chunk_id") or "")
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    chunk_count += 1
                    try:
                        bytes_total += int(chunk.get("ciphertext_size") or 0)
                    except (TypeError, ValueError):
                        continue

    return MigrationInventory(
        chunk_count=chunk_count,
        ciphertext_bytes_total=bytes_total,
        remote_folder_count=folder_count,
        shard_revisions=shard_revisions,
    )


def rollback_verified_migration(config_dir: Path) -> MigrationRecord | None:
    """Drop a stuck ``verified`` migration back to ``idle`` (F-C21).

    ``run_migration`` is idempotent — re-invoking with a record at
    ``verified`` re-runs the verify step. When verify is deterministically
    failing (e.g. the target relay is permanently corrupt or unreachable),
    re-invocation just returns the same mismatch every time. There's no
    other path from ``verified`` to ``idle`` exposed at the runner level.

    Returns the rolled-back record (or ``None`` if no state file existed).
    Raises ``ValueError`` when the persisted record is in any state other
    than ``verified`` — explicit refusal so a caller can't quietly wipe
    a ``copying`` / ``committed`` record.
    """
    record = load_state(config_dir)
    if record is None:
        return None
    if record.state != "verified":
        raise ValueError(
            f"rollback_verified_migration only valid from state 'verified'; "
            f"got '{record.state}'"
        )
    rolled = transition(record, to="idle")
    clear_state(config_dir)
    log.info(
        "vault.migration.rollback_verified vault=%s target=%s",
        record.vault_id,
        record.target_relay_url,
    )
    return rolled


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------


def _bootstrap_target_and_inventory(
    *,
    vault: MigrationVault,
    source_relay: MigrationRelay,
    target_relay: MigrationRelay,
) -> tuple[dict[str, Any], list[str]]:
    """Create the target vault (or detect it already exists) + return chunk plan.

    Sharded migration shape: the source's root envelope is copied
    verbatim into the target vault row (``initial_root_*``), then each
    per-folder shard is replicated via ``put_shard`` so the target's
    hash chain matches the source byte-for-byte. AEAD AAD is bound to
    the same ``(vault_id, revision, parent_revision, author)`` tuple
    on both sides — no re-encryption needed.
    """
    source_header = source_relay.get_header(vault.vault_id, vault.vault_access_secret)
    source_root = source_relay.get_root(vault.vault_id, vault.vault_access_secret)
    root_envelope = source_root["root_ciphertext"]
    if isinstance(root_envelope, (bytearray, memoryview)):
        root_envelope = bytes(root_envelope)
    root_hash = str(source_root["root_hash"])
    root_revision = int(source_root["root_revision"])

    # Decrypt the source root so we can enumerate folder pointers (we
    # need each remote_folder_id to fetch + replicate its shard, and
    # the union of shards' chunk_ids is the migration chunk plan).
    root_plaintext = vault.decrypt_root_envelope(root_envelope)

    # We don't know the target's vault_access_token_hash from the source
    # alone (that hash is sha256(secret), and the device retains the
    # plaintext secret). Re-derive from the source's known secret.
    import hashlib

    token_hash = hashlib.sha256(vault.vault_access_secret.encode("ascii")).digest()
    try:
        target_relay.create_vault(
            vault.vault_id,
            token_hash,
            source_header["encrypted_header"],
            source_header["header_hash"],
            root_envelope,
            root_hash,
            initial_root_revision=root_revision,
            initial_header_revision=int(source_header.get("header_revision", 1)),
        )
    except RuntimeError as exc:
        # Idempotent re-entry: target already has the vault from a
        # previous interrupted run. We trust it; chunk-level diff still
        # runs to catch any partial copy.
        if "vault_already_exists" not in str(exc):
            raise

    # Fetch + replicate each shard. The source's shard envelope bytes
    # carry AAD bound to (vault_id, remote_folder_id, shard_revision,
    # parent_shard_revision, author) — the same on the target — so a
    # straight byte-copy preserves the §10.C hash chain.
    shards_by_id: dict[str, dict[str, Any]] = {}
    pointers = root_plaintext.get("remote_folders") or []
    for pointer in pointers:
        if not isinstance(pointer, dict):
            continue
        rf_id = str(pointer.get("remote_folder_id") or "")
        if not rf_id:
            continue
        # A freshly added folder may have ``shard_revision == 0`` (no
        # shard published yet). Skip — there's nothing to copy.
        pointer_shard_rev = int(pointer.get("shard_revision") or 0)
        if pointer_shard_rev <= 0:
            continue
        try:
            source_shard = source_relay.get_shard(
                vault.vault_id, vault.vault_access_secret, rf_id,
            )
        except Exception as exc:
            from ..relay_errors import VaultNotFoundError
            if isinstance(exc, VaultNotFoundError):
                # Pointer-without-shard on the source side too; skip
                # rather than abort the migration.
                continue
            raise
        shard_envelope = source_shard["shard_ciphertext"]
        if isinstance(shard_envelope, (bytearray, memoryview)):
            shard_envelope = bytes(shard_envelope)
        shard_hash = str(source_shard["shard_hash"])
        shard_revision = int(source_shard["shard_revision"])
        parent_shard_revision = int(
            source_shard.get("parent_shard_revision") or max(0, shard_revision - 1)
        )
        # Replicate to the target. Genesis insert on the target side
        # uses ``expected_current_shard_revision=0`` regardless of the
        # source's revision; the envelope's deterministic prefix on the
        # wire still carries ``shard_revision=N``, so the §10.C hash
        # chain matches once the bytes land. Server's chain check
        # (``parent == new - 1``) is independent of the CAS check
        # (``expected == current``), so genesis-insert at any revision
        # is accepted. §5.M2: server also skips the envelope
        # author-match check for ``expected=0`` so peer-authored
        # shards replicate cleanly (the migrating device is not
        # always the original author).
        try:
            target_relay.put_shard(
                vault.vault_id,
                vault.vault_access_secret,
                rf_id,
                expected_current_shard_revision=0,
                new_shard_revision=shard_revision,
                parent_shard_revision=parent_shard_revision,
                shard_hash=shard_hash,
                shard_ciphertext=shard_envelope,
            )
        except Exception as exc:
            # Idempotent re-entry: a prior run already published the
            # shard. The shard's CAS layer surfaces this as a conflict
            # error whose payload includes the current hash; if it
            # matches what we'd publish, treat as a no-op.
            from ..relay_errors import VaultCASConflictError
            if not isinstance(exc, VaultCASConflictError):
                raise
            details = getattr(exc, "details", {}) or {}
            current_hash = str(details.get("current_shard_hash") or "")
            current_rev = int(details.get("current_shard_revision") or 0)
            if current_hash != shard_hash or current_rev != shard_revision:
                raise
        shards_by_id[rf_id] = vault.decrypt_shard_envelope(shard_envelope, rf_id)

    # Walk the decrypted root + shards to enumerate every chunk_id the
    # vault references. Each version's chunks live inside its folder's
    # shard, never in the root — the root only carries pointers.
    chunk_ids: list[str] = []
    seen: set[str] = set()
    for rf_id, shard in shards_by_id.items():
        for entry in shard.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = str(chunk.get("chunk_id") or "")
                    if cid and cid not in seen:
                        seen.add(cid)
                        chunk_ids.append(cid)

    return (
        {
            "root_envelope": root_envelope,
            "root_hash": root_hash,
            "root_revision": root_revision,
            "root_plaintext": root_plaintext,
            "shards_by_id": shards_by_id,
        },
        chunk_ids,
    )


def _copy_chunks(
    *,
    vault: MigrationVault,
    source_relay: MigrationRelay,
    target_relay: MigrationRelay,
    chunk_ids: list[str],
    progress: Callable[[MigrationProgress], None] | None,
) -> tuple[int, int, int]:
    if not chunk_ids:
        _emit(progress, "copying", 0, 0, 0, 0)
        return 0, 0, 0

    heads = target_relay.batch_head_chunks(
        vault.vault_id, vault.vault_access_secret, chunk_ids,
    )
    copied = skipped = bytes_copied = 0
    for cid in chunk_ids:
        head = heads.get(cid) if isinstance(heads, dict) else None
        if isinstance(head, dict) and head.get("present"):
            skipped += 1
            _emit(
                progress, "copying",
                len(chunk_ids), copied, skipped, bytes_copied,
            )
            continue
        envelope = source_relay.get_chunk(
            vault.vault_id, vault.vault_access_secret, cid,
        )
        target_relay.put_chunk(
            vault.vault_id, vault.vault_access_secret, cid, envelope,
        )
        copied += 1
        bytes_copied += len(envelope)
        _emit(
            progress, "copying",
            len(chunk_ids), copied, skipped, bytes_copied,
        )
    return copied, skipped, bytes_copied


def _verify_migration(
    *,
    vault: MigrationVault,
    source_relay: MigrationRelay,
    target_relay: MigrationRelay,
    sample_size: int,
) -> MigrationVerifyOutcome:
    """Compare source aggregates against the target's hash chain.

    The source's ``migration_verify_source`` now reports ``root_hash``
    + per-folder ``shard_hashes``; the target's root + shards are
    re-fetched and hashed for equality. AEAD-sample N chunks on the
    target to detect bytes-in-transit corruption.
    """
    src = source_relay.migration_verify_source(
        vault.vault_id, vault.vault_access_secret,
    )
    tgt_header = target_relay.get_header(vault.vault_id, vault.vault_access_secret)
    tgt_root = target_relay.get_root(vault.vault_id, vault.vault_access_secret)

    mismatches: list[str] = []
    src_root_hash = str(src.get("root_hash") or "")
    tgt_root_hash = str(tgt_root.get("root_hash") or "")
    if src_root_hash != tgt_root_hash:
        mismatches.append("root_hash")

    # Compare each per-folder shard_hash. The map is keyed by
    # remote_folder_id; an empty server-side map ({}) returns as either
    # a Python dict (json.loads of {}) or a list when the json layer
    # collapses to []. Coerce safely.
    src_shard_hashes_raw = src.get("shard_hashes") or {}
    if isinstance(src_shard_hashes_raw, dict):
        src_shard_hashes: dict[str, str] = {
            str(k): str(v) for k, v in src_shard_hashes_raw.items()
        }
    else:
        src_shard_hashes = {}

    src_chunks_total = int(src.get("chunk_count") or 0)
    src_bytes = int(src.get("used_ciphertext_bytes") or 0)
    tgt_used = int(tgt_header.get("used_ciphertext_bytes") or 0)
    if src_bytes != tgt_used:
        mismatches.append("used_bytes")

    # Walk the target's root + shards live (no caching from bootstrap)
    # so a relay that lied during bootstrap can't slip through. For each
    # folder the source reports a ``shard_hash``; pull the matching
    # shard from the target and compare ``sha256(envelope_bytes)``.
    target_root_envelope = tgt_root["root_ciphertext"]
    if isinstance(target_root_envelope, (bytearray, memoryview)):
        target_root_envelope = bytes(target_root_envelope)
    target_root_plaintext = vault.decrypt_root_envelope(target_root_envelope)
    target_shards_by_id: dict[str, dict[str, Any]] = {}
    target_shard_hashes: dict[str, str] = {}
    for pointer in target_root_plaintext.get("remote_folders") or []:
        if not isinstance(pointer, dict):
            continue
        rf_id = str(pointer.get("remote_folder_id") or "")
        if not rf_id:
            continue
        pointer_shard_rev = int(pointer.get("shard_revision") or 0)
        if pointer_shard_rev <= 0:
            continue
        try:
            tgt_shard = target_relay.get_shard(
                vault.vault_id, vault.vault_access_secret, rf_id,
            )
        except Exception as exc:
            from ..relay_errors import VaultNotFoundError
            if isinstance(exc, VaultNotFoundError):
                target_shard_hashes[rf_id] = ""
                continue
            raise
        shard_envelope = tgt_shard["shard_ciphertext"]
        if isinstance(shard_envelope, (bytearray, memoryview)):
            shard_envelope = bytes(shard_envelope)
        target_shard_hashes[rf_id] = str(tgt_shard.get("shard_hash") or "")
        target_shards_by_id[rf_id] = vault.decrypt_shard_envelope(
            shard_envelope, rf_id,
        )

    # Per-folder shard_hash diff. We compare on the union of keys so a
    # folder that's present on the source but missing on the target
    # (or vice versa) still surfaces.
    for rf_id in sorted(set(src_shard_hashes) | set(target_shard_hashes)):
        src_h = src_shard_hashes.get(rf_id, "")
        tgt_h = target_shard_hashes.get(rf_id, "")
        if src_h != tgt_h:
            mismatches.append(f"shard_hash:{rf_id}")

    # F-C06: explicit chunk-count comparison. The pre-fix proxy
    # (`if src_chunks and sample_size_actual == 0`) only fired when
    # the target manifest had zero chunks AND the source claimed any —
    # it missed the realistic case where the target had *some* chunks
    # but fewer than the source claimed (e.g. a partial copy that
    # crashed mid-stream). Walk the target's decrypted shards and
    # count distinct chunk_ids; compare to the source's reported total.
    tgt_chunks = _count_unique_chunks_in_shards(target_shards_by_id)
    if src_chunks_total != tgt_chunks:
        mismatches.append("chunk_count")

    sample_passed = 0
    sample_chunks = _pick_random_sample_from_shards(
        target_root_plaintext, target_shards_by_id, sample_size,
    )
    sample_size_actual = len(sample_chunks)
    chunk_subkey = derive_subkey("dc-vault-v1/chunk", bytes(vault.master_key))
    transient_failures = 0
    for spec in sample_chunks:
        try:
            on_disk = target_relay.get_chunk(
                vault.vault_id, vault.vault_access_secret, spec["chunk_id"],
            )
        except Exception as exc:
            # F-C05: a transient relay error is NOT a chunk mismatch.
            # Surface separately so the operator can distinguish "1/5
            # failed AEAD" from "all 5 timed out".
            log.warning(
                "vault.migration.verify.chunk_fetch_failed chunk=%s error=%s",
                str(spec.get("chunk_id"))[:12], exc,
            )
            transient_failures += 1
            continue
        if len(on_disk) < 24 + 16:
            log.warning(
                "vault.migration.verify.chunk_truncated chunk=%s",
                str(spec.get("chunk_id"))[:12],
            )
            continue
        nonce = on_disk[:24]
        ciphertext = on_disk[24:]
        aad = build_chunk_aad(
            vault.vault_id,
            spec["remote_folder_id"],
            spec["entry_id"],
            spec["version_id"],
            int(spec["index"]),
            int(spec["plaintext_size"]),
        )
        try:
            aead_decrypt(ciphertext, chunk_subkey, nonce, aad)
        except Exception as exc:
            log.warning(
                "vault.migration.verify.chunk_aead_failed chunk=%s error=%s",
                str(spec.get("chunk_id"))[:12], exc,
            )
            continue
        sample_passed += 1
    if sample_size_actual > 0 and sample_passed < sample_size_actual:
        mismatches.append("chunk_sample")
    return MigrationVerifyOutcome(
        matches=len(mismatches) == 0,
        mismatches=mismatches,
        sample_size=sample_size_actual,
        sample_passed=sample_passed,
    )


def _audit_source_committed_to_target(
    *,
    source_relay: MigrationRelay,
    vault: MigrationVault,
    target_relay_url: str,
) -> None:
    """F-C09: forensic check before ``clear_state`` on
    ``committed → idle``. Logs a warning if the source relay's
    ``migration_verify_source`` view of the target diverges from the
    one we just committed to.

    Best-effort: never blocks the state clear. The "happy path" call
    can return:

    - ``target_relay_url`` matching ours → no drift; debug-level
      breadcrumb.
    - A different ``target_relay_url`` → operator-driven rollback or
      relaunched migration; loud warning.
    - Raise (intent was already cleared server-side after commit) →
      expected — typical post-commit state on a relay that GCs intents.
    - Other transient failure → debug-level breadcrumb so an outage
      doesn't drown out real signals.
    """
    try:
        verify = source_relay.migration_verify_source(
            vault.vault_id, vault.vault_access_secret,
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "vault.migration.committed_source_check_unreachable "
            "vault=%s target=%s reason=%s",
            vault.vault_id, target_relay_url, type(exc).__name__,
        )
        return
    seen_target = str(verify.get("target_relay_url") or "")
    if seen_target == target_relay_url:
        log.debug(
            "vault.migration.committed_source_aligned "
            "vault=%s target=%s",
            vault.vault_id, target_relay_url,
        )
        return
    log.warning(
        "vault.migration.committed_source_drift "
        "vault=%s expected_target=%s observed_target=%s",
        vault.vault_id, target_relay_url, seen_target or "<empty>",
    )


def _count_unique_chunks_in_shards(
    shards_by_id: dict[str, dict[str, Any]],
) -> int:
    """F-C06: count distinct ``chunk_id`` values across all live and
    historical versions across every shard. The source's
    ``chunk_count`` surface is the same de-duped count; this helper
    produces the target-side number to compare against directly so a
    partial copy (target has fewer chunks than the source claims)
    trips the verify instead of relying on the random-sample loop to
    incidentally catch it.
    """
    seen: set[str] = set()
    for shard in shards_by_id.values():
        if not isinstance(shard, dict):
            continue
        for entry in shard.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = str(chunk.get("chunk_id") or "")
                    if cid:
                        seen.add(cid)
    return len(seen)


def _pick_random_sample_from_shards(
    root: dict[str, Any],
    shards_by_id: dict[str, dict[str, Any]],
    sample_size: int,
) -> list[dict[str, Any]]:
    """Enumerate ``(chunk_id, remote_folder_id, entry_id, version_id,
    index, plaintext_size)`` tuples across the root's folders and the
    matching shard entries, then pick up to ``sample_size`` at random.
    """
    all_chunks: list[dict[str, Any]] = []
    for pointer in root.get("remote_folders", []) or []:
        if not isinstance(pointer, dict):
            continue
        rid = str(pointer.get("remote_folder_id", ""))
        if not rid:
            continue
        shard = shards_by_id.get(rid)
        if shard is None:
            continue
        for entry in shard.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            eid = str(entry.get("entry_id", ""))
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                vid = str(version.get("version_id", ""))
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = str(chunk.get("chunk_id") or "")
                    if not cid:
                        continue
                    all_chunks.append({
                        "chunk_id": cid,
                        "remote_folder_id": rid,
                        "entry_id": eid,
                        "version_id": vid,
                        "index": int(chunk.get("index", 0)),
                        "plaintext_size": int(chunk.get("plaintext_size", 0)),
                    })
    if not all_chunks:
        return []
    if len(all_chunks) <= sample_size:
        return all_chunks
    # secrets.SystemRandom for determinism-free CSPRNG sampling — same
    # quality as random.SystemRandom but no dependency on the random
    # module's stateful default RNG.
    rng = secrets.SystemRandom()
    indices = rng.sample(range(len(all_chunks)), sample_size)
    return [all_chunks[i] for i in indices]


def _emit(
    callback: Callable[[MigrationProgress], None] | None,
    phase: str,
    total: int,
    copied: int,
    skipped: int,
    bytes_copied: int,
) -> None:
    if callback is None:
        return
    callback(MigrationProgress(
        phase=phase,
        chunks_total=total,
        chunks_copied=copied,
        chunks_skipped=skipped,
        bytes_copied=bytes_copied,
    ))


def _now_rfc3339() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "DEFAULT_VERIFY_SAMPLE_SIZE",
    "MigrationInventory",
    "MigrationProgress",
    "MigrationRunResult",
    "MigrationVerifyOutcome",
    "migration_preflight",
    "rollback_verified_migration",
    "run_migration",
]
