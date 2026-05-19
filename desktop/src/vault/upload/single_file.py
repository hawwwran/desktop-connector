"""Single-file upload (T6.1+).

Owns the per-file orchestration:
  1. lstat-classify (reject specials, enforce per-file size cap)
  2. SHA-256 + content-fingerprint short-circuit identical re-upload
  3. plan-and-encrypt chunks (kept here because folder.py shares
     ``_build_chunk_plan`` and ``_make_version_payload``)
  4. batch-HEAD then PUT missing chunks (relay dedup)
  5. CAS-publish a new manifest revision via ``_publish_with_cas_retry``
     (also shared with resume.py)
"""

import logging
import secrets
import stat as _stat
from pathlib import Path
from typing import Any, Callable

from ..binding.lifecycle import SyncCancelledError
from ..crypto import (
    aead_encrypt,
    build_chunk_aad,
    build_chunk_envelope,
    derive_chunk_id_key,
    derive_chunk_nonce_key,
    derive_content_fingerprint_key,
    derive_subkey,
    make_chunk_id,
    make_chunk_nonce,
    make_content_fingerprint,
)
from ..manifest import (
    add_or_append_file_version_in_shard,
    find_file_entry_in_shard,
    generate_file_entry_id,
    generate_file_version_id,
    merge_local_version_into_shard,
    normalize_manifest_path,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
)
from ..relay_errors import VaultCASConflictError
from ..state.op_log import append_op_log_entries, build_op_log_entry
from .constants import CAS_MAX_RETRIES, CHUNK_SIZE, MAX_FILE_BYTES_DEFAULT, UploadMode
from .folder_state import (
    FolderState,
    fetch_folder_state,
    find_root_folder_pointer,
)
from .errors import UploadConflictError, UploadFileTooLargeError, UploadSpecialFileSkipped
from .hashing import _hash_file, _now_rfc3339
from .protocols import UploadRelay, UploadVault
from .batch_session import (
    BatchedUploadStub,
    find_matching_stub,
    make_stub,
    reap_stubs_for_path,
    save_stub,
)
from .results import PreparedUpload, UploadProgress, UploadResult
from .session import UploadSession, default_upload_resume_dir, clear_session, save_session

log = logging.getLogger(__name__)


def _validate_local_for_upload(local_path: Path) -> int:
    """Run the lstat-classify + size-cap checks both upload paths share.

    Returns the observed file size on success. Raises the same
    typed exceptions ``upload_file`` and ``prepare_upload_for_batch``
    would have raised inline; both callers re-raise upward without
    translation.

    Extracted so a future security fix (a new special-file rejection,
    a tightened size cap, etc.) lands in one place instead of drifting
    across two near-duplicates.
    """
    # F-Y17: lstat first to reject symlinks / FIFOs / sockets / device
    # files. ``is_file()`` follows symlinks, which would silently
    # upload the symlink target's contents as if they belonged to the
    # binding (e.g. a symlink pointing at /etc/passwd).
    try:
        st = local_path.lstat()
    except OSError as exc:
        raise FileNotFoundError(f"local file not found: {local_path}") from exc
    mode_bits = st.st_mode
    if (
        _stat.S_ISLNK(mode_bits)
        or _stat.S_ISFIFO(mode_bits)
        or _stat.S_ISSOCK(mode_bits)
        or _stat.S_ISCHR(mode_bits)
        or _stat.S_ISBLK(mode_bits)
    ):
        log.info(
            "vault.sync.special_file_skipped path=%s reason=non-regular",
            local_path,
        )
        raise UploadSpecialFileSkipped(
            f"refusing to upload non-regular file: {local_path}"
        )
    if not local_path.is_file():
        raise FileNotFoundError(f"local file not found: {local_path}")

    # F-D01: enforce the §gaps §7 per-file size cap on every upload
    # path, not just the folder walker. Without this, a sync-engine
    # caller renaming a 4 GiB file into the binding root would
    # pre-encrypt the entire file into RAM (~2× peak) before the cap
    # had any chance to fire.
    try:
        observed_size = int(local_path.stat().st_size)
    except OSError as exc:
        raise FileNotFoundError(f"local file not found: {local_path}") from exc
    if observed_size > MAX_FILE_BYTES_DEFAULT:
        raise UploadFileTooLargeError(
            f"{local_path} is {observed_size} bytes, "
            f"max per-file is {MAX_FILE_BYTES_DEFAULT}"
        )
    return observed_size


