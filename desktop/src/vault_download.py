"""Vault browser download helpers (T5.3)."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from .vault_browser_model import get_file
from .vault_crypto import (
    aead_decrypt,
    build_chunk_aad,
    derive_subkey,
    normalize_vault_id,
)
from .vault_manifest import normalize_manifest_plaintext


ExistingFilePolicy = Literal["fail", "overwrite", "keep_both", "cancel"]


class DownloadCancelled(Exception):
    """Raised when the caller chooses not to overwrite an existing file."""


class ExistingDestinationError(FileExistsError):
    """Raised when the destination exists and no overwrite policy was chosen."""


class VaultLocalDiskFullError(OSError):
    """Local destination volume does not have enough free space."""


class VaultChunkMissingError(RuntimeError):
    """The manifest references a chunk the relay does not currently have."""


class ChunkRelay(Protocol):
    def batch_head_chunks(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]: ...

    def get_chunk(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_id: str,
    ) -> bytes: ...


class DownloadVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...


@dataclass(frozen=True)
class DownloadProgress:
    phase: str
    completed_chunks: int
    total_chunks: int
    bytes_written: int = 0


def download_latest_file(
    *,
    vault: DownloadVault,
    relay: ChunkRelay,
    manifest: dict[str, Any],
    path: str,
    destination: Path,
    existing_policy: ExistingFilePolicy = "fail",
    chunk_cache_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
) -> Path:
    """Download one file's latest non-deleted version to ``destination``."""
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    normalized = normalize_manifest_plaintext(manifest)
    folder = _folder_for_display_path(normalized, path)
    entry = get_file(normalized, path)
    if bool(entry.get("deleted")):
        raise ValueError("cannot download a deleted file")

    version = _latest_version(entry)
    if version is None:
        raise ValueError("file has no downloadable version")

    chunks = _version_chunks(version)
    final_path = resolve_download_destination(Path(destination), existing_policy)
    _preflight_disk_space(final_path, _int_value(version.get("logical_size")))

    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    _report(progress, "checking", 0, len(chunks))
    heads = relay.batch_head_chunks(vault.vault_id, vault.vault_access_secret, chunk_ids)
    for chunk in chunks:
        info = heads.get(chunk["chunk_id"])
        if not isinstance(info, dict) or not info.get("present"):
            raise VaultChunkMissingError(f"vault chunk missing: {chunk['chunk_id']}")

    plaintext_parts: list[bytes] = []
    completed = 0
    bytes_written = 0
    for chunk in chunks:
        encrypted = _load_cached_chunk(
            chunk_cache_dir=chunk_cache_dir,
            vault_id=vault.vault_id,
            chunk_id=chunk["chunk_id"],
            head=heads[chunk["chunk_id"]],
        )
        if encrypted is None:
            encrypted = relay.get_chunk(vault.vault_id, vault.vault_access_secret, chunk["chunk_id"])

        plaintext = _decrypt_chunk(
            vault=vault,
            remote_folder_id=str(folder["remote_folder_id"]),
            file_id=str(entry.get("entry_id", "")),
            version_id=str(version.get("version_id", "")),
            chunk=chunk,
            encrypted=encrypted,
        )
        _store_cached_chunk(chunk_cache_dir, vault.vault_id, chunk["chunk_id"], encrypted)
        plaintext_parts.append(plaintext)
        completed += 1
        bytes_written += len(plaintext)
        _report(progress, "downloading", completed, len(chunks), bytes_written)

    data = b"".join(plaintext_parts)
    expected_size = _int_value(version.get("logical_size"))
    if expected_size and len(data) != expected_size:
        raise ValueError(f"downloaded size mismatch: expected {expected_size}, got {len(data)}")

    atomic_write_file(final_path, data)
    _report(progress, "done", len(chunks), len(chunks), len(data))
    return final_path


def resolve_download_destination(destination: Path, policy: ExistingFilePolicy) -> Path:
    destination = Path(destination)
    if not destination.exists():
        return destination
    if policy == "overwrite":
        return destination
    if policy == "keep_both":
        return _keep_both_path(destination)
    if policy == "cancel":
        raise DownloadCancelled("download cancelled")
    raise ExistingDestinationError(f"destination already exists: {destination}")


