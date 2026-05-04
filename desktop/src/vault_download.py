"""Vault browser download helpers (T5.3/T5.4)."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Protocol

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


@dataclass(frozen=True)
class _FolderFilePlan:
    display_path: str
    relative_path: Path
    remote_folder_id: str
    file_id: str
    entry: dict[str, Any]
    version: dict[str, Any]
    chunks: list[dict[str, Any]]


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


def download_folder(
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
    """Download a remote folder's current, non-deleted tree."""
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    normalized = normalize_manifest_plaintext(manifest)
    plans = _folder_file_plans(normalized, path)
    final_root = resolve_folder_destination(Path(destination), existing_policy)
    total_logical_size = sum(_int_value(plan.version.get("logical_size")) for plan in plans)
    _preflight_folder_disk_space(final_root, total_logical_size)

    total_chunks = sum(len(plan.chunks) for plan in plans)
    chunk_ids = _unique_chunk_ids(plan.chunks for plan in plans)
    _report(progress, "checking", 0, total_chunks)
    heads = (
        relay.batch_head_chunks(vault.vault_id, vault.vault_access_secret, chunk_ids)
        if chunk_ids
        else {}
    )
    for plan in plans:
        for chunk in plan.chunks:
            info = heads.get(chunk["chunk_id"])
            if not isinstance(info, dict) or not info.get("present"):
                raise VaultChunkMissingError(f"vault chunk missing: {chunk['chunk_id']}")

    final_root.mkdir(parents=True, exist_ok=True)
    completed = 0
    bytes_written = 0
    for plan in plans:
        file_path = final_root / plan.relative_path

        def plaintext_chunks(plan=plan) -> Iterable[bytes]:
            nonlocal completed, bytes_written
            for chunk in plan.chunks:
                encrypted = _load_cached_chunk(
                    chunk_cache_dir=chunk_cache_dir,
                    vault_id=vault.vault_id,
                    chunk_id=chunk["chunk_id"],
                    head=heads[chunk["chunk_id"]],
                )
                if encrypted is None:
                    encrypted = relay.get_chunk(
                        vault.vault_id,
                        vault.vault_access_secret,
                        chunk["chunk_id"],
                    )

                plaintext = _decrypt_chunk(
                    vault=vault,
                    remote_folder_id=plan.remote_folder_id,
                    file_id=plan.file_id,
                    version_id=str(plan.version.get("version_id", "")),
                    chunk=chunk,
                    encrypted=encrypted,
                )
                _store_cached_chunk(chunk_cache_dir, vault.vault_id, chunk["chunk_id"], encrypted)
                completed += 1
                bytes_written += len(plaintext)
                _report(progress, "downloading", completed, total_chunks, bytes_written)
                yield plaintext

        written_for_file = atomic_write_chunks(file_path, plaintext_chunks())
        expected_size = _int_value(plan.version.get("logical_size"))
        if expected_size and written_for_file != expected_size:
            raise ValueError(
                f"downloaded size mismatch for {plan.display_path}: "
                f"expected {expected_size}, got {written_for_file}"
            )

    _report(progress, "done", total_chunks, total_chunks, bytes_written)
    return final_root


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


def resolve_folder_destination(destination: Path, policy: ExistingFilePolicy) -> Path:
    destination = Path(destination)
    if not destination.exists():
        return destination
    if policy == "overwrite":
        if not destination.is_dir():
            raise NotADirectoryError(f"destination is not a folder: {destination}")
        return destination
    if policy == "keep_both":
        return _keep_both_folder_path(destination)
    if policy == "cancel":
        raise DownloadCancelled("download cancelled")
    raise ExistingDestinationError(f"destination already exists: {destination}")


def atomic_write_file(destination: Path, data: bytes) -> None:
    """Power-loss-safe write using the T0 §gaps §11 temp-file pattern."""
    atomic_write_chunks(destination, (data,))