def upload_file(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    manifest: dict[str, Any],
    local_path: Path,
    remote_folder_id: str,
    remote_path: str,
    author_device_id: str,
    mode: UploadMode = "new_file_or_version",
    chunk_size: int = CHUNK_SIZE,
    created_at: str | None = None,
    progress: Callable[[UploadProgress], None] | None = None,
    local_index: Any = None,
    resume_cache_dir: Path | None = None,
    should_continue: Callable[[], bool] | None = None,
    parent_state: FolderState | None = None,
) -> UploadResult:
    """Encrypt + upload ``local_path`` and CAS-publish a new manifest revision.

    The function does not retry on ``VaultCASConflictError`` — that's
    T6.3's job. On any error after the manifest mutation step, partial
    state is bounded to chunks already PUT to the relay; nothing local
    is touched.

    F-Y08: ``should_continue`` is consulted between every chunk PUT
    and once before the CAS publish. When it returns ``False`` the
    function raises :class:`vault_binding_lifecycle.SyncCancelledError`;
    the upload session is already saved per chunk so the next
    :func:`resume_upload` picks up exactly where the bail happened
    (relay-side dedup means re-PUTting a completed chunk is a 200 OK
    no-op). Pass ``None`` (the default) for "always continue".
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    local_path = Path(local_path)
    _validate_local_for_upload(local_path)

    # Phase H step 4: read the binding's folder shard + vault root
    # fresh; the ``manifest`` kwarg is accepted for caller compatibility
    # and ignored. The shard is the authoritative source for the
    # existing-entry / fingerprint short-circuit; the root pointer
    # carries the folder retention policy used by tombstone publishes.
    # ``parent_state`` lets §D4 acceptance tests inject a stale view so
    # the CAS-retry merge path runs deterministically — production
    # callers don't pass it.
    state = parent_state if parent_state is not None else fetch_folder_state(
        vault, relay, remote_folder_id, author_device_id,
    )

    normalized_remote_path = normalize_manifest_path(remote_path)
    existing_entry = find_file_entry_in_shard(state.shard, normalized_remote_path)
    has_existing = existing_entry is not None and not bool(existing_entry.get("deleted"))

    if mode == "new_file_only" and has_existing:
        raise UploadConflictError(
            f"refusing to overwrite existing remote path: {normalized_remote_path}"
        )
    if mode == "append_version_only" and not has_existing:
        raise UploadConflictError(
            f"no existing remote entry to append a version to: {normalized_remote_path}"
        )

    # Compute the file's keyed content fingerprint up-front so we can
    # short-circuit re-uploads of identical bytes (T6.1 acceptance:
    # "uploading the same file twice the second time uploads zero new
    # chunks"). Hashing is cheap (SHA-256 on disk), saves the entire
    # encrypt + PUT path when the answer is "no change".
    plaintext_sha256, total_logical_size = _hash_file(local_path)
    content_fp_key = derive_content_fingerprint_key(vault.master_key)
    fingerprint = make_content_fingerprint(content_fp_key, plaintext_sha256)

    if has_existing:
        for version in existing_entry.get("versions", []) or []:
            if not isinstance(version, dict):
                continue
            if str(version.get("content_fingerprint", "")) == fingerprint and \
               not bool(existing_entry.get("deleted")):
                _report(progress, "done", 0, 0, 0)
                return UploadResult(
                    root=state.root,
                    shard=state.shard,
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

    # Plan + encrypt chunks. We hold the whole encrypted set in memory
    # for v1 (single-file path; folder upload streams in T6.4).
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
    total_chunks = len(chunks_plan)

    timestamp = created_at or _now_rfc3339()
    cache_dir = resume_cache_dir or default_upload_resume_dir()
    session = UploadSession(
        session_id=secrets.token_hex(8),
        vault_id=vault.vault_id,
        remote_folder_id=remote_folder_id,
        remote_path=normalized_remote_path,
        entry_id=entry_id,
        version_id=version_id,
        author_device_id=str(author_device_id),
        content_fingerprint=fingerprint,
        logical_size=total_logical_size,
        local_path=str(local_path.resolve()),
        chunk_size=chunk_size,
        created_at=timestamp,
        chunks=[
            {
                "chunk_id": c["chunk_id"],
                "index": c["index"],
                "plaintext_size": c["plaintext_size"],
                "ciphertext_size": c["ciphertext_size"],
                "done": False,
            }
            for c in chunks_plan
        ],
        phase="uploading",
    )
    save_session(session, cache_dir)

    # batch-HEAD to learn which chunks the relay already has.
    chunk_ids = [chunk["chunk_id"] for chunk in chunks_plan]
    _report(progress, "checking", 0, total_chunks)
    heads = (
        relay.batch_head_chunks(vault.vault_id, vault.vault_access_secret, chunk_ids)
        if chunk_ids
        else {}
    )

    # PUT missing chunks (idempotent; same chunk_id from same content).
    chunks_uploaded = 0
    chunks_skipped = 0
    bytes_uploaded = 0
    completed = 0
    for index, chunk in enumerate(chunks_plan):
        # F-Y08: bail before each chunk so a Pause / Disconnect lands
        # within ~1 chunk worth of work. The session is already saved
        # for every prior chunk, so a future resume is a HEAD-and-skip.
        if should_continue is not None and not should_continue():
            log.info(
                "vault.sync.upload_cancelled vault=%s remote_path=%s chunks_done=%d total=%d",
                vault.vault_id, normalized_remote_path, completed, total_chunks,
            )
            raise SyncCancelledError(
                f"upload cancelled at chunk {completed}/{total_chunks} of "
                f"{normalized_remote_path}"
            )
        head = heads.get(chunk["chunk_id"]) if isinstance(heads, dict) else None
        if isinstance(head, dict) and head.get("present"):
            chunks_skipped += 1
        else:
            relay.put_chunk(
                vault.vault_id,
                vault.vault_access_secret,
                chunk["chunk_id"],
                chunk["envelope"],
            )
            chunks_uploaded += 1
            bytes_uploaded += chunk["ciphertext_size"]
        session.chunks[index]["done"] = True
        save_session(session, cache_dir)
        completed += 1
        _report(progress, "uploading", completed, total_chunks, bytes_uploaded)

    # F-Y08: one more checkpoint before publishing — if pause lands
    # right after the last PUT, we'd rather defer the manifest mutation
    # to the next cycle than hold the CAS slot.
    if should_continue is not None and not should_continue():
        log.info(
            "vault.sync.upload_cancelled_pre_publish vault=%s remote_path=%s",
            vault.vault_id, normalized_remote_path,
        )
        raise SyncCancelledError(
            f"upload cancelled before publish for {normalized_remote_path}"
        )

    # Build the version payload + mutate the manifest.
    version_payload = _make_version_payload(
        version_id=version_id,
        chunks_plan=chunks_plan,
        author_device_id=author_device_id,
        created_at=timestamp,
        logical_size=total_logical_size,
        content_fingerprint=fingerprint,
    )

    session.phase = "ready_to_publish"
    save_session(session, cache_dir)

    published_state = _publish_with_cas_retry(
        vault=vault,
        relay=relay,
        parent_state=state,
        remote_folder_id=remote_folder_id,
        normalized_remote_path=normalized_remote_path,
        version_payload=version_payload,
        entry_id=entry_id,
        author_device_id=author_device_id,
    )
    # Review §4.H1: unlink first so a crash after this point leaves no
    # session JSON on disk. Pre-fix the ordering was
    # save(phase=complete) → unlink — a crash between the two leaked
    # the JSON forever (``list_resumable_sessions`` filtered it out
    # correctly, but nothing reaped it). If the unlink itself errors
    # (rare disk failure), fall back to the old marker behaviour so
    # ``list_resumable_sessions`` still skips it; the TTL reaper
    # (``reap_expired_sessions``) sweeps both lingerers after 14 days.
    try:
        clear_session(session.session_id, cache_dir)
    except OSError:
        log.warning(
            "vault.upload.session_clear_failed session=%s",
            session.session_id,
        )
        session.phase = "complete"
        try:
            save_session(session, cache_dir)
        except OSError:
            log.exception(
                "vault.upload.session_tombstone_failed session=%s",
                session.session_id,
            )
    _report(progress, "done", total_chunks, total_chunks, bytes_uploaded)

    # F-510: anchor the Activity tab's "Uploaded" timeline row.
    log.info(
        "vault.upload.completed vault=%s revision=%d path=%s",
        vault.vault_id,
        int(published_state.root.get("root_revision", 0)),
        normalized_remote_path,
    )

    return UploadResult(
        root=published_state.root,
        shard=published_state.shard,
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


def prepare_upload_for_batch(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    shard: dict[str, Any],
    local_path: Path,
    remote_folder_id: str,
    remote_path: str,
    author_device_id: str,
    chunk_size: int = CHUNK_SIZE,
    created_at: str | None = None,
    progress: Callable[[UploadProgress], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
    batch_cache_dir: Path | None = None,
) -> PreparedUpload:
    """SO-3: encrypt + PUT chunks for ``local_path`` without publishing
    a manifest revision. Returns the version payload the caller will
    fold into a batched CAS publish.

    Crash safety: chunk PUTs are idempotent (server dedupes by
    content-addressed ``chunk_id``); re-running prep after a kill
    HEAD-and-skips chunks that already landed *provided the same
    ``version_id`` is reused* — chunk_ids depend on version_id, so a
    fresh random id between runs would invalidate dedupe. We pin
    that by persisting a :class:`BatchedUploadStub` (lightweight, no
    per-chunk state) keyed by ``(vault, remote_path, fingerprint)``;
    the next prep finds it and reuses the same ``entry_id`` /
    ``version_id``. Stubs live under ``<batch_cache_dir>/batched/`` so
    they don't surface in the resume banner (which only scans the
    parent ``cache_dir``). The cycle clears them after a successful
    batch publish.

    Skipped-identical short circuit: when the file's keyed fingerprint
    already matches the latest live version in ``shard``, returns
    ``PreparedUpload(skipped_identical=True, ...)`` without touching
    the relay. The caller stamps the local-entry row and drops the
    pending-op without growing the batch.

    Review §3.M4 — *read-at-prep, not at-enqueue.* The watcher coalesces
    every event on a single path into one pending-op via
    ``coalesce_op``; the actual ``_hash_file`` + ``_build_chunk_plan``
    reads happen here, at prep time, after the stability gate (3 s
    local / 10 s network) has settled the file. The bytes uploaded
    therefore reflect the file's state at batch-fire time, not the
    state at the moment of the first watcher event in a burst — this
    is **last-write-wins by design**, not a race. A 200-event burst
    on one file produces exactly one upload with the final bytes.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    local_path = Path(local_path)
    _validate_local_for_upload(local_path)

    normalized_remote_path = normalize_manifest_path(remote_path)
    existing_entry = find_file_entry_in_shard(shard, normalized_remote_path)
    has_existing = existing_entry is not None and not bool(existing_entry.get("deleted"))

    plaintext_sha256, total_logical_size = _hash_file(local_path)
    content_fp_key = derive_content_fingerprint_key(vault.master_key)
    fingerprint = make_content_fingerprint(content_fp_key, plaintext_sha256)

    if has_existing:
        for version in existing_entry.get("versions", []) or []:
            if not isinstance(version, dict):
                continue
            if str(version.get("content_fingerprint", "")) == fingerprint and \
               not bool(existing_entry.get("deleted")):
                _report(progress, "done", 0, 0, 0)
                return PreparedUpload(
                    entry_id=str(existing_entry["entry_id"]),
                    version_id=str(version.get("version_id", "")),
                    normalized_remote_path=normalized_remote_path,
                    content_fingerprint=fingerprint,
                    logical_size=total_logical_size,
                    chunks_uploaded=0,
                    chunks_skipped=int(len(version.get("chunks") or [])),
                    bytes_uploaded=0,
                    version_payload=None,
                    skipped_identical=True,
                )

    # Dedupe pin: if a prior batched-prep for this (vault, path,
    # fingerprint) tuple persisted a stub, reuse its entry_id +
    # version_id so the chunk_ids match and HEAD-and-skip works.
    # Stubs whose fingerprint differs (the file was edited between
    # the first attempt and the retry) are reaped, since reusing
    # their version_id would produce chunks that don't match the
    # current bytes.
    stub_cache = Path(batch_cache_dir) if batch_cache_dir else default_upload_resume_dir()
    existing_stub = find_matching_stub(
        vault_id=vault.vault_id,
        remote_path=normalized_remote_path,
        content_fingerprint=fingerprint,
        cache_dir=stub_cache,
    )
    if existing_stub is not None:
        entry_id = existing_stub.entry_id
        version_id = existing_stub.version_id
        stub = existing_stub
    else:
        reap_stubs_for_path(
            vault_id=vault.vault_id,
            remote_path=normalized_remote_path,
            cache_dir=stub_cache,
            keep_fingerprint=fingerprint,
        )
        entry_id = (
            existing_entry["entry_id"] if existing_entry else generate_file_entry_id()
        )
        version_id = generate_file_version_id()
        stub = make_stub(
            vault_id=vault.vault_id,
            remote_path=normalized_remote_path,
            entry_id=entry_id,
            version_id=version_id,
            content_fingerprint=fingerprint,
        )
        save_stub(stub, stub_cache)

    chunks_plan, _ = _build_chunk_plan(
        vault=vault,
        local_path=local_path,
        remote_folder_id=remote_folder_id,
        entry_id=entry_id,
        version_id=version_id,
        chunk_size=chunk_size,
    )
    total_chunks = len(chunks_plan)

    chunk_ids = [chunk["chunk_id"] for chunk in chunks_plan]
    _report(progress, "checking", 0, total_chunks)
    heads = (
        relay.batch_head_chunks(vault.vault_id, vault.vault_access_secret, chunk_ids)
        if chunk_ids
        else {}
    )

    chunks_uploaded = 0
    chunks_skipped = 0
    bytes_uploaded = 0
    completed = 0
    for index, chunk in enumerate(chunks_plan):
        if should_continue is not None and not should_continue():
            log.info(
                "vault.sync.upload_cancelled vault=%s remote_path=%s "
                "chunks_done=%d total=%d (batch-prep)",
                vault.vault_id, normalized_remote_path, completed, total_chunks,
            )
            raise SyncCancelledError(
                f"upload cancelled at chunk {completed}/{total_chunks} of "
                f"{normalized_remote_path}"
            )
        head = heads.get(chunk["chunk_id"]) if isinstance(heads, dict) else None
        if isinstance(head, dict) and head.get("present"):
            chunks_skipped += 1
        else:
            relay.put_chunk(
                vault.vault_id,
                vault.vault_access_secret,
                chunk["chunk_id"],
                chunk["envelope"],
            )
            chunks_uploaded += 1
            bytes_uploaded += chunk["ciphertext_size"]
        completed += 1
        _report(progress, "uploading", completed, total_chunks, bytes_uploaded)

    if should_continue is not None and not should_continue():
        log.info(
            "vault.sync.upload_cancelled_pre_publish vault=%s remote_path=%s "
            "(batch-prep)",
            vault.vault_id, normalized_remote_path,
        )
        raise SyncCancelledError(
            f"upload cancelled before publish for {normalized_remote_path}"
        )

    timestamp = created_at or _now_rfc3339()
    version_payload = _make_version_payload(
        version_id=version_id,
        chunks_plan=chunks_plan,
        author_device_id=author_device_id,
        created_at=timestamp,
        logical_size=total_logical_size,
        content_fingerprint=fingerprint,
    )
    _report(progress, "ready_to_publish", total_chunks, total_chunks, bytes_uploaded)

    return PreparedUpload(
        entry_id=entry_id,
        version_id=version_id,
        normalized_remote_path=normalized_remote_path,
        content_fingerprint=fingerprint,
        logical_size=total_logical_size,
        chunks_uploaded=chunks_uploaded,
        chunks_skipped=chunks_skipped,
        bytes_uploaded=bytes_uploaded,
        version_payload=version_payload,
        skipped_identical=False,
        stub_session_id=stub.session_id,
        stub_cache_dir=str(stub_cache),
    )


