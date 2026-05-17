"""Resume a partially-completed ``upload_file`` from disk (T6.5).

Walks the saved session, batch-HEADs the chunk_ids, re-encrypts and
re-PUTs anything still missing (deterministic-nonce crypto means the
envelope bytes are byte-identical to the pre-crash run, so the relay's
hash-equality idempotency turns retries into 200 OKs), then runs the
normal CAS publish.
"""

import logging
import os
from pathlib import Path
from typing import Any, Callable

from ..binding.lifecycle import SyncCancelledError
from ..crypto import (
    aead_encrypt,
    build_chunk_aad,
    build_chunk_envelope,
    derive_chunk_id_key,
    derive_chunk_nonce_key,
    derive_subkey,
    make_chunk_id,
    make_chunk_nonce,
)
from ..manifest import assemble_unified_manifest
from .folder_state import fetch_folder_state
from .protocols import UploadRelay, UploadVault
from .results import UploadProgress, UploadResult
from .session import UploadSession, default_upload_resume_dir, clear_session, save_session
from .single_file import _publish_with_cas_retry, _report

log = logging.getLogger(__name__)


def resume_upload(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    manifest: dict[str, Any],
    session: UploadSession,
    progress: Callable[[UploadProgress], None] | None = None,
    local_index: Any = None,
    resume_cache_dir: Path | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> UploadResult:
    """Pick up a partially-completed ``upload_file`` from disk (T6.5).

    Walks the saved session, batch-HEADs the chunk_ids to learn what
    the relay already has, re-encrypts and re-PUTs anything still
    missing (deterministic-nonce crypto means the envelope bytes are
    byte-identical to the pre-crash run, so the relay's hash-equality
    idempotency turns retries into 200 OKs), then runs the normal CAS
    publish.

    F-Y08: ``should_continue`` is checked between each chunk PUT and
    once before publish — same contract as :func:`upload_file`. The
    on-disk session keeps progress so a second resume picks up
    seamlessly.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")
    if session.vault_id != vault.vault_id:
        raise ValueError(
            f"session is for vault {session.vault_id!r}, got {vault.vault_id!r}"
        )

    cache_dir = resume_cache_dir or default_upload_resume_dir()
    local_path = Path(session.local_path)
    if not local_path.is_file():
        raise FileNotFoundError(f"local file no longer present: {local_path}")

    chunk_ids = [str(c["chunk_id"]) for c in session.chunks]
    _report(progress, "checking", 0, len(chunk_ids))
    heads = (
        relay.batch_head_chunks(vault.vault_id, vault.vault_access_secret, chunk_ids)
        if chunk_ids
        else {}
    )

    chunks_uploaded = 0
    chunks_skipped = 0
    bytes_uploaded = 0
    plaintext_chunks: list[dict[str, Any]] = []
    chunk_id_key = derive_chunk_id_key(vault.master_key)
    chunk_nonce_key = derive_chunk_nonce_key(vault.master_key)
    chunk_subkey = derive_subkey("dc-vault-v1/chunk", bytes(vault.master_key))

    completed = 0
    with open(local_path, "rb") as fh:
        for index, record in enumerate(session.chunks):
            # F-Y08: same per-chunk bail as upload_file. Already-done
            # chunks are still counted (cheap) so the resume can finish
            # where it left off if Pause is followed by another resume.
            if should_continue is not None and not should_continue():
                log.info(
                    "vault.sync.resume_cancelled vault=%s session=%s chunks_done=%d total=%d",
                    vault.vault_id, session.session_id, completed, len(chunk_ids),
                )
                raise SyncCancelledError(
                    f"resume cancelled at chunk {completed}/{len(chunk_ids)} of "
                    f"{session.remote_path}"
                )
            chunk_id = str(record["chunk_id"])
            head = heads.get(chunk_id) if isinstance(heads, dict) else None
            already_done = bool(record.get("done"))
            head_present = isinstance(head, dict) and head.get("present")
            # F-D12: if both the local session AND the relay agree this
            # chunk is already uploaded, seek past the bytes instead of
            # reading them. Resuming the last 50 MB of a 2 GiB file no
            # longer requires re-reading 1.95 GiB just to skip them.
            # The "file changed since the original upload" check
            # naturally still fires for any chunk that needs to be
            # re-PUT (the else branch), which is the only branch where
            # plaintext mismatch would actually matter — the relay
            # already has the old bytes for the seek-skip case.
            if already_done and head_present:
                fh.seek(int(session.chunk_size), os.SEEK_CUR)
                chunks_skipped += 1
            elif already_done or head_present:
                # Mixed signal — relay says yes but session says no, or
                # vice versa. Read past the bytes (the file pointer
                # still advances under the original pre-F-D12 semantic
                # of "any-true skips re-PUT") and flush the done flag
                # so the next resume short-circuits via the seek-skip
                # branch above. The plaintext is otherwise unused —
                # re-deriving chunk_id here would trip on
                # legitimately-changed-but-already-published files.
                fh.seek(int(session.chunk_size), os.SEEK_CUR)
                chunks_skipped += 1
                session.chunks[index]["done"] = True
                save_session(session, cache_dir)
            else:
                plaintext = fh.read(int(session.chunk_size))
                # Re-encrypt deterministically so the envelope bytes match
                # whatever was stored before (or what would have been). The
                # T6.1 chunk_id derivation already binds plaintext + version
                # + index, so we re-derive it for cross-check.
                derived_id = make_chunk_id(
                    chunk_id_key, plaintext, session.version_id, index,
                )
                if derived_id != chunk_id:
                    raise RuntimeError(
                        f"resume mismatch: session expects {chunk_id!r} but "
                        f"local bytes hash to {derived_id!r} — file changed "
                        "since the original upload"
                    )
                nonce = make_chunk_nonce(
                    chunk_nonce_key, plaintext, session.version_id, index,
                )
                aad = build_chunk_aad(
                    vault.vault_id,
                    session.remote_folder_id,
                    session.entry_id,
                    session.version_id,
                    index,
                    len(plaintext),
                )
                ciphertext = aead_encrypt(plaintext, chunk_subkey, nonce, aad)
                envelope = build_chunk_envelope(
                    nonce=nonce, aead_ciphertext_and_tag=ciphertext,
                )
                relay.put_chunk(
                    vault.vault_id, vault.vault_access_secret, chunk_id, envelope,
                )
                chunks_uploaded += 1
                bytes_uploaded += len(envelope)
                session.chunks[index]["done"] = True
                save_session(session, cache_dir)
            plaintext_chunks.append({
                "chunk_id": chunk_id,
                "index": index,
                "plaintext_size": int(record["plaintext_size"]),
                "ciphertext_size": int(record["ciphertext_size"]),
            })
            completed += 1
            _report(progress, "uploading", completed, len(chunk_ids), bytes_uploaded)

    # F-Y08: pre-publish checkpoint, same as upload_file.
    if should_continue is not None and not should_continue():
        log.info(
            "vault.sync.resume_cancelled_pre_publish vault=%s session=%s",
            vault.vault_id, session.session_id,
        )
        raise SyncCancelledError(
            f"resume cancelled before publish for {session.remote_path}"
        )

    version_payload = {
        "version_id": session.version_id,
        "created_at": session.created_at,
        "modified_at": session.created_at,
        "logical_size": int(session.logical_size),
        "ciphertext_size": int(sum(c["ciphertext_size"] for c in plaintext_chunks)),
        "content_fingerprint": session.content_fingerprint,
        "author_device_id": session.author_device_id,
        "chunks": plaintext_chunks,
    }

    session.phase = "ready_to_publish"
    save_session(session, cache_dir)

    # Phase H step 4: fetch the binding's sharded state fresh; the
    # ``manifest`` kwarg is accepted for caller compatibility and
    # ignored.
    parent_state = fetch_folder_state(
        vault, relay, session.remote_folder_id, session.author_device_id,
    )
    published_state = _publish_with_cas_retry(
        vault=vault,
        relay=relay,
        parent_state=parent_state,
        remote_folder_id=session.remote_folder_id,
        normalized_remote_path=session.remote_path,
        version_payload=version_payload,
        entry_id=session.entry_id,
        author_device_id=session.author_device_id,
    )
    clear_session(session.session_id, cache_dir)
    _report(progress, "done", len(chunk_ids), len(chunk_ids), bytes_uploaded)

    return UploadResult(
        manifest=assemble_unified_manifest(
            published_state.root,
            {session.remote_folder_id: published_state.shard},
        ),
        entry_id=session.entry_id,
        version_id=session.version_id,
        path=session.remote_path,
        remote_folder_id=session.remote_folder_id,
        chunks_uploaded=chunks_uploaded,
        chunks_skipped=chunks_skipped,
        bytes_uploaded=bytes_uploaded,
        logical_size=int(session.logical_size),
        content_fingerprint=session.content_fingerprint,
        skipped_identical=False,
    )