def atomic_write_chunks(destination: Path, chunks: Iterable[bytes]) -> int:
    """Power-loss-safe streaming write using an adjacent temp file."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f"{destination.name}.dc-temp-{uuid.uuid4().hex}")
    written = 0
    try:
        with open(tmp, "wb") as fh:
            for chunk in chunks:
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, destination)
        _fsync_dir(destination.parent)
        return written
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
    parts = _split_display_path(path)
    if not parts:
        raise KeyError(f"file not found: {path}")
    for folder in manifest.get("remote_folders", []):
        if not isinstance(folder, dict):
            continue
        if str(folder.get("display_name_enc") or "") == parts[0]:
            return folder
    raise KeyError(f"folder not found: {parts[0]}")


def _folder_file_plans(manifest: dict[str, Any], path: str) -> list[_FolderFilePlan]:
    folder = _folder_for_display_path(manifest, path)
    display_parts = _split_display_path(path)
    if not display_parts:
        raise KeyError("choose a remote folder to download")
    prefix = tuple(display_parts[1:])
    remote_folder_id = str(folder["remote_folder_id"])
    display_folder_name = str(folder.get("display_name_enc") or "")
    entries = folder.get("entries", [])
    if not isinstance(entries, list):
        entries = []

    plans: list[_FolderFilePlan] = []
    seen_relative_paths: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if bool(entry.get("deleted")) or str(entry.get("type", "file")) != "file":
            continue
        entry_parts = _safe_manifest_path_parts(str(entry.get("path", "")))
        if len(entry_parts) < len(prefix) or tuple(entry_parts[:len(prefix)]) != prefix:
            continue
        relative_parts = tuple(entry_parts[len(prefix):])
        if not relative_parts:
            continue
        relative_path = Path(*relative_parts)
        if relative_path in seen_relative_paths:
            raise ValueError(f"duplicate current file path in folder: {relative_path}")
        seen_relative_paths.add(relative_path)

        version = _latest_version(entry)
        if version is None:
            raise ValueError(f"file has no downloadable version: {entry.get('path', '')}")
        display_path = "/".join([display_folder_name, *entry_parts])
        plans.append(_FolderFilePlan(
            display_path=display_path,
            relative_path=relative_path,
            remote_folder_id=remote_folder_id,
            file_id=str(entry.get("entry_id", "")),
            entry=entry,
            version=version,
            chunks=_version_chunks(version),
        ))

    return sorted(plans, key=lambda plan: str(plan.relative_path).casefold())


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


def _preflight_folder_disk_space(destination: Path, logical_size: int) -> None:
    required = int(max(0, logical_size) * 1.25)
    if required <= 0:
        return
    probe = _nearest_existing_parent(Path(destination))
    free = shutil.disk_usage(probe).free
    if free < required:
        raise VaultLocalDiskFullError(
            f"not enough free space for folder download: required {required} bytes, "
            f"available {free} bytes at {probe}"
        )


def _keep_both_path(destination: Path) -> Path:
    stem = destination.stem
    suffix = destination.suffix
    for index in range(1, 10_000):
        candidate = destination.with_name(f"{stem} (downloaded {index}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not choose a keep-both path for {destination}")


def _keep_both_folder_path(destination: Path) -> Path:
    for index in range(1, 10_000):
        candidate = destination.with_name(f"{destination.name} (downloaded {index})")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not choose a keep-both folder path for {destination}")


def _nearest_existing_parent(path: Path) -> Path:
    probe = Path(path)
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            return probe
        probe = parent
    return probe


def _split_display_path(path: str) -> list[str]:
    return [
        part for part in str(path).replace("\\", "/").split("/")
        if part and part != "."
    ]


def _safe_manifest_path_parts(path: str) -> tuple[str, ...]:
    parts = []
    for part in str(path).replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(f"unsafe vault path: {path}")
        parts.append(part)
    if not parts:
        raise ValueError("empty vault file path")
    return tuple(parts)


def _unique_chunk_ids(chunk_lists: Iterable[list[dict[str, Any]]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for chunks in chunk_lists:
        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id and chunk_id not in seen:
                seen.add(chunk_id)
                out.append(chunk_id)
    return out


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
