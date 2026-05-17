"""Recursive folder upload (T6.4).

Walks ``local_root`` once, classifying each file via the binding's
``ignore_patterns`` + the §gaps §7 size cap + the special-file gate.
All accepted files chunk + PUT individually; their version additions
collect into one batch which is CAS-published in a single manifest
revision so the folder upload is atomic from the manifest's POV.
"""

import logging
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from ..binding.lifecycle import SyncCancelledError
from ..crypto import (
    derive_content_fingerprint_key,
    make_content_fingerprint,
)
from ..manifest import (
    add_or_append_file_version_in_shard,
    assemble_unified_manifest,
    find_file_entry_in_shard,
    generate_file_entry_id,
    generate_file_version_id,
    merge_local_version_into_shard,
    normalize_manifest_path,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
)
from ..relay_errors import VaultCASConflictError
from .constants import CAS_MAX_RETRIES, CHUNK_SIZE, MAX_FILE_BYTES_DEFAULT
from .folder_state import (
    FolderState,
    fetch_folder_state,
    find_root_folder_pointer,
)
from .hashing import _hash_file, _now_rfc3339
from .ignore_patterns import _matches_ignore
from .protocols import UploadRelay, UploadVault
from .results import FileSkipped, FolderUploadProgress, FolderUploadResult, UploadResult
from .single_file import (
    _build_chunk_plan,
    _bumped_root_for_shard_publish,
    _make_version_payload,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FilePlan:
    local_path: Path
    relative_path: str
    size_bytes: int


@dataclass
class _VersionAddition:
    remote_folder_id: str
    path: str
    entry_id: str
    version: dict[str, Any]


def upload_folder(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    manifest: dict[str, Any],
    local_root: Path,
    remote_folder_id: str,
    remote_sub_path: str,
    author_device_id: str,
    chunk_size: int = CHUNK_SIZE,
    max_file_bytes: int = MAX_FILE_BYTES_DEFAULT,
    extra_ignore_patterns: list[str] | None = None,
    created_at: str | None = None,
    progress: Callable[[FolderUploadProgress], None] | None = None,
    local_index: Any = None,
    should_continue: Callable[[], bool] | None = None,
    parent_state: FolderState | None = None,
) -> FolderUploadResult:
    """Recursively upload every accepted file under ``local_root``.

    "Accepted" = not ignored by the remote folder's ``ignore_patterns``,
    not over the per-file size cap, and not a special file (symlink /
    FIFO / socket / device). Skipped files are logged with the §gaps §7
    event names so a user can find them later.

    All file additions land in **one** CAS-published manifest revision —
    a folder upload is atomic from the manifest's point of view. The CAS
    retry from T6.3 still kicks in when a concurrent device beat us to
    the publish.

    F-U03: ``should_continue`` is checked between every file in the
    plan and once before the CAS publish. Already-uploaded chunks
    stay on the relay (orphans cleaned up by the next eviction
    housekeeping pass per §D2); the manifest is NOT mutated when the
    bail fires before publish, so a cancelled folder upload leaves no
    half-applied state in the vault.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    local_root = Path(local_root)
    if not local_root.is_dir():
        raise NotADirectoryError(f"local folder not found: {local_root}")

    # Phase H step 4: fetch the binding's sharded state fresh; the
    # ``manifest`` kwarg is accepted for caller compatibility and
    # ignored. ``parent_state`` lets §D4 acceptance tests inject a
    # stale view so the CAS-retry merge path runs deterministically —
    # production callers don't pass it.
    state = parent_state if parent_state is not None else fetch_folder_state(
        vault, relay, remote_folder_id, author_device_id,
    )
    pointer = find_root_folder_pointer(state.root, remote_folder_id)
    if pointer is None:
        raise ValueError(f"remote folder not found: {remote_folder_id}")
    ignore_patterns = list(pointer.get("ignore_patterns", []) or [])
    ignore_patterns.extend(extra_ignore_patterns or [])

    base_remote_path = normalize_manifest_path(remote_sub_path) if remote_sub_path else ""

    # 1. Walk + classify.
    plans: list[_FilePlan] = []
    skipped: list[FileSkipped] = []
    files_total = 0
    bytes_total = 0
    for entry in _walk_for_upload(local_root, ignore_patterns, max_file_bytes):
        if isinstance(entry, FileSkipped):
            skipped.append(entry)
            event = {
                "ignored": "vault.sync.file_skipped_ignored",
                "too_large": "vault.sync.file_skipped_too_large",
                "special": "vault.sync.special_file_skipped",
                "error": "vault.sync.file_walk_error",
            }[entry.reason]
            if entry.reason == "error":
                # F-D13: errno surfaces "permission-denied" vs "dangling
                # symlink" vs other I/O classes; without it the user sees
                # a "skipped" line and can't tell why.
                log.warning(
                    "%s path=%s errno=%d", event, entry.relative_path, entry.errno,
                )
            else:
                log.info(
                    "%s path=%s size=%d", event, entry.relative_path, entry.size_bytes,
                )
            continue
        plans.append(entry)
        files_total += 1
        bytes_total += entry.size_bytes

    _report_folder(progress, "walking", files_total, 0, bytes_total, 0, "")

    # 2. Upload chunks for each file (the per-file fingerprint short-circuit
    # still applies — identical content lands as a no-op per file).
    additions: list[_VersionAddition] = []
    upload_results: list[UploadResult] = []
    bytes_completed = 0
    for plan in plans:
        if should_continue is not None and not should_continue():
            log.info(
                "vault.folder_upload.cancelled vault=%s files_done=%d total=%d",
                vault.vault_id, len(upload_results), files_total,
            )
            raise SyncCancelledError(
                f"folder upload cancelled at file {len(upload_results)}/{files_total}"
            )
        remote_path = (
            f"{base_remote_path}/{plan.relative_path}"
            if base_remote_path
            else plan.relative_path
        )
        result = _upload_one_into_batch(
            vault=vault,
            relay=relay,
            parent_state=state,
            local_path=plan.local_path,
            remote_folder_id=remote_folder_id,
            remote_path=remote_path,
            author_device_id=author_device_id,
            chunk_size=chunk_size,
            created_at=created_at,
            additions=additions,
        )
        upload_results.append(result)
        bytes_completed += plan.size_bytes
        _report_folder(
            progress,
            "uploading",
            files_total,
            len(upload_results),
            bytes_total,
            bytes_completed,
            plan.relative_path,
        )

    # 3. CAS-publish all version additions atomically. Identical-content
    # short-circuits leave nothing to publish — the shard is unchanged.
    if not any(addition for addition in additions):
        _report_folder(progress, "done", files_total, files_total, bytes_total, bytes_completed, "")
        return FolderUploadResult(
            manifest=assemble_unified_manifest(
                state.root, {remote_folder_id: state.shard},
            ),
            uploaded=upload_results,
            skipped=skipped,
        )

    if should_continue is not None and not should_continue():
        log.info(
            "vault.folder_upload.cancelled_pre_publish vault=%s files_done=%d",
            vault.vault_id, len(upload_results),
        )
        raise SyncCancelledError(
            f"folder upload cancelled before publish ({len(upload_results)} files staged)"
        )

    _report_folder(progress, "publishing", files_total, files_total, bytes_total, bytes_completed, "")
    published_state = _publish_batch_with_cas_retry(
        vault=vault,
        relay=relay,
        parent_state=state,
        remote_folder_id=remote_folder_id,
        additions=additions,
        author_device_id=author_device_id,
    )
    _report_folder(progress, "done", files_total, files_total, bytes_total, bytes_completed, "")

    # Re-stamp uploaded entries with the published manifest so the caller
    # sees consistent state in `result.uploaded[i].manifest`.
    published_manifest = assemble_unified_manifest(
        published_state.root, {remote_folder_id: published_state.shard},
    )
    upload_results = [
        UploadResult(
            manifest=published_manifest,
            entry_id=r.entry_id,
            version_id=r.version_id,
            path=r.path,
            remote_folder_id=r.remote_folder_id,
            chunks_uploaded=r.chunks_uploaded,
            chunks_skipped=r.chunks_skipped,
            bytes_uploaded=r.bytes_uploaded,
            logical_size=r.logical_size,
            content_fingerprint=r.content_fingerprint,
            skipped_identical=r.skipped_identical,
        )
        for r in upload_results
    ]
    return FolderUploadResult(
        manifest=published_manifest,
        uploaded=upload_results,
        skipped=skipped,
    )


def _walk_for_upload(
    root: Path,
    ignore_patterns: list[str],
    max_file_bytes: int,
) -> Iterator["_FilePlan | FileSkipped"]:
    """Yield ``_FilePlan`` for accepted files and ``FileSkipped`` otherwise.

    Walks alphabetically for deterministic ordering. Directory ignore
    matches prune the subtree (no recursion into ``node_modules/``,
    etc.).
    """
    root = Path(root)
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        current, current_rel = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            continue
        for child in children:
            child_rel = f"{current_rel}/{child.name}" if current_rel else child.name
            try:
                st = child.lstat()
            except OSError as exc:
                # F-D13: lstat-failed paths (permission denied, dangling
                # symlink, transient I/O) are an *error* class — distinct
                # from a successfully-stat'd special file.
                yield FileSkipped(
                    child_rel, "error", 0, errno=int(getattr(exc, "errno", 0) or 0),
                )
                continue
            mode = st.st_mode

            if stat.S_ISLNK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) \
               or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                yield FileSkipped(child_rel, "special", 0)
                continue

            if stat.S_ISDIR(mode):
                if _matches_ignore(child.name, child_rel, ignore_patterns, is_dir=True):
                    yield FileSkipped(child_rel + "/", "ignored", 0)
                    continue
                stack.append((child, child_rel))
                continue

            if not stat.S_ISREG(mode):
                yield FileSkipped(child_rel, "special", 0)
                continue

            if _matches_ignore(child.name, child_rel, ignore_patterns, is_dir=False):
                yield FileSkipped(child_rel, "ignored", int(st.st_size))
                continue

            if int(st.st_size) > int(max_file_bytes):
                yield FileSkipped(child_rel, "too_large", int(st.st_size))
                continue

            yield _FilePlan(
                local_path=child,
                relative_path=child_rel,
                size_bytes=int(st.st_size),
            )


def _upload_one_into_batch(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    parent_state: FolderState,
    local_path: Path,
    remote_folder_id: str,
    remote_path: str,
    author_device_id: str,
    chunk_size: int,
    created_at: str | None,
    additions: list[_VersionAddition],
) -> "UploadResult":
    """Per-file half of ``upload_folder``: chunk + PUT + collect the version
    payload into ``additions`` for the batched shard publish."""
    normalized_remote_path = normalize_manifest_path(remote_path)
    existing_entry = find_file_entry_in_shard(parent_state.shard, normalized_remote_path)
    has_existing = existing_entry is not None and not bool(existing_entry.get("deleted"))

    plaintext_sha256, total_logical_size = _hash_file(local_path)
    content_fp_key = derive_content_fingerprint_key(vault.master_key)
    fingerprint = make_content_fingerprint(content_fp_key, plaintext_sha256)

    if has_existing:
        for version in existing_entry.get("versions", []) or []:
            if not isinstance(version, dict):
                continue
            if str(version.get("content_fingerprint", "")) == fingerprint:
                return UploadResult(
                    manifest=assemble_unified_manifest(
                        parent_state.root, {remote_folder_id: parent_state.shard},
                    ),
                    entry_id=str(existing_entry["entry_id"]),
                    version_id=str(version.get("version_id", "")),
                    path=normalized_remote_path,
                    remote_folder_id=remote_folder_id,
                    chunks_uploaded=0,
                    chunks_skipped=int(len(version.get("chunks") or [])),
                    bytes_uploaded=0,
                    logical_size=total_logical_size,
                    content_fingerprint=fingerprint,
                    skipped_identical=True,
                )

    entry_id = (
        existing_entry["entry_id"] if existing_entry else generate_file_entry_id()
    )
    version_id = generate_file_version_id()

    chunks_plan, _ = _build_chunk_plan(
        vault=vault,
        local_path=local_path,
        remote_folder_id=remote_folder_id,
        entry_id=entry_id,
        version_id=version_id,
        chunk_size=chunk_size,
    )

    chunk_ids = [chunk["chunk_id"] for chunk in chunks_plan]
    heads = (
        relay.batch_head_chunks(vault.vault_id, vault.vault_access_secret, chunk_ids)
        if chunk_ids
        else {}
    )
    chunks_uploaded = 0
    chunks_skipped = 0
    bytes_uploaded = 0
    for chunk in chunks_plan:
        head = heads.get(chunk["chunk_id"]) if isinstance(heads, dict) else None
        if isinstance(head, dict) and head.get("present"):
            chunks_skipped += 1
            continue
        relay.put_chunk(
            vault.vault_id, vault.vault_access_secret, chunk["chunk_id"], chunk["envelope"],
        )
        chunks_uploaded += 1
        bytes_uploaded += int(chunk["ciphertext_size"])

    version_payload = _make_version_payload(
        version_id=version_id,
        chunks_plan=chunks_plan,
        author_device_id=author_device_id,
        created_at=created_at or _now_rfc3339(),
        logical_size=total_logical_size,
        content_fingerprint=fingerprint,
    )
    additions.append(_VersionAddition(
        remote_folder_id=remote_folder_id,
        path=normalized_remote_path,
        entry_id=entry_id,
        version=version_payload,
    ))

    return UploadResult(
        manifest=assemble_unified_manifest(
            parent_state.root, {remote_folder_id: parent_state.shard},
        ),  # patched after the batch publish
        entry_id=entry_id,
        version_id=version_id,
        path=normalized_remote_path,
        remote_folder_id=remote_folder_id,
        chunks_uploaded=chunks_uploaded,
        chunks_skipped=chunks_skipped,
        bytes_uploaded=bytes_uploaded,
        logical_size=total_logical_size,
        content_fingerprint=fingerprint,
        skipped_identical=False,
    )


def _publish_batch_with_cas_retry(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    parent_state: FolderState,
    remote_folder_id: str,
    additions: list[_VersionAddition],
    author_device_id: str,
    max_retries: int = CAS_MAX_RETRIES,
) -> FolderState:
    """Apply N version additions to ``parent_state.shard`` and CAS-publish
    via ``publish_shard_with_root``.

    First attempt uses blind path-append (``_apply_additions_to_shard``)
    — matches the pre-Phase-H semantic where a non-conflicting publish
    is a fresh upload, no tie-break needed. On the first 409 we flip
    ``use_merge = True`` and rebuild every retry candidate via
    ``_merge_additions_into_shard_with_bump`` so concurrent same-path
    uploads from different devices land with the §D4-correct
    ``entry_id`` (collision-rename) and ``latest_version_id``
    (tie-break by ``(modified_at, sha256(author_device_id))``).
    """
    timestamp = _now_rfc3339()
    initial_parent = parent_state
    current_state = parent_state
    use_merge = False
    for attempt in range(max_retries):
        if use_merge:
            candidate_shard = _merge_additions_into_shard_with_bump(
                server_shard=current_state.shard,
                parent_shard=initial_parent.shard,
                additions=additions,
                remote_folder_id=remote_folder_id,
                author_device_id=author_device_id,
                created_at=timestamp,
            )
        else:
            candidate_shard = _apply_additions_to_shard(
                current_state.shard, additions,
                remote_folder_id=remote_folder_id,
                author_device_id=author_device_id,
                created_at=timestamp,
            )
        candidate_root = _bumped_root_for_shard_publish(
            current_state.root,
            author_device_id=author_device_id,
            created_at=timestamp,
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
                    "vault.upload.batch_cas_exhausted vault=%s additions=%d attempts=%d",
                    getattr(vault, "vault_id", "?"),
                    len(additions),
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
                "vault.folder_upload.cas_retry attempt=%d/%d additions=%d "
                "shard_conflict=%s root_conflict=%s",
                attempt + 1, max_retries, len(additions),
                bool(shard_envelope), bool(root_envelope),
            )
            current_state = FolderState(root=new_root, shard=new_shard)
            use_merge = True
    raise AssertionError("unreachable: loop exits via return or raise")


def _apply_additions_to_shard(
    parent_shard: dict[str, Any],
    additions: list[_VersionAddition],
    *,
    remote_folder_id: str,
    author_device_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Fold N version additions onto ``parent_shard`` and bump revisions.

    Idempotent under CAS replay: ``add_or_append_file_version_in_shard``
    is a no-op when the same ``version_id`` already exists on the entry.
    """
    parent_n = normalize_shard_plaintext(parent_shard)
    parent_revision = int(parent_n.get("shard_revision", 0))
    candidate = parent_n
    for addition in additions:
        candidate = add_or_append_file_version_in_shard(
            candidate,
            path=addition.path,
            version=addition.version,
            entry_id=addition.entry_id,
        )
    candidate["shard_revision"] = parent_revision + 1
    candidate["parent_shard_revision"] = parent_revision
    candidate["created_at"] = created_at
    candidate["author_device_id"] = str(author_device_id)
    candidate["remote_folder_id"] = remote_folder_id
    return candidate


def _merge_additions_into_shard_with_bump(
    *,
    server_shard: dict[str, Any],
    parent_shard: dict[str, Any],
    additions: list[_VersionAddition],
    remote_folder_id: str,
    author_device_id: str,
    created_at: str,
) -> dict[str, Any]:
    """CAS-retry candidate for the folder-batch path: rebuild N version
    additions on top of ``server_shard`` with §D4 tie-break +
    collision-rename per addition.

    Iterates ``merge_local_version_into_shard`` once per addition. Each
    call sees the entries accumulated by previous iterations, so a
    same-path collision among in-batch additions correctly chains
    renames. ``parent_shard`` is the pre-attempt local snapshot used by
    the merge for version-id deduplication.

    Bumps the shard revision pair once at the end so the result is
    directly publishable via ``publish_shard_with_root``.
    """
    server_n = normalize_shard_plaintext(server_shard)
    parent_revision = int(server_n.get("shard_revision", 0))
    merged = server_n
    for addition in additions:
        merged = merge_local_version_into_shard(
            merged,
            parent_shard=parent_shard,
            entry_id=addition.entry_id,
            path=addition.path,
            version=addition.version,
        )
    merged["shard_revision"] = parent_revision + 1
    merged["parent_shard_revision"] = parent_revision
    merged["created_at"] = created_at
    merged["author_device_id"] = str(author_device_id)
    merged["remote_folder_id"] = remote_folder_id
    return merged


def _report_folder(
    callback: Callable[[FolderUploadProgress], None] | None,
    phase: str,
    files_total: int,
    files_completed: int,
    bytes_total: int,
    bytes_completed: int,
    current_path: str,
) -> None:
    if callback is None:
        return
    callback(FolderUploadProgress(
        phase=phase,  # type: ignore[arg-type]
        files_total=files_total,
        files_completed=files_completed,
        bytes_total=bytes_total,
        bytes_completed=bytes_completed,
        current_path=current_path,
    ))
