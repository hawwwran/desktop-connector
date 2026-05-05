"""Vault browser soft-delete + restore (T7.1, T7.2, T7.4).

Each operation is one CAS-published manifest revision. The chunks
themselves are never touched here — soft delete keeps history available
until eviction (T7.5) or retention (T7.6) reclaims it.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .vault_browser_model import decrypt_manifest as decrypt_manifest_envelope
from .vault_manifest import (
    generate_file_version_id,
    normalize_manifest_path,
    normalize_manifest_plaintext,
    restore_file_entry,
    tombstone_file_entry,
    tombstone_files_under,
)
from .vault_relay_errors import VaultCASConflictError


CAS_MAX_RETRIES = 5


class DeleteVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_manifest(self, relay, *, local_index=None) -> dict: ...

    def publish_manifest(self, relay, manifest, *, local_index=None) -> dict: ...


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
    """Soft-delete one file (T7.1). Returns the published manifest plaintext."""
    deleted_at_str = str(deleted_at or _now_rfc3339())

    def op(parent: dict[str, Any]) -> dict[str, Any]:
        candidate = _bump_revision(parent, author_device_id=author_device_id)
        return tombstone_file_entry(
            candidate,
            remote_folder_id=remote_folder_id,
            path=remote_path,
            deleted_at=deleted_at_str,
            author_device_id=author_device_id,
        )

    return _publish_with_retry(
        vault=vault,
        relay=relay,
        parent_manifest=manifest,
        op=op,
        local_index=local_index,
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
    UI can show how many files were affected. The paths are computed
    from the *initial* parent manifest (the one passed in); concurrent
    edits that re-add files under the same prefix may add or skip
    entries during CAS retry, but those don't show up here.
    """
    deleted_at_str = str(deleted_at or _now_rfc3339())
    captured: list[list[str]] = []

    def op(parent: dict[str, Any]) -> dict[str, Any]:
        candidate = _bump_revision(parent, author_device_id=author_device_id)
        mutated, tombstoned = tombstone_files_under(
            candidate,
            remote_folder_id=remote_folder_id,
            path_prefix=path_prefix,
            deleted_at=deleted_at_str,
            author_device_id=author_device_id,
        )
        captured.append(tombstoned)
        return mutated

    published = _publish_with_retry(
        vault=vault,
        relay=relay,
        parent_manifest=manifest,
        op=op,
        local_index=local_index,
    )
    # F-D08: return the tombstone list from the *winning* CAS attempt
    # so the UI count reflects what actually landed (including any
    # peer-added files that were tombstoned on retry). The first
    # attempt's list undercounts whenever a CAS retry happens.
    last_tombstoned = captured[-1] if captured else []
    return published, last_tombstoned


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
    detection, just a manifest mutation. Tombstoned files restore via
    the same path: the new version clears the deleted flag.
    """
    timestamp = str(created_at or _now_rfc3339())

    def op(parent: dict[str, Any]) -> dict[str, Any]:
        candidate = _bump_revision(parent, author_device_id=author_device_id)
        normalized_path = normalize_manifest_path(remote_path)
        # Find the source version inside the candidate (which is built
        # on top of the freshly-fetched parent on retry).
        source = _find_version_for_restore(
            candidate, remote_folder_id, normalized_path, source_version_id,
        )
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
        return restore_file_entry(
            candidate,
            remote_folder_id=remote_folder_id,
            path=normalized_path,
            new_version=new_version,
            author_device_id=author_device_id,
        )

    return _publish_with_retry(
        vault=vault,
        relay=relay,
        parent_manifest=manifest,
        op=op,
        local_index=local_index,
    )


def _publish_with_retry(
    *,
    vault: DeleteVault,
    relay: Any,
    parent_manifest: dict[str, Any],
    op: Callable[[dict[str, Any]], dict[str, Any]],
    local_index: Any,
    max_retries: int = CAS_MAX_RETRIES,
) -> dict[str, Any]:
    """Publish ``op(parent)`` with §D4-style retry.

    Unlike the upload helper which carries a structured set of
    additions, deletes and restores are easy to re-derive on top of
    any server head: just call ``op`` again with the new parent. The
    op is idempotent under repeated application as long as the
    file/version is still locatable.
    """
    candidate = op(normalize_manifest_plaintext(parent_manifest))
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


def _bump_revision(parent: dict[str, Any], *, author_device_id: str) -> dict[str, Any]:
    parent_n = normalize_manifest_plaintext(parent)
    parent_revision = int(parent_n.get("revision", 0))
    out = dict(parent_n)
    out["revision"] = parent_revision + 1
    out["parent_revision"] = parent_revision
    out["created_at"] = _now_rfc3339()
    out["author_device_id"] = str(author_device_id)
    return out


def _find_version_for_restore(
    manifest: dict[str, Any],
    remote_folder_id: str,
    path: str,
    version_id: str,
) -> dict[str, Any] | None:
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        if folder.get("remote_folder_id") != remote_folder_id:
            continue
        for entry in folder.get("entries", []) or []:
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
