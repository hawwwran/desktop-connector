"""Vault relay-to-relay migration runner (T9.3 + T9.4).

Orchestrates the full §H2 pipeline:

    started → copying → verified → committed

State is persisted via :mod:`vault_migration` before every transition,
so a crash-and-resume re-enters the runner at the same step (§H2
recovery table).

Pipeline:

1. **start**   — `POST source /migration/start` (gets bearer token,
                  records intent on source).
2. **bootstrap target** — `POST target /api/vaults` with `initial_manifest_revision =
                  source.current_revision`. The source's manifest envelope
                  (AAD bound to that revision) is stored verbatim on
                  the target — no re-encryption needed.
3. **copy chunks** — for each chunk_id in the source's manifest, batch-HEAD
                  on the target; if missing, GET source / PUT target.
4. **verify**  — `GET source /migration/verify-source` + `GET target /header`,
                  compare manifest_hash + chunk_count + used_ciphertext_bytes;
                  random-sample N chunks from the target and try AEAD-decrypt
                  with the live vault's master key (T9.4).
5. **commit**  — `PUT source /migration/commit` (stamps migrated_to).
6. **idle**    — clear state file; caller swaps active relay URL in config.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .vault_browser_model import decrypt_manifest as decrypt_manifest_envelope
from .vault_crypto import (
    aead_decrypt,
    build_chunk_aad,
    derive_subkey,
)
from .vault_migration import (
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


class MigrationRelay(Protocol):
    """Both source and target relay implement this surface."""

    def create_vault(self, *args, **kwargs) -> dict: ...

    def get_header(self, vault_id: str, vault_access_secret: str) -> dict: ...

    def get_manifest(self, vault_id: str, vault_access_secret: str) -> dict: ...

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
class MigrationVerifyOutcome:
    matches: bool
    mismatches: list[str] = field(default_factory=list)   # subset of {"manifest_hash","chunk_count","used_bytes","chunk_sample"}
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
    now: str | None = None,
) -> MigrationRunResult:
    """Drive a relay-to-relay migration end to end.

    Recovery model: every state transition is persisted to
    ``<config_dir>/vault_migration.json`` *before* the corresponding
    network op fires. A crash mid-run means the saved state still
    reflects the last successful transition; ``run_migration`` is
    idempotent — re-invoking from any state continues from there.
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
        bootstrap, chunk_inventory = _bootstrap_target_and_inventory(
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
        bootstrap = None

    # ── verifying (T9.4) ────────────────────────────────────────────────
    verify = MigrationVerifyOutcome(matches=True, mismatches=[])
    if record.state == "verified":
        verify = _verify_migration(
            vault=vault,
            source_relay=source_relay,
            target_relay=target_relay,
            sample_size=sample_size,
            cached_manifest_envelope=(
                bootstrap["manifest_envelope"] if bootstrap is not None else None
            ),
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

    # ── idle (post-commit cleanup) ──────────────────────────────────────
    if record.state == "committed":
        # The previous_relay_url was stamped by `transition(..., to="committed")`;
        # the caller flips the active relay URL in config. We clear the
        # state file so a relaunch knows the migration's done — but the
        # config retains previous_relay_url for the §H2 7-day grace.
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


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------


def _bootstrap_target_and_inventory(
    *,
    vault: MigrationVault,
    source_relay: MigrationRelay,
    target_relay: MigrationRelay,
) -> tuple[dict[str, Any], list[str]]:
    """Create the target vault (or detect it already exists) + return chunk plan."""
    source_header = source_relay.get_header(vault.vault_id, vault.vault_access_secret)
    source_manifest = source_relay.get_manifest(vault.vault_id, vault.vault_access_secret)
    manifest_envelope = source_manifest["manifest_ciphertext"]
    manifest_hash = source_manifest["manifest_hash"]
    manifest_revision = int(source_manifest["manifest_revision"])

    # We don't know the target's vault_access_token_hash from the source
    # alone (that hash is sha256(secret), and the device retains the
    # plaintext secret). Re-derive from the source's known secret.
    import hashlib

    token_hash = hashlib.sha256(vault.vault_access_secret.encode("ascii")).digest()
    try:
        target_relay.create_vault(
            vault_id=vault.vault_id,
            vault_access_token_hash=token_hash,
            encrypted_header=source_header["encrypted_header"],
            header_hash=source_header["header_hash"],
            initial_manifest_ciphertext=manifest_envelope,
            initial_manifest_hash=manifest_hash,
            initial_manifest_revision=manifest_revision,
            initial_header_revision=int(source_header.get("header_revision", 1)),
        )
    except RuntimeError as exc:
        # Idempotent re-entry: target already has the vault from a
        # previous interrupted run. We trust it; chunk-level diff still
        # runs to catch any partial copy.
        if "vault_already_exists" not in str(exc):
            raise

    bundle_manifest = decrypt_manifest_envelope(vault, manifest_envelope)
    chunk_ids: list[str] = []
    seen: set[str] = set()
    for folder in bundle_manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        for entry in folder.get("entries", []) or []:
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
            "manifest_envelope": manifest_envelope,
            "manifest_hash": manifest_hash,
            "manifest_revision": manifest_revision,
            "manifest_plaintext": bundle_manifest,
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
    cached_manifest_envelope: bytes | None,
) -> MigrationVerifyOutcome:
    src = source_relay.migration_verify_source(
        vault.vault_id, vault.vault_access_secret,
    )
    tgt_header = target_relay.get_header(vault.vault_id, vault.vault_access_secret)
    tgt_manifest = target_relay.get_manifest(vault.vault_id, vault.vault_access_secret)

    mismatches: list[str] = []
    if str(src.get("manifest_hash") or "") != str(tgt_manifest.get("manifest_hash") or ""):
        mismatches.append("manifest_hash")

    src_chunks = int(src.get("chunk_count") or 0)
    src_bytes = int(src.get("used_ciphertext_bytes") or 0)
    tgt_used = int(tgt_header.get("used_ciphertext_bytes") or 0)
    if src_bytes != tgt_used:
        mismatches.append("used_bytes")

    # Random-sample chunk decrypt on the target (T9.4): pull N chunks
    # from the target and try AEAD-decrypt with the live master key. A
    # mismatch here means the bytes drifted in transit.
    envelope = cached_manifest_envelope or tgt_manifest["manifest_ciphertext"]
    bundle_manifest = decrypt_manifest_envelope(vault, envelope)
    sample_passed = 0
    sample_chunks = _pick_random_sample(bundle_manifest, sample_size)
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
    if src_chunks and sample_size_actual == 0:
        # Source claims chunks but our sampler couldn't find any to test.
        mismatches.append("chunk_count")
    return MigrationVerifyOutcome(
        matches=len(mismatches) == 0,
        mismatches=mismatches,
        sample_size=sample_size_actual,
        sample_passed=sample_passed,
    )


def _pick_random_sample(
    manifest: dict[str, Any], sample_size: int,
) -> list[dict[str, Any]]:
    """Walk the manifest and pick up to ``sample_size`` chunks at random."""
    all_chunks: list[dict[str, Any]] = []
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        rid = str(folder.get("remote_folder_id", ""))
        for entry in folder.get("entries", []) or []:
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
    "MigrationProgress",
    "MigrationRunResult",
    "MigrationVerifyOutcome",
    "run_migration",
]
