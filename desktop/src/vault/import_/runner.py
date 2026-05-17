"""Vault import orchestration (T8.5 backbone).

Reads a bundle, decrypts its manifest envelope, runs the §D9 merge
against the active vault state, uploads any chunks the relay doesn't
already have, and CAS-publishes the merged manifest. The GTK wizard
in :mod:`windows_vault_import` consumes this module — keeping the
orchestration testable without GTK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..binding.lifecycle import SyncCancelledError
from ..ui.browser_model import decrypt_manifest as decrypt_manifest_envelope
from ..crypto import normalize_vault_id
from ..export.bundle import (
    BundleContents,
    ExportError,
    read_export_bundle,
)
from .bundle import (
    ImportAction,
    ImportMergeResolution,
    ImportPreview,
    ImportMergeResult,
    decide_import_action,
    find_conflict_batches,
    merge_import_into,
    preview_import,
)
from ..relay_errors import VaultCASConflictError
from ..manifest import (
    assemble_unified_manifest,
    make_folder_shard,
    normalize_manifest_plaintext,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
)
from ..upload.folder_state import fetch_folder_state, FolderState


log = logging.getLogger(__name__)
CAS_MAX_RETRIES = 5


def _assert_bundle_vault_id_matches(
    *, bundle_vault_id: str, expected_vault_id: str
) -> None:
    """Defensive layering (F-C14) on top of the wrap-AAD vault_id binding.

    ``read_export_bundle`` already binds the AEAD-decryption key to the
    caller-supplied ``vault_id`` via the wrap AAD; if a future regression
    relaxed that binding, this check would catch a bundle whose internal
    header.vault_id disagrees with the active vault before any chunk
    upload or manifest decryption happens. Raises ``ExportError`` with
    the protocol's ``vault_export_vault_mismatch`` shape so the wizard
    can surface a clear refusal.
    """
    bundle_canonical = normalize_vault_id(bundle_vault_id)
    expected_canonical = normalize_vault_id(expected_vault_id)
    if bundle_canonical != expected_canonical:
        raise ExportError(
            "vault_export_vault_mismatch",
            f"bundle header vault_id {bundle_canonical} does not match "
            f"active vault {expected_canonical}",
        )


@dataclass(frozen=True)
class ImportRunProgress:
    phase: str  # "preview" | "uploading_chunks" | "publishing" | "done"
    chunks_uploaded: int = 0
    chunks_skipped: int = 0
    chunks_total: int = 0
    bytes_uploaded: int = 0


@dataclass(frozen=True)
class ImportRunResult:
    action: ImportAction
    preview: ImportPreview
    merge: ImportMergeResult | None
    chunks_uploaded: int
    chunks_skipped: int
    bytes_uploaded: int
    published_manifest: dict[str, Any] | None


class ImportVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    # Phase H step 7c: import reads + writes via sharded methods. The
    # ``manifest=`` kwarg accepted by ``run_import`` is the caller's
    # snapshot used for the §D9 merge math; the actual publish lands
    # per-folder on the sharded surface.
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

    def fetch_unified_manifest(self, relay, *, local_index=None) -> dict: ...


class ImportRelay(Protocol):
    def batch_head_chunks(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]: ...

    def put_chunk(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_id: str,
        body: bytes,
    ) -> dict[str, Any]: ...


def open_bundle_for_preview(
    *,
    vault: ImportVault,
    bundle_path: Path,
    passphrase: str,
    active_manifest: dict[str, Any] | None,
    active_genesis_fingerprint: str | None,
    bundle_genesis_fingerprint: str | None,
    chunks_already_on_relay: int,
    source_label: str | None = None,
) -> tuple[BundleContents, dict[str, Any], ImportPreview]:
    """Read+decrypt the bundle and build the preview.

    Uses ``vault.vault_id`` for the wrap AAD (single-vault-per-device
    invariant per §D9). Returns ``(bundle_contents,
    decrypted_bundle_manifest, preview)`` so the wizard can show the
    §17 fields and reuse the manifest for the merge step.
    """
    contents = read_export_bundle(
        bundle_path=bundle_path,
        passphrase=passphrase,
        vault_id=vault.vault_id,
    )
    _assert_bundle_vault_id_matches(
        bundle_vault_id=contents.header.vault_id,
        expected_vault_id=vault.vault_id,
    )
    bundle_manifest = decrypt_manifest_envelope(vault, contents.manifest_envelope)
    preview = preview_import(
        bundle_manifest=bundle_manifest,
        bundle_vault_id=contents.header.vault_id,
        active_manifest=active_manifest,
        source_label=source_label or f"File: {Path(bundle_path).name}",
        chunks_already_on_relay=chunks_already_on_relay,
        bundle_genesis_fingerprint=bundle_genesis_fingerprint,
        active_genesis_fingerprint=active_genesis_fingerprint,
    )
    return contents, bundle_manifest, preview


def run_import(
    *,
    vault: ImportVault,
    relay: ImportRelay,
    bundle_path: Path,
    passphrase: str,
    active_manifest: dict[str, Any],
    resolution: ImportMergeResolution,
    author_device_id: str,
    bundle_genesis_fingerprint: str | None = None,
    active_genesis_fingerprint: str | None = None,
    progress: Callable[[ImportRunProgress], None] | None = None,
    local_index: Any = None,
    should_continue: Callable[[], bool] | None = None,
) -> ImportRunResult:
    """Drive the import end to end (T8.5 backbone).

    F-U03: ``should_continue`` is checked before each chunk PUT and
    once before the CAS publish. Cancel-before-publish is safe (the
    merge isn't published until all chunks land); cancelled chunks
    that already landed on the relay stay there as orphans, cleaned up
    by the next eviction housekeeping pass per §D2.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    # Use the active vault's id for the wrap AAD — single-vault-per-device
    # invariant per §D9.
    contents = read_export_bundle(
        bundle_path=bundle_path,
        passphrase=passphrase,
        vault_id=vault.vault_id,
    )
    # F-C14: defensive layering on top of the wrap-AAD vault_id binding.
    _assert_bundle_vault_id_matches(
        bundle_vault_id=contents.header.vault_id,
        expected_vault_id=vault.vault_id,
    )

    # §D9 identity gate first — refuse exits before any chunk fetches
    # or manifest decryption attempts.
    action = decide_import_action(
        active_manifest=active_manifest,
        active_genesis_fingerprint=active_genesis_fingerprint,
        bundle_vault_id=contents.header.vault_id,
        bundle_genesis_fingerprint=bundle_genesis_fingerprint,
    )
    if action == "refuse":
        log.warning(
            "vault.import.refused vault_id=%s reason=identity-mismatch",
            contents.header.vault_id,
        )
        # Build a stub preview from the contents we have without going
        # into manifest decryption (which would fail anyway when the
        # bundle is for a different vault).
        stub_preview = preview_import(
            bundle_manifest={"remote_folders": []},
            bundle_vault_id=contents.header.vault_id,
            active_manifest=active_manifest,
            source_label=f"File: {Path(bundle_path).name}",
            chunks_already_on_relay=0,
            bundle_genesis_fingerprint=bundle_genesis_fingerprint,
            active_genesis_fingerprint=active_genesis_fingerprint,
        )
        return ImportRunResult(
            action=action, preview=stub_preview, merge=None,
            chunks_uploaded=0, chunks_skipped=0, bytes_uploaded=0,
            published_manifest=None,
        )

    bundle_manifest = decrypt_manifest_envelope(vault, contents.manifest_envelope)
    bundle_chunk_ids = sorted(contents.chunks.keys())

    chunks_already = relay.batch_head_chunks(
        vault.vault_id, vault.vault_access_secret, bundle_chunk_ids,
    )
    already_present = sum(
        1
        for cid in bundle_chunk_ids
        if isinstance(chunks_already.get(cid), dict) and chunks_already[cid].get("present")
    )

    preview = preview_import(
        bundle_manifest=bundle_manifest,
        bundle_vault_id=contents.header.vault_id,
        active_manifest=active_manifest,
        source_label=f"File: {Path(bundle_path).name}",
        chunks_already_on_relay=already_present,
        bundle_genesis_fingerprint=bundle_genesis_fingerprint,
        active_genesis_fingerprint=active_genesis_fingerprint,
    )

    # Upload missing chunks first (§D9: chunks land before manifest so a
    # CAS-published manifest never references absent chunks).
    chunks_uploaded = 0
    chunks_skipped = 0
    bytes_uploaded = 0
    _emit_progress(
        progress, "uploading_chunks", 0, already_present, len(bundle_chunk_ids), 0,
    )
    for cid in bundle_chunk_ids:
        if should_continue is not None and not should_continue():
            log.info(
                "vault.import.cancelled vault=%s chunks_done=%d total=%d",
                vault.vault_id,
                chunks_uploaded + chunks_skipped + already_present,
                len(bundle_chunk_ids),
            )
            raise SyncCancelledError(
                f"import cancelled at chunk "
                f"{chunks_uploaded + chunks_skipped + already_present}"
                f"/{len(bundle_chunk_ids)}"
            )
        head = chunks_already.get(cid) if isinstance(chunks_already, dict) else None
        if isinstance(head, dict) and head.get("present"):
            chunks_skipped += 1
            continue
        envelope = contents.chunks[cid]
        relay.put_chunk(
            vault.vault_id, vault.vault_access_secret, cid, envelope,
        )
        chunks_uploaded += 1
        bytes_uploaded += len(envelope)
        _emit_progress(
            progress, "uploading_chunks",
            chunks_uploaded, chunks_skipped + already_present,
            len(bundle_chunk_ids), bytes_uploaded,
        )

    if should_continue is not None and not should_continue():
        log.info(
            "vault.import.cancelled_pre_publish vault=%s chunks_done=%d",
            vault.vault_id, chunks_uploaded + chunks_skipped + already_present,
        )
        raise SyncCancelledError(
            "import cancelled before merge publish"
        )

    # Merge manifests + CAS-publish (T8.4 + T6.3 retry).
    merge = merge_import_into(
        active_manifest=active_manifest,
        bundle_manifest=bundle_manifest,
        resolution=resolution,
        author_device_id=author_device_id,
    )
    _emit_progress(
        progress, "publishing",
        chunks_uploaded, chunks_skipped + already_present,
        len(bundle_chunk_ids), bytes_uploaded,
    )
    published = _publish_merge_via_sharded(
        vault=vault,
        relay=relay,
        merged_manifest=merge.manifest,
        author_device_id=author_device_id,
        local_index=local_index,
    )
    _emit_progress(
        progress, "done",
        chunks_uploaded, chunks_skipped + already_present,
        len(bundle_chunk_ids), bytes_uploaded,
    )
    return ImportRunResult(
        action=action,
        preview=preview,
        merge=merge,
        chunks_uploaded=chunks_uploaded,
        chunks_skipped=chunks_skipped,
        bytes_uploaded=bytes_uploaded,
        published_manifest=published,
    )


def _publish_merge_via_sharded(
    *,
    vault: ImportVault,
    relay: Any,
    merged_manifest: dict[str, Any],
    author_device_id: str,
    local_index: Any,
    max_retries: int = CAS_MAX_RETRIES,
) -> dict[str, Any]:
    """Phase H step 7c: publish the §D9 merge result via per-folder
    ``publish_shard_with_root`` calls instead of one legacy
    ``publish_manifest``.

    Each folder in ``merged_manifest`` that has any entries lands as
    its own shard publish (one CAS unit). Folders the merge left
    empty are skipped. The merged folder-set is assumed to already
    exist in the active vault root — adding brand-new folder
    pointers is a separate ``vault.add_remote_folder`` flow (the
    bundle preview UI exposes it). Returns a synthesized unified
    manifest assembled from the post-publish sharded state.
    """
    merged_n = normalize_manifest_plaintext(merged_manifest)
    timestamp = str(merged_n.get("created_at") or "")

    # Iterate folders in the merge. For each, run a per-folder publish
    # with CAS retry — gather entries from the merge as the candidate
    # shard contents.
    for folder in merged_n.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        if not folder_id:
            continue
        merge_entries = list(folder.get("entries", []) or [])
        if not merge_entries:
            continue
        _publish_folder_merge_with_retry(
            vault=vault,
            relay=relay,
            remote_folder_id=folder_id,
            merge_entries=merge_entries,
            author_device_id=author_device_id,
            timestamp=timestamp,
            max_retries=max_retries,
        )

    return vault.fetch_unified_manifest(relay, local_index=local_index)


def _publish_folder_merge_with_retry(
    *,
    vault: ImportVault,
    relay: Any,
    remote_folder_id: str,
    merge_entries: list[dict[str, Any]],
    author_device_id: str,
    timestamp: str,
    max_retries: int,
) -> FolderState:
    """Publish one folder's merged-shard via ``publish_shard_with_root``.

    On CAS conflict, fetches the server head and re-applies the merge
    entries (overlay semantics: entries in ``merge_entries`` replace
    any same-path entry on the server; entries only on the server
    are preserved). This matches the unified-manifest merge_into
    behaviour scoped to one folder.
    """
    state = fetch_folder_state(vault, relay, remote_folder_id, author_device_id)
    for attempt in range(max_retries):
        candidate_shard, candidate_root = _build_candidate(
            state, remote_folder_id, merge_entries, author_device_id, timestamp,
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
                    "vault.import.cas_exhausted vault=%s folder=%s attempts=%d",
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
                "vault.import.cas_retry attempt=%d/%d folder=%s "
                "shard_conflict=%s root_conflict=%s",
                attempt + 1, max_retries, remote_folder_id,
                bool(shard_envelope), bool(root_envelope),
            )
            state = FolderState(root=new_root, shard=new_shard)
    raise AssertionError("unreachable: loop exits via return or raise")


def _build_candidate(
    state: FolderState,
    remote_folder_id: str,
    merge_entries: list[dict[str, Any]],
    author_device_id: str,
    timestamp: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Overlay the merge entries onto the current shard. Same-path
    entries from the merge replace the server's entry; server-only
    entries (unrelated paths) are preserved."""
    parent_n = normalize_shard_plaintext(state.shard)
    parent_revision = int(parent_n.get("shard_revision", 0))
    merge_paths = {
        str(e.get("path", "")) for e in merge_entries if isinstance(e, dict)
    }
    kept_entries = [
        e for e in parent_n.get("entries", []) or []
        if isinstance(e, dict) and str(e.get("path", "")) not in merge_paths
    ]
    out = dict(parent_n)
    out["entries"] = kept_entries + [
        e for e in merge_entries if isinstance(e, dict)
    ]
    out["shard_revision"] = parent_revision + 1
    out["parent_shard_revision"] = parent_revision
    out["created_at"] = timestamp or _now_rfc3339_default()
    out["author_device_id"] = str(author_device_id)
    out["remote_folder_id"] = remote_folder_id

    root_n = normalize_root_manifest_plaintext(state.root)
    parent_root_revision = int(root_n.get("root_revision", 0))
    candidate_root = dict(root_n)
    candidate_root["root_revision"] = parent_root_revision + 1
    candidate_root["parent_root_revision"] = parent_root_revision
    candidate_root["created_at"] = out["created_at"]
    candidate_root["author_device_id"] = str(author_device_id)
    return out, candidate_root


def _now_rfc3339_default() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _emit_progress(
    callback: Callable[[ImportRunProgress], None] | None,
    phase: str,
    chunks_uploaded: int,
    chunks_skipped: int,
    chunks_total: int,
    bytes_uploaded: int,
) -> None:
    if callback is None:
        return
    callback(ImportRunProgress(
        phase=phase,
        chunks_uploaded=chunks_uploaded,
        chunks_skipped=chunks_skipped,
        chunks_total=chunks_total,
        bytes_uploaded=bytes_uploaded,
    ))


__all__ = [
    "ImportRunProgress",
    "ImportRunResult",
    "open_bundle_for_preview",
    "run_import",
]