def _build_chunk_plan(
    *,
    vault: UploadVault,
    local_path: Path,
    remote_folder_id: str,
    entry_id: str,
    version_id: str,
    chunk_size: int,
) -> tuple[list[dict[str, Any]], int]:
    """Read+encrypt the file in `chunk_size` slices."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    chunk_id_key = derive_chunk_id_key(vault.master_key)
    chunk_nonce_key = derive_chunk_nonce_key(vault.master_key)
    chunk_subkey = derive_subkey("dc-vault-v1/chunk", bytes(vault.master_key))

    plan: list[dict[str, Any]] = []
    total_size = 0
    index = 0
    with open(local_path, "rb") as fh:
        while True:
            plaintext = fh.read(chunk_size)
            if not plaintext and index > 0:
                break
            chunk_id = make_chunk_id(chunk_id_key, plaintext, version_id, index)
            nonce = make_chunk_nonce(chunk_nonce_key, plaintext, version_id, index)
            aad = build_chunk_aad(
                vault.vault_id,
                remote_folder_id,
                entry_id,
                version_id,
                index,
                len(plaintext),
            )
            ciphertext_and_tag = aead_encrypt(plaintext, chunk_subkey, nonce, aad)
            envelope = build_chunk_envelope(
                nonce=nonce, aead_ciphertext_and_tag=ciphertext_and_tag,
            )
            plan.append({
                "chunk_id": chunk_id,
                "index": index,
                "plaintext_size": len(plaintext),
                "ciphertext_size": len(envelope),
                "envelope": envelope,
            })
            total_size += len(plaintext)
            index += 1
            if not plaintext:
                # Empty file → produce one empty-plaintext chunk so the
                # file's manifest record is non-empty (matches download
                # which would otherwise see zero chunks for a 0-byte file).
                break
    return plan, total_size


def _make_version_payload(
    *,
    version_id: str,
    chunks_plan: list[dict[str, Any]],
    author_device_id: str,
    created_at: str,
    logical_size: int,
    content_fingerprint: str,
) -> dict[str, Any]:
    return {
        "version_id": version_id,
        "created_at": created_at,
        "modified_at": created_at,
        "logical_size": int(logical_size),
        "ciphertext_size": int(sum(c["ciphertext_size"] for c in chunks_plan)),
        "content_fingerprint": str(content_fingerprint),
        "author_device_id": str(author_device_id),
        "chunks": [
            {
                "chunk_id": c["chunk_id"],
                "index": int(c["index"]),
                "plaintext_size": int(c["plaintext_size"]),
                "ciphertext_size": int(c["ciphertext_size"]),
            }
            for c in chunks_plan
        ],
    }


def _publish_with_cas_retry(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    parent_state: FolderState,
    remote_folder_id: str,
    normalized_remote_path: str,
    version_payload: dict[str, Any],
    entry_id: str,
    author_device_id: str,
    max_retries: int = CAS_MAX_RETRIES,
) -> FolderState:
    """CAS-publish a single-version upload via ``publish_shard_with_root``.

    On 409, the server inlines the conflicting shard and/or root
    envelope(s) (§A1); we decrypt whichever side(s) we got back,
    rebuild the candidate on top of the new head(s), bump a fresh
    root revision, and retry. ``max_retries`` caps the loop to avoid
    livelocking against a busy multi-device vault.

    Returns the published ``(root, shard)`` state.
    """
    created_at = str(version_payload.get("created_at")) or _now_rfc3339()
    initial_parent = parent_state
    current_state = parent_state
    # Initial attempt: blind append (``latest_version_id = new_version``)
    # — matches the pre-Phase-H semantic where a non-conflicting publish
    # was a fresh upload, no tie-break needed. CAS retries (``use_merge
    # = True``) fold the local version onto the server head via §D4
    # merge so concurrent uploads from different devices land with the
    # right ``latest_version_id`` (tie-break) and/or path (rename).
    use_merge = False
    for attempt in range(max_retries):
        if use_merge:
            candidate_shard = _merge_local_version_into_shard_with_bump(
                server_shard=current_state.shard,
                parent_shard=initial_parent.shard,
                remote_folder_id=remote_folder_id,
                path=normalized_remote_path,
                version=version_payload,
                entry_id=entry_id,
                author_device_id=author_device_id,
                created_at=created_at,
            )
        else:
            candidate_shard = _apply_version_to_shard(
                current_state.shard,
                remote_folder_id=remote_folder_id,
                path=normalized_remote_path,
                version=version_payload,
                entry_id=entry_id,
                author_device_id=author_device_id,
                created_at=created_at,
            )
        candidate_root = _bumped_root_for_shard_publish(
            current_state.root,
            author_device_id=author_device_id,
            created_at=created_at,
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
                    "vault.upload.cas_exhausted vault=%s path=%s attempts=%d",
                    getattr(vault, "vault_id", "?"),
                    normalized_remote_path,
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
                "vault.upload.cas_retry attempt=%d/%d path=%s "
                "shard_conflict=%s root_conflict=%s",
                attempt + 1, max_retries, normalized_remote_path,
                bool(shard_envelope), bool(root_envelope),
            )
            current_state = FolderState(root=new_root, shard=new_shard)
            use_merge = True
    raise AssertionError("unreachable: loop exits via return or raise")


def _apply_version_to_shard(
    parent_shard: dict[str, Any],
    *,
    remote_folder_id: str,
    path: str,
    version: dict[str, Any],
    entry_id: str,
    author_device_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Fold a single new version onto ``parent_shard`` and bump revisions.

    Idempotent: re-applying the same version_id is a no-op. The
    canonical ``remote_folder_id`` discriminator is pinned so a
    synthetic genesis-shard parent becomes publishable on first
    publish.
    """
    parent_n = normalize_shard_plaintext(parent_shard)
    parent_revision = int(parent_n.get("shard_revision", 0))
    next_revision = parent_revision + 1
    candidate = add_or_append_file_version_in_shard(
        parent_n, path=path, version=version, entry_id=entry_id,
    )
    candidate["shard_revision"] = next_revision
    candidate["parent_shard_revision"] = parent_revision
    candidate["created_at"] = created_at
    candidate["author_device_id"] = str(author_device_id)
    candidate["remote_folder_id"] = remote_folder_id
    candidate["operation_log_tail"] = append_op_log_entries(
        parent_n.get("operation_log_tail"),
        [_upload_op_log_entry(
            path=path, device_id=author_device_id,
            revision=next_revision, created_at=created_at,
        )],
    )
    return candidate


