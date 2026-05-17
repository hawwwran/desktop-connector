"""Vault browser soft-delete + restore (T7.1, T7.2, T7.4).

Each operation is one CAS-published shard-with-root revision. Chunks
themselves are never touched here — soft delete keeps history available
until eviction (T7.5) or retention (T7.6) reclaims it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)
from ..manifest import (
    assemble_unified_manifest,
    generate_file_version_id,
    normalize_manifest_path,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
    restore_file_entry_in_shard,
    tombstone_file_entry_in_shard,
    tombstone_files_under_in_shard,
)
from ..relay_errors import VaultCASConflictError
from ..upload.folder_state import (
    FolderState,
    fetch_folder_state,
    find_root_folder_pointer,
)


CAS_MAX_RETRIES = 5


class DeleteVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    # Legacy fields kept for caller compat — Phase H step 5 ignores them.
    def fetch_manifest(self, relay, *, local_index=None) -> dict: ...

    def publish_manifest(self, relay, manifest, *, local_index=None) -> dict: ...

    # Sharded methods used by the ported delete / restore path.
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


def delete_file(
    *,
    vault: DeleteVault,
    relay: Any,
    manifest: dict[str, Any],
    remote_folder_id: str,
    remote_path: str,
    author_device_id: str,
    deleted_at: str | None = None,
    local_index: Any = None,
) -> dict[str, Any]:
    """Soft-delete one file (T7.1). Returns the published unified manifest plaintext.

    Phase H step 5: ``manifest`` kwarg is accepted for caller compat
    and ignored — state is fetched fresh from the sharded relay
    surface. Publishes via ``publish_shard_with_root``.
    """
    deleted_at_str = str(deleted_at or _now_rfc3339())

    def op(shard: dict[str, Any], retention: dict[str, int] | None) -> dict[str, Any]:
        return tombstone_file_entry_in_shard(
            shard,
            path=remote_path,
            deleted_at=deleted_at_str,
            author_device_id=author_device_id,
            folder_retention_policy=retention,
        )

    published_state = _publish_shard_with_retry(
        vault=vault,
        relay=relay,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        op=op,
    )
    # F-510: anchor the Activity tab "Deleted" timeline row.
    log.info(
        "vault.delete.completed vault=%s revision=%d remote_folder_id=%s path=%s",
        vault.vault_id,
        int(published_state.root.get("root_revision", 0)),
        remote_folder_id,
        remote_path,
    )
    return assemble_unified_manifest(
        published_state.root, {remote_folder_id: published_state.shard},
    )


def delete_folder_contents(
    *,
    vault: DeleteVault,
    relay: Any,
    manifest: dict[str, Any],
    remote_folder_id: str,
    path_prefix: str,
    author_device_id: str,
    deleted_at: str | None = None,
    local_index: Any = None,
) -> tuple[dict[str, Any], list[str]]:
    """Soft-delete every file at or under ``path_prefix`` in one revision (T7.2).

    Returns ``(published_manifest, paths_tombstoned_locally)`` so the
    UI can show how many files were affected.
    """
    deleted_at_str = str(deleted_at or _now_rfc3339())
    captured: list[list[str]] = []

    def op(shard: dict[str, Any], retention: dict[str, int] | None) -> dict[str, Any]:
        mutated, tombstoned = tombstone_files_under_in_shard(
            shard,
            path_prefix=path_prefix,
            deleted_at=deleted_at_str,
            author_device_id=author_device_id,
            folder_retention_policy=retention,
        )
        captured.append(tombstoned)
        return mutated

    published_state = _publish_shard_with_retry(
        vault=vault,
        relay=relay,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        op=op,
    )
    # F-D08: return the tombstone list from the *winning* CAS attempt
    # so the UI count reflects what actually landed.
    last_tombstoned = captured[-1] if captured else []
    log.info(
        "vault.delete.completed vault=%s revision=%d remote_folder_id=%s "
        "path_prefix=%s tombstoned=%d",
        vault.vault_id,
        int(published_state.root.get("root_revision", 0)),
        remote_folder_id,
        path_prefix,
        len(last_tombstoned),
    )
    return (
        assemble_unified_manifest(
            published_state.root, {remote_folder_id: published_state.shard},
        ),
        last_tombstoned,
    )


def restore_version_to_current(
    *,
    vault: DeleteVault,
    relay: Any,
    manifest: dict[str, Any],
    remote_folder_id: str,
    remote_path: str,
    source_version_id: str,
    author_device_id: str,
    created_at: str | None = None,
    local_index: Any = None,
) -> dict[str, Any]:
    """T7.4: Promote ``source_version_id`` to the current version.

    Builds a fresh version_id whose chunks list copies the source's
    chunk references — no new chunk uploads, no fingerprint change
    detection, just a shard mutation. Tombstoned files restore via
    the same path: the new version clears the deleted flag.
    """
    timestamp = str(created_at or _now_rfc3339())

    def op(shard: dict[str, Any], retention: dict[str, int] | None) -> dict[str, Any]:
        normalized_path = normalize_manifest_path(remote_path)
        source = _find_version_for_restore(shard, normalized_path, source_version_id)
        if source is None:
            raise KeyError(
                f"version not found for restore: {source_version_id} "
                f"at {remote_folder_id}/{normalized_path}"
            )
        new_version = {
            "version_id": generate_file_version_id(),
            "created_at": timestamp,
            "modified_at": timestamp,
            "logical_size": int(source.get("logical_size", 0)),
            "ciphertext_size": int(source.get("ciphertext_size", 0)),
            "content_fingerprint": str(source.get("content_fingerprint", "")),
            "author_device_id": str(author_device_id),
            "chunks": [
                {
                    "chunk_id": str(c.get("chunk_id", "")),
                    "index": int(c.get("index", 0)),
                    "plaintext_size": int(c.get("plaintext_size", 0)),
                    "ciphertext_size": int(c.get("ciphertext_size", 0)),
                }
                for c in source.get("chunks", []) or []
                if isinstance(c, dict)
            ],
            "restored_from_version_id": str(source.get("version_id", "")),
        }
        return restore_file_entry_in_shard(
            shard,
            path=normalized_path,
            new_version=new_version,
            author_device_id=author_device_id,
        )

    published_state = _publish_shard_with_retry(
        vault=vault,
        relay=relay,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        op=op,
    )
    log.info(
        "vault.restore.completed vault=%s revision=%d remote_folder_id=%s "
        "path=%s source_version_id=%s",
        vault.vault_id,
        int(published_state.root.get("root_revision", 0)),
        remote_folder_id,
        remote_path,
        source_version_id[:12],
    )
    return assemble_unified_manifest(
        published_state.root, {remote_folder_id: published_state.shard},
    )


def _publish_shard_with_retry(
    *,
    vault: DeleteVault,
    relay: Any,
    remote_folder_id: str,
    author_device_id: str,
    op: Callable[[dict[str, Any], dict[str, int] | None], dict[str, Any]],
    max_retries: int = CAS_MAX_RETRIES,
) -> FolderState:
    """Publish ``op(shard, folder_retention_policy)`` with §D4 CAS retry.

    Deletes and restores are easy to re-derive on top of any server
    head: just call ``op`` again with the new shard. The op is
    idempotent under repeated application as long as the
    file/version is still locatable.
    """
    current_state = fetch_folder_state(vault, relay, remote_folder_id, author_device_id)
    for attempt in range(max_retries):
        candidate_shard, candidate_root = _build_candidate(
            current_state, remote_folder_id, author_device_id, op,
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
                    "vault.delete.cas_exhausted vault=%s attempts=%d",
                    getattr(vault, "vault_id", "?"),
                    max_retries,
                )
                raise
            new_shard = (
                vault.decrypt_shard_envelope(shard_envelope, remote_folder_id)
                if shard_envelope else current_state.shard
            )
            new_root = (
                vault.decrypt_root_envelope(root_envelope)
                if root_envelope else current_state.root
            )
            log.info(
                "vault.delete.cas_retry attempt=%d/%d folder=%s "
                "shard_conflict=%s root_conflict=%s",
                attempt + 1, max_retries, remote_folder_id,
                bool(shard_envelope), bool(root_envelope),
            )
            current_state = FolderState(root=new_root, shard=new_shard)
    raise AssertionError("unreachable: loop exits via return or raise")


def _build_candidate(
    state: FolderState,
    remote_folder_id: str,
    author_device_id: str,
    op: Callable[[dict[str, Any], dict[str, int] | None], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer = find_root_folder_pointer(state.root, remote_folder_id)
    folder_retention = (
        dict(pointer["retention_policy"])
        if pointer is not None and isinstance(pointer.get("retention_policy"), dict)
        else None
    )
    created_at = _now_rfc3339()
    parent_n = normalize_shard_plaintext(state.shard)
    parent_revision = int(parent_n.get("shard_revision", 0))
    mutated = op(parent_n, folder_retention)
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


def _find_version_for_restore(
    shard: dict[str, Any],
    path: str,
    version_id: str,
) -> dict[str, Any] | None:
    for entry in shard.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        entry_path = str(entry.get("path", ""))
        if normalize_manifest_path(entry_path) != path:
            continue
        for version in entry.get("versions", []) or []:
            if isinstance(version, dict) and \
               str(version.get("version_id", "")) == version_id:
                return version
    return None


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "delete_file",
    "delete_folder_contents",
    "restore_version_to_current",
]
