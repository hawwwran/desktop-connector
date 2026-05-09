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

from ..vault_binding_lifecycle import SyncCancelledError
from ..vault_browser_model import decrypt_manifest as decrypt_manifest_envelope
from ..vault_crypto import (
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
from ..vault_manifest import (
    add_or_append_file_version,
    find_file_entry,
    generate_file_entry_id,
    generate_file_version_id,
    merge_with_remote_head,
    normalize_manifest_path,
    normalize_manifest_plaintext,
)
from ..vault.relay_errors import VaultCASConflictError
from .constants import CAS_MAX_RETRIES, CHUNK_SIZE, MAX_FILE_BYTES_DEFAULT, UploadMode
from .errors import UploadConflictError, UploadFileTooLargeError, UploadSpecialFileSkipped
from .hashing import _hash_file, _now_rfc3339
from .protocols import UploadRelay, UploadVault
from .results import UploadProgress, UploadResult
from .session import UploadSession, default_upload_resume_dir, clear_session, save_session

log = logging.getLogger(__name__)


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

    normalized_remote_path = normalize_manifest_path(remote_path)
    existing_entry = find_file_entry(manifest, remote_folder_id, normalized_remote_path)
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
                    manifest=normalize_manifest_plaintext(manifest),
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

    published = _publish_with_cas_retry(
        vault=vault,
        relay=relay,
        parent_manifest=manifest,
        remote_folder_id=remote_folder_id,
        normalized_remote_path=normalized_remote_path,
        version_payload=version_payload,
        entry_id=entry_id,
        author_device_id=author_device_id,
        local_index=local_index,
    )
    # F-D05: mark the session as published BEFORE the filesystem-side
    # cleanup. If clear_session fails (rare disk error), the
    # list_resumable_sessions filter still skips this row so we don't
    # republish a duplicate version on the next resume.
    session.phase = "complete"
    save_session(session, cache_dir)
    clear_session(session.session_id, cache_dir)
    _report(progress, "done", total_chunks, total_chunks, bytes_uploaded)

    # F-510: anchor the Activity tab's "Uploaded" timeline row.
    log.info(
        "vault.upload.completed vault=%s revision=%d path=%s",
        vault.vault_id,
        int(published.get("revision", 0)) if isinstance(published, dict) else 0,
        normalized_remote_path,
    )

    return UploadResult(
        manifest=published,
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
    parent_manifest: dict[str, Any],
    remote_folder_id: str,
    normalized_remote_path: str,
    version_payload: dict[str, Any],
    entry_id: str,
    author_device_id: str,
    local_index: Any,
    max_retries: int = CAS_MAX_RETRIES,
) -> dict[str, Any]:
    """CAS-publish a single-version upload, retrying via §D4 on 409.

    Each retry decrypts the server head from the 409 details (no follow-up
    GET — server inlines the manifest per §A1), runs ``merge_with_remote
    _head`` to rebuild the local change on top of the new revision, and
    publishes again. Cap is ``max_retries`` to avoid livelocking against
    a busy multi-device vault.
    """
    parent_n = normalize_manifest_plaintext(parent_manifest)
    parent_revision = int(parent_n.get("revision", 0))

    candidate = dict(parent_n)
    candidate["revision"] = parent_revision + 1
    candidate["parent_revision"] = parent_revision
    candidate["created_at"] = str(version_payload.get("created_at"))
    candidate["author_device_id"] = str(author_device_id)
    candidate = add_or_append_file_version(
        candidate,
        remote_folder_id=remote_folder_id,
        path=normalized_remote_path,
        version=version_payload,
        entry_id=entry_id,
    )

    rebased_parent = parent_n
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
    # One more try after the final merge — no exception means success;
    # any 409 here propagates as the caller's terminal CAS error.
    # F-D25: tag exhaustion separately from a first-attempt 409 so the
    # activity log distinguishes "live CAS race the user retried" from
    # "we ran out of retry budget against a busy multi-device vault".
    try:
        return vault.publish_manifest(relay, last_attempt, local_index=local_index)
    except VaultCASConflictError:
        log.warning(
            "vault.upload.cas_exhausted vault=%s path=%s retries=%d",
            getattr(vault, "vault_id", "?"),
            normalized_remote_path,
            max_retries,
        )
        raise


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