def _merge_local_version_into_shard_with_bump(
    *,
    server_shard: dict[str, Any],
    parent_shard: dict[str, Any],
    remote_folder_id: str,
    path: str,
    version: dict[str, Any],
    entry_id: str,
    author_device_id: str,
    created_at: str,
) -> dict[str, Any]:
    """CAS-retry candidate: merge the local version onto the server head
    shard with §D4 tie-break, then bump the shard revision pair so the
    result is publishable.
    """
    server_n = normalize_shard_plaintext(server_shard)
    parent_revision = int(server_n.get("shard_revision", 0))
    next_revision = parent_revision + 1
    merged = merge_local_version_into_shard(
        server_n,
        parent_shard=parent_shard,
        entry_id=entry_id,
        path=path,
        version=version,
    )
    merged["shard_revision"] = next_revision
    merged["parent_shard_revision"] = parent_revision
    merged["created_at"] = created_at
    merged["author_device_id"] = str(author_device_id)
    merged["remote_folder_id"] = remote_folder_id
    # D7: prior tail comes from server_n so concurrent writers' entries
    # survive the CAS-retry replay.
    merged["operation_log_tail"] = append_op_log_entries(
        server_n.get("operation_log_tail"),
        [_upload_op_log_entry(
            path=path, device_id=author_device_id,
            revision=next_revision, created_at=created_at,
        )],
    )
    return merged


