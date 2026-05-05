"""Vault browser upload helpers (T6.1+).

Mirrors :mod:`vault_download`'s shape: the desktop owns the chunk
plan, encryption, manifest mutation and CAS publish; the relay only
sees opaque ciphertext and a revision-bumped manifest envelope.

T6.1 implements the no-conflict path (path doesn't exist → create
entry; path exists → append a new version, callers signal intent via
``mode="append_version"``). T6.2 layers the conflict-prompt UX on top
of this module.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Protocol


log = logging.getLogger(__name__)

from .vault_atomic import atomic_write_file
from .vault_binding_lifecycle import SyncCancelledError
from .vault_crypto import (
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
from .vault_browser_model import decrypt_manifest as decrypt_manifest_envelope
from .vault_manifest import (
    add_or_append_file_version,
    find_file_entry,
    generate_file_entry_id,
    generate_file_version_id,
    merge_with_remote_head,
    normalize_manifest_path,
    normalize_manifest_plaintext,
)
from .vault_relay_errors import (
    VaultCASConflictError,
    VaultQuotaExceededError,
    VaultRelayError,
)


CHUNK_SIZE = 2 * 1024 * 1024  # 2 MiB; must match the download-side reader
MAX_FILE_BYTES_DEFAULT = 2 * 1024 * 1024 * 1024  # §gaps §7 per-file cap (2 GiB)
CAS_MAX_RETRIES = 5

UploadMode = Literal["new_file_or_version", "new_file_only", "append_version_only"]


@dataclass(frozen=True)
class FileSkipped:
    """A file the folder walker decided not to upload."""
    relative_path: str
    reason: Literal["ignored", "too_large", "special"]
    size_bytes: int = 0


@dataclass(frozen=True)
class FolderUploadResult:
    manifest: dict[str, Any]
    uploaded: list["UploadResult"]
    skipped: list[FileSkipped] = field(default_factory=list)

    @property
    def chunks_uploaded(self) -> int:
        return sum(r.chunks_uploaded for r in self.uploaded)

    @property
    def chunks_skipped(self) -> int:
        return sum(r.chunks_skipped for r in self.uploaded)

    @property
    def bytes_uploaded(self) -> int:
        return sum(r.bytes_uploaded for r in self.uploaded)


@dataclass
class UploadSession:
    """Persisted across-process upload plan (T6.5).

    Lives at ``<cache_dir>/<session_id>.json`` and is rewritten in place
    after every chunk PUT success. Holds enough information to resume a
    killed mid-upload without re-deriving anything from the manifest.
    """
    session_id: str
    vault_id: str
    remote_folder_id: str
    remote_path: str
    entry_id: str
    version_id: str
    author_device_id: str
    content_fingerprint: str
    logical_size: int
    local_path: str
    chunk_size: int
    created_at: str
    chunks: list[dict[str, Any]]
    phase: Literal["uploading", "ready_to_publish", "complete"] = "uploading"

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "vault_id": self.vault_id,
            "remote_folder_id": self.remote_folder_id,
            "remote_path": self.remote_path,
            "entry_id": self.entry_id,
            "version_id": self.version_id,
            "author_device_id": self.author_device_id,
            "content_fingerprint": self.content_fingerprint,
            "logical_size": self.logical_size,
            "local_path": self.local_path,
            "chunk_size": self.chunk_size,
            "created_at": self.created_at,
            "chunks": list(self.chunks),
            "phase": self.phase,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "UploadSession":
        return cls(
            session_id=str(data["session_id"]),
            vault_id=str(data["vault_id"]),
            remote_folder_id=str(data["remote_folder_id"]),
            remote_path=str(data["remote_path"]),
            entry_id=str(data["entry_id"]),
            version_id=str(data["version_id"]),
            author_device_id=str(data["author_device_id"]),
            content_fingerprint=str(data.get("content_fingerprint", "")),
            logical_size=int(data["logical_size"]),
            local_path=str(data["local_path"]),
            chunk_size=int(data["chunk_size"]),
            created_at=str(data["created_at"]),
            chunks=list(data.get("chunks", [])),
            phase=data.get("phase", "uploading"),
        )


def default_upload_resume_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "desktop-connector" / "vault" / "uploads"


def save_session(session: UploadSession, cache_dir: Path) -> Path:
    """Atomically write the session JSON to ``<cache_dir>/<session_id>.json``."""
    cache_dir = Path(cache_dir)
    target = cache_dir / f"{session.session_id}.json"
    payload = json.dumps(session.to_json(), separators=(",", ":")).encode("utf-8")
    atomic_write_file(target, payload)
    return target


def clear_session(session_id: str, cache_dir: Path) -> None:
    cache_dir = Path(cache_dir)
    target = cache_dir / f"{session_id}.json"
    try:
        target.unlink()
    except FileNotFoundError:
        return


def list_resumable_sessions(vault_id: str, cache_dir: Path) -> list[UploadSession]:
    """Return every saved session that targets ``vault_id`` and is unfinished."""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return []
    out: list[UploadSession] = []
    for path in sorted(cache_dir.glob("*.json")):
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            session = UploadSession.from_json(data)
        except (KeyError, TypeError, ValueError):
            continue
        if session.vault_id != vault_id:
            continue
        if session.phase == "complete":
            continue
        out.append(session)
    return out


@dataclass(frozen=True)
class FolderUploadProgress:
    phase: Literal["walking", "uploading", "publishing", "done"]
    files_total: int
    files_completed: int
    bytes_total: int
    bytes_completed: int
    current_path: str = ""


class UploadConflictError(RuntimeError):
    """Raised when the chosen ``mode`` doesn't match the existing path state."""