def atomic_write_file(destination: Path, data: bytes) -> None:
    """Power-loss-safe write using the T0 §gaps §11 temp-file pattern."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f"{destination.name}.dc-temp-{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, destination)
        _fsync_dir(destination.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def vault_chunk_cache_path(cache_dir: Path, vault_id: str, chunk_id: str) -> Path:
    canonical = normalize_vault_id(vault_id)
    return Path(cache_dir) / "chunks" / canonical / chunk_id[6:8] / chunk_id


def default_vault_download_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "desktop-connector" / "vault"


def _decrypt_chunk(
    *,
    vault: DownloadVault,
    remote_folder_id: str,
    file_id: str,
    version_id: str,
    chunk: dict[str, Any],
    encrypted: bytes,
) -> bytes:
    if vault.master_key is None:
        raise ValueError("vault is closed")
    if len(encrypted) < 24 + 16:
        raise ValueError(f"chunk envelope too short: {chunk['chunk_id']}")
    nonce = encrypted[:24]
    ciphertext = encrypted[24:]
    plaintext_size = _int_value(chunk.get("plaintext_size"))
    aad = build_chunk_aad(
        vault.vault_id,
        remote_folder_id,
        file_id,
        version_id,
        int(chunk.get("index", 0)),
        plaintext_size,
    )
    subkey = derive_subkey("dc-vault-v1/chunk", vault.master_key)
    plaintext = aead_decrypt(ciphertext, subkey, nonce, aad)
    if plaintext_size and len(plaintext) != plaintext_size:
        raise ValueError(
            f"chunk plaintext size mismatch for {chunk['chunk_id']}: "
            f"expected {plaintext_size}, got {len(plaintext)}"
        )
    return plaintext


def _load_cached_chunk(
    *,
    chunk_cache_dir: Path | None,
    vault_id: str,
    chunk_id: str,
    head: dict[str, Any],
) -> bytes | None:
    if chunk_cache_dir is None:
        return None
    path = vault_chunk_cache_path(chunk_cache_dir, vault_id, chunk_id)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    expected_size = _int_value(head.get("size"))
    expected_hash = str(head.get("hash") or "")
    if expected_size and len(data) != expected_size:
        return None
    if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
        return None
    return data


def _store_cached_chunk(
    chunk_cache_dir: Path | None,
    vault_id: str,
    chunk_id: str,
    data: bytes,
) -> None:
    if chunk_cache_dir is None:
        return
    path = vault_chunk_cache_path(chunk_cache_dir, vault_id, chunk_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_file(path, data)


def _folder_for_display_path(manifest: dict[str, Any], path: str) -> dict[str, Any]:
    parts = [part for part in str(path).replace("\\", "/").split("/") if part and part != "."]
    if not parts:
        raise KeyError(f"file not found: {path}")
    for folder in manifest.get("remote_folders", []):
        if not isinstance(folder, dict):
            continue
        if str(folder.get("display_name_enc") or "") == parts[0]:
            return folder
    raise KeyError(f"folder not found: {parts[0]}")


def _latest_version(entry: dict[str, Any]) -> dict[str, Any] | None:
    versions = [v for v in entry.get("versions", []) if isinstance(v, dict)]
    latest_id = str(entry.get("latest_version_id") or "")
    if latest_id:
        for version in versions:
            if str(version.get("version_id", "")) == latest_id:
                return version
    if versions:
        return versions[-1]
    return None


def _version_chunks(version: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = version.get("chunks", [])
    if not isinstance(chunks, list):
        return []
    out = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or "")
        if not chunk_id:
            continue
        out.append(dict(chunk, chunk_id=chunk_id))
    return sorted(out, key=lambda c: int(c.get("index", 0)))


def _preflight_disk_space(destination: Path, logical_size: int) -> None:
    required = int(max(0, logical_size) * 1.25)
    if required <= 0:
        return
    parent = Path(destination).parent
    parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(parent).free
    if free < required:
        raise VaultLocalDiskFullError(
            f"not enough free space for download: required {required} bytes, "
            f"available {free} bytes at {parent}"
        )


def _keep_both_path(destination: Path) -> Path:
    stem = destination.stem
    suffix = destination.suffix
    for index in range(1, 10_000):
        candidate = destination.with_name(f"{stem} (downloaded {index}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not choose a keep-both path for {destination}")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _report(
    callback: Callable[[DownloadProgress], None] | None,
    phase: str,
    completed_chunks: int,
    total_chunks: int,
    bytes_written: int = 0,
) -> None:
    if callback is not None:
        callback(DownloadProgress(
            phase=phase,
            completed_chunks=completed_chunks,
            total_chunks=total_chunks,
            bytes_written=bytes_written,
        ))


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