def _upload_op_log_entry(
    *,
    path: str,
    device_id: str,
    revision: int,
    created_at: str,
) -> dict[str, Any]:
    """Build the vault.upload.completed op-log entry for this publish.

    ``created_at`` is the RFC3339 string already stamped on the
    candidate shard; we parse it back to epoch seconds so the entry's
    ``ts`` agrees with the manifest's ``created_at`` within
    sub-second precision.
    """
    from datetime import datetime, timezone
    try:
        ts = int(datetime.strptime(
            created_at, "%Y-%m-%dT%H:%M:%S.000Z",
        ).replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        ts = None  # build_op_log_entry falls back to time.time()
    return build_op_log_entry(
        type="vault.upload.completed",
        device_id=device_id,
        revision=revision,
        path=path,
        ts=ts,
    )


def _bumped_root_for_shard_publish(
    parent_root: dict[str, Any],
    *,
    author_device_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Build a fresh root revision for ``publish_shard_with_root``.

    ``publish_shard_with_root`` patches the matching folder pointer's
    ``shard_hash`` + ``shard_revision`` internally before sealing —
    this helper only owns the vault-wide revision bump.
    """
    parent_n = normalize_root_manifest_plaintext(parent_root)
    parent_revision = int(parent_n.get("root_revision", 0))
    candidate = dict(parent_n)
    candidate["root_revision"] = parent_revision + 1
    candidate["parent_root_revision"] = parent_revision
    candidate["created_at"] = created_at
    candidate["author_device_id"] = str(author_device_id)
    return candidate


def _report(
    callback: Callable[[UploadProgress], None] | None,
    phase: str,
    completed_chunks: int,
    total_chunks: int,
    bytes_uploaded: int = 0,
) -> None:
    if callback is None:
        return
    callback(UploadProgress(
        phase=phase,
        completed_chunks=completed_chunks,
        total_chunks=total_chunks,
        bytes_uploaded=bytes_uploaded,
    ))
