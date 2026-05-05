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

from .vault_binding_lifecycle import SyncCancelledError
from .vault_browser_model import decrypt_manifest as decrypt_manifest_envelope
from .vault_export import (
    BundleContents,
    ExportError,
    read_export_bundle,
)
from .vault_import import (
    ImportAction,
    ImportMergeResolution,
    ImportPreview,
    ImportMergeResult,
    decide_import_action,
    find_conflict_batches,
    merge_import_into,
    preview_import,
)
from .vault_relay_errors import VaultCASConflictError
from .vault_manifest import merge_with_remote_head, normalize_manifest_plaintext


log = logging.getLogger(__name__)
CAS_MAX_RETRIES = 5


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

    def fetch_manifest(self, relay, *, local_index=None) -> dict: ...

    def publish_manifest(self, relay, manifest, *, local_index=None) -> dict: ...


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
    published = _publish_with_cas_retry(
        vault=vault,
        relay=relay,
        parent_manifest=active_manifest,
        candidate=merge.manifest,
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


def _publish_with_cas_retry(
    *,
    vault: ImportVault,
    relay: Any,
    parent_manifest: dict[str, Any],
    candidate: dict[str, Any],
    author_device_id: str,
    local_index: Any,
    max_retries: int = CAS_MAX_RETRIES,
) -> dict[str, Any]:
    rebased_parent = parent_manifest
    last_attempt = candidate
    for _ in range(max_retries):
        try:
            return vault.publish_manifest(relay, last_attempt, local_index=local_index)
        except VaultCASConflictError as exc:
            envelope = exc.current_manifest_ciphertext_bytes()
            if not envelope:
                raise
            server_head = decrypt_manifest_envelope(vault, envelope)
            last_attempt = merge_with_remote_head(
                parent=rebased_parent,
                local_attempt=last_attempt,
                server_head=server_head,
                author_device_id=author_device_id,
            )
            rebased_parent = server_head
    return vault.publish_manifest(relay, last_attempt, local_index=local_index)


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
