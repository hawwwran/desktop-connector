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
from ..state.op_log import append_op_log_entries, build_op_log_entry
from ..upload.folder_state import (
    FolderState,
    fetch_folder_state,
    find_root_folder_pointer,
)


CAS_MAX_RETRIES = 5


# Signature: given the candidate's new shard revision (parent + 1),
# return the list of op-log entries to append to the shard's tail.
# Called once per CAS attempt — after the op closure runs, so any
# captured-per-attempt state (e.g., the list of paths tombstoned by
# ``delete_folder_contents``) is fresh.
OpLogEntriesBuilder = Callable[[int], list[dict[str, Any]]]


class DeleteVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_root_manifest(self, relay, *, local_index=None) -> dict: ...

    def fetch_folder_shard(
        self, relay, remote_folder_id: str, *,
        expected_shard_hash: str | None = None,
    ) -> dict: ...

    def publish_root_manifest(
        self, relay, root: dict, *, local_index=None,
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

    def op_log_entries(new_revision: int) -> list[dict[str, Any]]:
        return [build_op_log_entry(
            type="vault.delete.completed",
            device_id=author_device_id,
            revision=new_revision,
            path=normalize_manifest_path(remote_path),
        )]

    published_state = _publish_shard_with_retry(
        vault=vault,
        relay=relay,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        op=op,
        op_log_entries=op_log_entries,
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
    summary_op_log_event: str | None = None,
    summary_op_log_path: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Soft-delete every file at or under ``path_prefix`` in one revision (T7.2).

    Returns ``(published_manifest, paths_tombstoned_locally)`` so the
    UI can show how many files were affected.

    ``summary_op_log_event`` lets a caller (e.g., ``clear_folder``)
    request an extra summary entry on the same shard revision —
    typically ``"vault.folder.cleared"`` — alongside the per-file
    ``vault.delete.completed`` entries. ``summary_op_log_path`` is
    forwarded into that entry's ``path`` field (use the folder's
    display name or the path prefix for a UI label).
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

    def op_log_entries(new_revision: int) -> list[dict[str, Any]]:
        last = captured[-1] if captured else []
        entries = [
            build_op_log_entry(
                type="vault.delete.completed",
                device_id=author_device_id,
                revision=new_revision,
                path=p,
            )
            for p in last
        ]
        if summary_op_log_event and last:
            # Lands on the same shard revision as the per-file
            # delete entries — Activity tab shows the user's intent
            # ("Folder cleared: Docs") alongside the per-file
            # audit trail.
            entries.append(build_op_log_entry(
                type=summary_op_log_event,
                device_id=author_device_id,
                revision=new_revision,
                path=summary_op_log_path,
            ))
        return entries

    published_state = _publish_shard_with_retry(
        vault=vault,
        relay=relay,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        op=op,
        op_log_entries=op_log_entries,
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

    def op_log_entries(new_revision: int) -> list[dict[str, Any]]:
        return [build_op_log_entry(
            type="vault.restore.completed",
            device_id=author_device_id,
            revision=new_revision,
            path=normalize_manifest_path(remote_path),
            extra={"source_version_id": str(source_version_id)},
        )]

    published_state = _publish_shard_with_retry(
        vault=vault,
        relay=relay,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        op=op,
        op_log_entries=op_log_entries,
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


def restore_folder_contents(
    *,
    vault: DeleteVault,
    relay: Any,
    manifest: dict[str, Any],
    remote_folder_id: str,
    path_prefix: str,
    author_device_id: str,
    created_at: str | None = None,
    local_index: Any = None,
) -> tuple[dict[str, Any], list[str]]:
    """Bulk-restore every tombstoned file at or under ``path_prefix``.

    Symmetric to :func:`delete_folder_contents`: one sharded publish
    flips every tombstoned entry under ``path_prefix`` back to a live
    version by minting a fresh ``version_id`` that points at the
    chunks of the entry's last-known version. Mirrors what the user
    gets if they restore each file individually, but in a single CAS
    cycle so the manifest revision count stays sane on a folder full
    of deletions.

    Empty ``path_prefix`` means "everything in this shard" — used by
    the browser's per-folder "Restore" action when an entire remote
    folder's contents were cleared.

    Returns ``(published_unified_manifest, paths_restored)``; the
    second element reflects what landed in the winning CAS attempt.
    """
    import unicodedata  # local — keep the top-of-file import set focused

    timestamp = str(created_at or _now_rfc3339())
    captured: list[list[str]] = []

    def op(shard: dict[str, Any], retention: dict[str, int] | None) -> dict[str, Any]:
        prefix_norm = (
            normalize_manifest_path(path_prefix) if path_prefix else ""
        )
        targets: list[tuple[str, dict[str, Any]]] = []
        for entry in shard.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "file")) != "file":
                continue
            if not bool(entry.get("deleted")):
                continue
            entry_path = unicodedata.normalize(
                "NFC", str(entry.get("path", "")),
            )
            if prefix_norm and not (
                entry_path == prefix_norm
                or entry_path.startswith(prefix_norm + "/")
            ):
                continue
            # Restore from the entry's latest version (the same one
            # ``delete_file`` left a tombstone on top of). Fall back
            # to the newest version dict if ``latest_version_id``
            # isn't resolvable.
            latest_id = str(entry.get("latest_version_id", ""))
            source: dict[str, Any] | None = None
            for v in entry.get("versions", []) or []:
                if isinstance(v, dict) and str(v.get("version_id", "")) == latest_id:
                    source = v
                    break
            if source is None:
                versions_list = [
                    v for v in (entry.get("versions") or [])
                    if isinstance(v, dict)
                ]
                if versions_list:
                    source = versions_list[-1]
            if source is None:
                continue
            targets.append((entry_path, source))

        out = shard
        restored: list[str] = []
        for entry_path, source in targets:
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
            out = restore_file_entry_in_shard(
                out, path=entry_path, new_version=new_version,
                author_device_id=author_device_id,
            )
            restored.append(entry_path)
        captured.append(restored)
        return out

    def op_log_entries(new_revision: int) -> list[dict[str, Any]]:
        last = captured[-1] if captured else []
        return [
            build_op_log_entry(
                type="vault.restore.completed",
                device_id=author_device_id,
                revision=new_revision,
                path=p,
            )
            for p in last
        ]

    published_state = _publish_shard_with_retry(
        vault=vault, relay=relay,
        remote_folder_id=remote_folder_id,
        author_device_id=author_device_id,
        op=op,
        op_log_entries=op_log_entries,
    )
    last_restored = captured[-1] if captured else []
    # Collateral fix per docs/plans/activity-timeline.md: align the log
    # event with the ``vault.restore.completed`` label in
    # ``state/activity.py:_EVENT_TYPE_LABELS``. The pre-rename event
    # rendered as the raw machine string in the Activity tab.
    log.info(
        "vault.restore.completed vault=%s revision=%d "
        "remote_folder_id=%s path_prefix=%s restored=%d",
        vault.vault_id,
        int(published_state.root.get("root_revision", 0)),
        remote_folder_id,
        path_prefix,
        len(last_restored),
    )
    return (
        assemble_unified_manifest(
            published_state.root, {remote_folder_id: published_state.shard},
        ),
        last_restored,
    )


def _publish_shard_with_retry(
    *,
    vault: DeleteVault,
    relay: Any,
    remote_folder_id: str,
    author_device_id: str,
    op: Callable[[dict[str, Any], dict[str, int] | None], dict[str, Any]],
    op_log_entries: OpLogEntriesBuilder | None = None,
    max_retries: int = CAS_MAX_RETRIES,
) -> FolderState:
    """Publish ``op(shard, folder_retention_policy)`` with §D4 CAS retry.

    Deletes and restores are easy to re-derive on top of any server
    head: just call ``op`` again with the new shard. The op is
    idempotent under repeated application as long as the
    file/version is still locatable.

    ``op_log_entries`` (Phase 2 activity-timeline wiring) is invoked
    once per CAS attempt, after ``op``, so any captured-per-attempt
    state in the caller's closure (e.g., ``captured[-1]`` in
    ``delete_folder_contents``) reflects this attempt's mutation.
    """
    current_state = fetch_folder_state(vault, relay, remote_folder_id, author_device_id)
    for attempt in range(max_retries):
        candidate_shard, candidate_root = _build_candidate(
            current_state, remote_folder_id, author_device_id, op,
            op_log_entries=op_log_entries,
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
    *,
    op_log_entries: OpLogEntriesBuilder | None = None,
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
    next_revision = parent_revision + 1
    mutated = op(parent_n, folder_retention)
    mutated["shard_revision"] = next_revision
    mutated["parent_shard_revision"] = parent_revision
    mutated["created_at"] = created_at
    mutated["author_device_id"] = str(author_device_id)
    mutated["remote_folder_id"] = remote_folder_id
    if op_log_entries is not None:
        # D7: prior tail comes from the just-fetched state.shard so a
        # concurrent writer's entries (rolled in by a 409 refetch)
        # survive the candidate rebuild.
        mutated["operation_log_tail"] = append_op_log_entries(
            parent_n.get("operation_log_tail"),
            op_log_entries(next_revision),
        )

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