class UploadSpecialFileSkipped(RuntimeError):
    """Raised when ``upload_file`` rejects a non-regular file (symlink/FIFO/etc).

    F-Y17 — a binding that contains a symlink would otherwise follow it
    and upload the symlink target as if it were the user's file. The
    sync engine catches this and treats the op as ``skipped``.
    """


class UploadFileTooLargeError(RuntimeError):
    """Raised when a single file exceeds ``MAX_FILE_BYTES_DEFAULT`` (F-D01)."""


class UploadVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_manifest(self, relay, *, local_index=None) -> dict: ...

    def publish_manifest(self, relay, manifest, *, local_index=None) -> dict: ...


class UploadRelay(Protocol):
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

    def put_manifest(self, *args, **kwargs) -> Any: ...


@dataclass(frozen=True)
class UploadProgress:
    phase: str
    completed_chunks: int
    total_chunks: int
    bytes_uploaded: int = 0


@dataclass(frozen=True)
class UploadResult:
    manifest: dict[str, Any]
    entry_id: str
    version_id: str
    path: str
    remote_folder_id: str
    chunks_uploaded: int
    chunks_skipped: int
    bytes_uploaded: int
    logical_size: int
    content_fingerprint: str
    skipped_identical: bool = False


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
    import stat as _stat
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
            plaintext = fh.read(int(session.chunk_size))
            chunk_id = str(record["chunk_id"])
            head = heads.get(chunk_id) if isinstance(heads, dict) else None
            already_done = bool(record.get("done"))
            if (already_done or (isinstance(head, dict) and head.get("present"))):
                chunks_skipped += 1
                # Even if the local session said done=False, a present
                # chunk on the relay means the PUT landed before the
                # crash — flush the flag.
                session.chunks[index]["done"] = True
                save_session(session, cache_dir)
            else:
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

    published = _publish_with_cas_retry(
        vault=vault,
        relay=relay,
        parent_manifest=manifest,
        remote_folder_id=session.remote_folder_id,
        normalized_remote_path=session.remote_path,
        version_payload=version_payload,
        entry_id=session.entry_id,
        author_device_id=session.author_device_id,
        local_index=local_index,
    )
    clear_session(session.session_id, cache_dir)
    _report(progress, "done", len(chunk_ids), len(chunk_ids), bytes_uploaded)

    return UploadResult(
        manifest=published,
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

    parent_n = normalize_manifest_plaintext(manifest)
    folder_entry = _find_remote_folder(parent_n, remote_folder_id)
    if folder_entry is None:
        raise ValueError(f"remote folder not found: {remote_folder_id}")
    ignore_patterns = list(folder_entry.get("ignore_patterns", []) or [])
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
            }[entry.reason]
            log.info("%s path=%s size=%d", event, entry.relative_path, entry.size_bytes)
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
            parent_manifest=parent_n,
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
    # short-circuits leave nothing to publish — the manifest is unchanged.
    if not any(addition for addition in additions):
        _report_folder(progress, "done", files_total, files_total, bytes_total, bytes_completed, "")
        return FolderUploadResult(
            manifest=parent_n,
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
    published = _publish_batch_with_cas_retry(
        vault=vault,
        relay=relay,
        parent_manifest=parent_n,
        additions=additions,
        author_device_id=author_device_id,
        local_index=local_index,
    )
    _report_folder(progress, "done", files_total, files_total, bytes_total, bytes_completed, "")

    # Re-stamp uploaded entries with the published manifest so the caller
    # sees consistent state in `result.uploaded[i].manifest`.
    upload_results = [
        UploadResult(
            manifest=published,
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
        manifest=published,
        uploaded=upload_results,
        skipped=skipped,
    )


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


def _find_remote_folder(manifest: dict[str, Any], remote_folder_id: str) -> dict[str, Any] | None:
    for folder in manifest.get("remote_folders", []) or []:
        if isinstance(folder, dict) and folder.get("remote_folder_id") == remote_folder_id:
            return folder
    return None


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
            except OSError:
                yield FileSkipped(child_rel, "special", 0)
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


def _matches_ignore(
    name: str,
    rel_path: str,
    patterns: Iterable[str],
    *,
    is_dir: bool,
) -> bool:
    """Subset of gitignore semantics covering the §gaps §7 default list:

    - ``pattern/`` — matches a directory by its leaf name; subtree pruned.
    - ``pattern`` — matches a file or directory by leaf name.
    - ``*.ext`` / ``~$*`` — fnmatch glob against the leaf.
    - ``foo/bar`` — slash-bearing pattern: fnmatch against the relative
      path so ``a/b.txt`` patterns work for nested config.

    Negation, ``**`` and rooted ``/foo`` patterns are not yet supported
    — the §7 defaults don't need them and v1.5 can extend if a
    user-written pattern requires more.
    """
    rel_unix = str(rel_path).replace("\\", "/")
    for raw in patterns:
        pat = str(raw).strip()
        if not pat or pat.startswith("#"):
            continue
        is_dir_pat = pat.endswith("/")
        if is_dir_pat:
            pat = pat[:-1]
        if "/" in pat:
            if fnmatch.fnmatch(rel_unix, pat):
                if not is_dir_pat or is_dir:
                    return True
            continue
        if is_dir_pat and not is_dir:
            continue
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _upload_one_into_batch(
    *,
    vault: UploadVault,
    relay: UploadRelay,
    parent_manifest: dict[str, Any],
    local_path: Path,
    remote_folder_id: str,
    remote_path: str,
    author_device_id: str,
    chunk_size: int,
    created_at: str | None,
    additions: list[_VersionAddition],
) -> "UploadResult":
    """Per-file half of ``upload_folder``: chunk + PUT + collect the version
    payload into ``additions`` for the batched manifest publish."""
    normalized_remote_path = normalize_manifest_path(remote_path)
    existing_entry = find_file_entry(parent_manifest, remote_folder_id, normalized_remote_path)
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
                    manifest=parent_manifest,
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
        manifest=parent_manifest,  # patched after the batch publish
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
    parent_manifest: dict[str, Any],
    additions: list[_VersionAddition],
    author_device_id: str,
    local_index: Any,
    max_retries: int = CAS_MAX_RETRIES,
) -> dict[str, Any]:
    """Apply N version additions to ``parent_manifest`` and CAS-publish.

    Same retry shape as ``_publish_with_cas_retry`` but every retry
    re-applies *all* additions on top of the freshly-fetched server
    head (via §D4 merge).
    """
    parent_n = normalize_manifest_plaintext(parent_manifest)
    parent_revision = int(parent_n.get("revision", 0))
    timestamp = _now_rfc3339()

    candidate = dict(parent_n)
    candidate["revision"] = parent_revision + 1
    candidate["parent_revision"] = parent_revision
    candidate["created_at"] = timestamp
    candidate["author_device_id"] = str(author_device_id)
    for addition in additions:
        candidate = add_or_append_file_version(
            candidate,
            remote_folder_id=addition.remote_folder_id,
            path=addition.path,
            version=addition.version,
            entry_id=addition.entry_id,
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
    return vault.publish_manifest(relay, last_attempt, local_index=local_index)


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
    return vault.publish_manifest(relay, last_attempt, local_index=local_index)


def _hash_file(local_path: Path) -> tuple[bytes, int]:
    """Stream a SHA-256 over the file; return (digest, byte length)."""
    h = hashlib.sha256()
    total = 0
    with open(local_path, "rb") as fh:
        while True:
            chunk = fh.read(1 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.digest(), total


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_conflict_renamed_path(
    remote_path: str,
    device_name: str,
    *,
    kind: str = "uploaded",
    now: datetime | None = None,
) -> str:
    """A20-style conflict-rename for "Keep both" uploads.

    Thin wrapper over :func:`vault_conflict_naming.make_conflict_path`.
    Kept as a stable import for the existing T6.2 callers.
    """
    from .vault_conflict_naming import make_conflict_path
    return make_conflict_path(
        original_path=remote_path,
        kind=kind,
        when=now,
        device_name=device_name,
    )


def describe_quota_exceeded(error: VaultQuotaExceededError) -> dict[str, Any]:
    """Format a 507 ``vault_quota_exceeded`` for UI surfacing (T6.6).

    Returns ``{eviction_available: bool, used_bytes, quota_bytes, percent,
    heading, body, primary_action_label}``. The heading + body strings
    come straight from §D2:

    - Eviction-available variant offers to free space (the actual eviction
      pass lands in T7 — for T6.6 the button just sets up the prompt).
    - No-history variant is the §D2 step-4 terminal banner: sync stopped,
      no automatic recovery, user must export or migrate.
    """
    used = max(0, int(error.used_bytes or 0))
    quota = max(0, int(error.quota_bytes or 0))
    percent = (used * 100) // quota if quota else 100
    if error.eviction_available:
        heading = "Vault is full — make space?"
        body = (
            f"This vault is at {percent}% of its quota ({used} / {quota} bytes). "
            "Old historical versions can be purged to make room for the new upload. "
            "Eviction lands in T7; for now the upload pauses."
        )
        primary_action_label = "Make space"
    else:
        heading = "Vault is full and no backup history remains."
        body = (
            "Sync is stopped. Free space by deleting files, or export and "
            "migrate to a relay with more capacity."
        )
        primary_action_label = "Open vault settings"
    return {
        "eviction_available": bool(error.eviction_available),
        "used_bytes": used,
        "quota_bytes": quota,
        "percent": percent,
        "heading": heading,
        "body": body,
        "primary_action_label": primary_action_label,
    }


def detect_path_conflict(
    manifest: dict[str, Any],
    remote_folder_id: str,
    remote_path: str,
) -> bool:
    """Return True if ``remote_path`` already has a non-deleted file entry.

    Uses ``find_file_entry`` (which returns deleted entries too) so the
    UI's conflict prompt fires only for live entries — re-uploading over
    a tombstone implicitly restores the file in T6.1.
    """
    entry = find_file_entry(manifest, remote_folder_id, remote_path)
    if entry is None:
        return False
    return not bool(entry.get("deleted"))


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


__all__ = [
    "CHUNK_SIZE",
    "MAX_FILE_BYTES_DEFAULT",
    "FileSkipped",
    "FolderUploadProgress",
    "FolderUploadResult",
    "UploadConflictError",
    "UploadProgress",
    "UploadResult",
    "UploadSession",
    "VaultRelayError",
    "clear_session",
    "default_upload_resume_dir",
    "describe_quota_exceeded",
    "detect_path_conflict",
    "list_resumable_sessions",
    "make_conflict_renamed_path",
    "resume_upload",
    "save_session",
    "upload_file",
    "upload_folder",
]
