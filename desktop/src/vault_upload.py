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

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Protocol

from .vault_crypto import (
    aead_encrypt,
    build_chunk_aad,
    build_chunk_envelope,
    derive_chunk_id_key,
    derive_content_fingerprint_key,
    derive_subkey,
    make_chunk_id,
    make_content_fingerprint,
)
from .vault_manifest import (
    add_or_append_file_version,
    find_file_entry,
    generate_file_entry_id,
    generate_file_version_id,
    normalize_manifest_path,
    normalize_manifest_plaintext,
)
from .vault_relay_errors import VaultRelayError


CHUNK_SIZE = 2 * 1024 * 1024  # 2 MiB; must match the download-side reader

UploadMode = Literal["new_file_or_version", "new_file_only", "append_version_only"]


class UploadConflictError(RuntimeError):
    """Raised when the chosen ``mode`` doesn't match the existing path state."""


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
) -> UploadResult:
    """Encrypt + upload ``local_path`` and CAS-publish a new manifest revision.

    The function does not retry on ``VaultCASConflictError`` — that's
    T6.3's job. On any error after the manifest mutation step, partial
    state is bounded to chunks already PUT to the relay; nothing local
    is touched.
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    local_path = Path(local_path)
    if not local_path.is_file():
        raise FileNotFoundError(f"local file not found: {local_path}")

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
    for chunk in chunks_plan:
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

    # Build the version payload + mutate the manifest.
    version_payload = _make_version_payload(
        version_id=version_id,
        chunks_plan=chunks_plan,
        author_device_id=author_device_id,
        created_at=created_at or _now_rfc3339(),
        logical_size=total_logical_size,
        content_fingerprint=fingerprint,
    )

    parent_revision = int(manifest.get("revision", 0))
    next_manifest = dict(normalize_manifest_plaintext(manifest))
    next_manifest["revision"] = parent_revision + 1
    next_manifest["parent_revision"] = parent_revision
    next_manifest["created_at"] = version_payload["created_at"]
    next_manifest["author_device_id"] = str(author_device_id)
    next_manifest = add_or_append_file_version(
        next_manifest,
        remote_folder_id=remote_folder_id,
        path=normalized_remote_path,
        version=version_payload,
        entry_id=entry_id,
    )

    published = vault.publish_manifest(relay, next_manifest, local_index=local_index)
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
            nonce = secrets.token_bytes(24)
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
    "UploadConflictError",
    "UploadProgress",
    "UploadResult",
    "VaultRelayError",
    "upload_file",
]
