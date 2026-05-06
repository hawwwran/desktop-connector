"""Vault browser download helpers (T5.3/T5.4)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
import uuid

log = logging.getLogger(__name__)
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Protocol


# F-D11: §6.9 retry budget for transient ``vault_chunk_missing`` (404)
# from the relay. The relay can return 404 between PUT and replication
# completion; the spec promises auto-retry within the transfer budget
# before surfacing as terminal. 3 retries (4 attempts total) with
# exponential backoff capped at 60 s — matches the F-Y06 delete retry
# shape and stays well under the user-cancel patience window.
_CHUNK_MISSING_MAX_RETRIES = 3
_CHUNK_MISSING_BASE_BACKOFF_S = 1.0
_CHUNK_MISSING_CAP_BACKOFF_S = 60.0

# Test seam: replace this callable to skip real sleeps in tests. The
# helpers below call ``_chunk_missing_sleep(seconds)`` instead of
# ``time.sleep`` so a unit test can drop the wall-clock cost while
# still exercising the retry counter + log emission. Production code
# leaves the default in place.
_chunk_missing_sleep: Callable[[float], None] = time.sleep


def _missing_retry_delay_s(
    exc: "VaultChunkMissingError",
    attempt: int,
) -> float:
    """Pick a backoff duration: server hint wins, else exp backoff."""
    server_hint_ms = exc.details.get("retry_after_ms") if exc.details else None
    if isinstance(server_hint_ms, (int, float)) and server_hint_ms > 0:
        return min(
            float(server_hint_ms) / 1000.0, _CHUNK_MISSING_CAP_BACKOFF_S,
        )
    return min(
        _CHUNK_MISSING_BASE_BACKOFF_S * (2 ** attempt),
        _CHUNK_MISSING_CAP_BACKOFF_S,
    )

from .vault_binding_lifecycle import SyncCancelledError
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


# Single source of truth for the chunk-missing error lives in
# vault_relay_errors so the relay client and the download pipeline
# share one type. The local re-export keeps any existing imports
# in this module working.
from .vault_relay_errors import VaultChunkMissingError  # noqa: F401, E402


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


def _ensure_all_chunks_present(
    *,
    relay: ChunkRelay,
    vault_id: str,
    vault_access_secret: str,
    chunk_ids: list[str],
    should_continue: Callable[[], bool] | None = None,
) -> dict[str, dict[str, Any]]:
    """F-D11: re-poll the relay's batch HEAD until every chunk reports
    ``present=True`` or the §6.9 retry budget is exhausted.

    The relay returns 404-style "not present" via the head dict
    (``info["present"] == False``), not via an exception. We surface
    that case as :class:`VaultChunkMissingError` after the budget
    closes so callers see the same terminal type whether the miss
    came from a head ping or a bytes fetch.

    Empty ``chunk_ids`` short-circuits to ``{}`` so callers don't pay
    a network round-trip for zero-version downloads.
    """
    if not chunk_ids:
        return {}

    last_missing: list[str] = []
    last_exc: VaultChunkMissingError | None = None
    for attempt in range(_CHUNK_MISSING_MAX_RETRIES + 1):
        # Only cost a ``should_continue`` tick on retries — attempt 0 is
        # the fast path that callers expect to be free, and the
        # per-chunk loop downstream has its own cancellation check
        # before each fetch. Without this carve-out a caller's gate
        # like "True once, False after" would spend its single True on
        # the head call and bail before any chunk fetched, regressing
        # the F-U03 cancel-between-chunks contract.
        if (
            attempt > 0
            and should_continue is not None
            and not should_continue()
        ):
            raise SyncCancelledError(
                f"download cancelled at chunk-presence check ({attempt}/"
                f"{_CHUNK_MISSING_MAX_RETRIES} retries)",
            )
        heads = relay.batch_head_chunks(
            vault_id, vault_access_secret, chunk_ids,
        )
        last_missing = [
            cid for cid in chunk_ids
            if not isinstance(heads.get(cid), dict)
            or not heads.get(cid, {}).get("present")
        ]
        if not last_missing:
            return heads
        last_exc = VaultChunkMissingError(
            f"vault chunk missing: {last_missing[0]}"
            + (f" (+{len(last_missing) - 1} more)" if len(last_missing) > 1 else "")
        )
        if attempt == _CHUNK_MISSING_MAX_RETRIES:
            log.warning(
                "vault.download.chunk_missing_exhausted "
                "vault=%s missing_count=%d first_missing=%s "
                "attempts=%d",
                vault_id, len(last_missing), last_missing[0],
                _CHUNK_MISSING_MAX_RETRIES + 1,
            )
            raise last_exc
        delay = _missing_retry_delay_s(last_exc, attempt)
        log.info(
            "vault.download.chunk_missing_retry "
            "vault=%s missing_count=%d first_missing=%s "
            "attempt=%d/%d delay_s=%.1f",
            vault_id, len(last_missing), last_missing[0],
            attempt + 1, _CHUNK_MISSING_MAX_RETRIES, delay,
        )
        _chunk_missing_sleep(delay)
    if last_exc is not None:
        raise last_exc
    return heads


def _get_chunk_with_retry(
    *,
    relay: ChunkRelay,
    vault_id: str,
    vault_access_secret: str,
    chunk_id: str,
    should_continue: Callable[[], bool] | None = None,
) -> bytes:
    """F-D11: single-chunk GET with §6.9 retry on 404.

    The pre-flight ``_ensure_all_chunks_present`` already filters out
    chunks the relay knows are gone, but a chunk *can* be deleted
    between head-success and bytes-fetch (concurrent eviction,
    operator action). The same backoff budget covers that race.
    """
    last_exc: VaultChunkMissingError | None = None
    for attempt in range(_CHUNK_MISSING_MAX_RETRIES + 1):
        # Same first-attempt-is-free carve-out as
        # ``_ensure_all_chunks_present``. The download loop already
        # gates each chunk with ``should_continue`` before calling us;
        # checking again here would double-spend the caller's gate.
        if (
            attempt > 0
            and should_continue is not None
            and not should_continue()
        ):
            raise SyncCancelledError(
                f"download cancelled before chunk fetch (chunk={chunk_id})",
            )
        try:
            return relay.get_chunk(vault_id, vault_access_secret, chunk_id)
        except VaultChunkMissingError as exc:
            last_exc = exc
            if attempt == _CHUNK_MISSING_MAX_RETRIES:
                log.warning(
                    "vault.download.chunk_missing_exhausted "
                    "vault=%s chunk=%s attempts=%d",
                    vault_id, chunk_id,
                    _CHUNK_MISSING_MAX_RETRIES + 1,
                )
                raise
            delay = _missing_retry_delay_s(exc, attempt)
            log.info(
                "vault.download.chunk_missing_retry "
                "vault=%s chunk=%s attempt=%d/%d delay_s=%.1f",
                vault_id, chunk_id, attempt + 1,
                _CHUNK_MISSING_MAX_RETRIES, delay,
            )
            _chunk_missing_sleep(delay)
    # Loop only reaches here if it didn't return; re-raise last seen.
    if last_exc is not None:
        raise last_exc
    raise VaultChunkMissingError(f"vault chunk missing: {chunk_id}")


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
    should_continue: Callable[[], bool] | None = None,
) -> Path:
    """Download one file's latest non-deleted version to ``destination``.

    F-U03: ``should_continue`` is checked between every chunk fetch
    and once before the final atomic write. When it returns ``False``,
    the function raises :class:`vault_binding_lifecycle.SyncCancelledError`.
    Already-fetched plaintext is discarded (the destination's
    ``.dc-temp-…`` is cleaned up by the atomic-write guard); idempotent
    chunk dedup means a future restart pays the cache-hit price, not
    a re-fetch.
    """
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
    heads = _ensure_all_chunks_present(
        relay=relay,
        vault_id=vault.vault_id,
        vault_access_secret=vault.vault_access_secret,
        chunk_ids=chunk_ids,
        should_continue=should_continue,
    )

    plaintext_parts: list[bytes] = []
    completed = 0
    bytes_written = 0
    for chunk in chunks:
        # F-U03: bail before each chunk fetch so a Cancel button click
        # lands within ~1 chunk worth of network + decrypt work.
        if should_continue is not None and not should_continue():
            log.info(
                "vault.download.cancelled vault=%s path=%s chunks_done=%d total=%d",
                vault.vault_id, path, completed, len(chunks),
            )
            raise SyncCancelledError(
                f"download cancelled at chunk {completed}/{len(chunks)} of {path}"
            )
        encrypted = _load_cached_chunk(
            chunk_cache_dir=chunk_cache_dir,
            vault_id=vault.vault_id,
            chunk_id=chunk["chunk_id"],
            head=heads[chunk["chunk_id"]],
        )
        if encrypted is None:
            encrypted = _get_chunk_with_retry(
                relay=relay,
                vault_id=vault.vault_id,
                vault_access_secret=vault.vault_access_secret,
                chunk_id=chunk["chunk_id"],
                should_continue=should_continue,
            )

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

    if should_continue is not None and not should_continue():
        log.info(
            "vault.download.cancelled_pre_write vault=%s path=%s",
            vault.vault_id, path,
        )
        raise SyncCancelledError(f"download cancelled before write of {path}")

    data = b"".join(plaintext_parts)
    expected_size = _int_value(version.get("logical_size"))
    if expected_size and len(data) != expected_size:
        raise ValueError(f"downloaded size mismatch: expected {expected_size}, got {len(data)}")

    atomic_write_file(final_path, data)
    _report(progress, "done", len(chunks), len(chunks), len(data))
    return final_path


def download_version(
    *,
    vault: DownloadVault,
    relay: ChunkRelay,
    manifest: dict[str, Any],
    path: str,
    version_id: str,
    destination: Path,
    existing_policy: ExistingFilePolicy = "fail",
    chunk_cache_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> Path:
    """Download a specific historical version to a side path.

    Per A20, downloading a previous version must never overwrite the
    file's current/latest bytes. Callers compose ``destination`` from
    :func:`previous_version_filename` so the leaf name carries a
    version timestamp and cannot collide with the latest file's name.
    The ``existing_policy`` is honoured if even that side-path already
    exists (e.g. the same version was downloaded twice).

    F-U03: ``should_continue`` is checked between every chunk fetch
    (same contract as :func:`download_latest_file`).
    """
    if vault.master_key is None or vault.vault_access_secret is None:
        raise ValueError("vault is closed")
    if not version_id:
        raise ValueError("version_id is required")

    normalized = normalize_manifest_plaintext(manifest)
    folder = _folder_for_display_path(normalized, path)
    entry = get_file(normalized, path, include_deleted=True)

    version = _find_version(entry, version_id)
    if version is None:
        raise KeyError(f"version not found: {version_id}")

    chunks = _version_chunks(version)
    final_path = resolve_download_destination(Path(destination), existing_policy)
    _preflight_disk_space(final_path, _int_value(version.get("logical_size")))

    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    _report(progress, "checking", 0, len(chunks))
    heads = _ensure_all_chunks_present(
        relay=relay,
        vault_id=vault.vault_id,
        vault_access_secret=vault.vault_access_secret,
        chunk_ids=chunk_ids,
        should_continue=should_continue,
    )

    plaintext_parts: list[bytes] = []
    completed = 0
    bytes_written = 0
    for chunk in chunks:
        if should_continue is not None and not should_continue():
            log.info(
                "vault.download.cancelled vault=%s path=%s version=%s chunks_done=%d total=%d",
                vault.vault_id, path, version_id, completed, len(chunks),
            )
            raise SyncCancelledError(
                f"version download cancelled at chunk {completed}/{len(chunks)} of {path}"
            )
        encrypted = _load_cached_chunk(
            chunk_cache_dir=chunk_cache_dir,
            vault_id=vault.vault_id,
            chunk_id=chunk["chunk_id"],
            head=heads[chunk["chunk_id"]],
        )
        if encrypted is None:
            encrypted = _get_chunk_with_retry(
                relay=relay,
                vault_id=vault.vault_id,
                vault_access_secret=vault.vault_access_secret,
                chunk_id=chunk["chunk_id"],
                should_continue=should_continue,
            )

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

    if should_continue is not None and not should_continue():
        log.info(
            "vault.download.cancelled_pre_write vault=%s path=%s version=%s",
            vault.vault_id, path, version_id,
        )
        raise SyncCancelledError(
            f"version download cancelled before write of {path}"
        )

    data = b"".join(plaintext_parts)
    expected_size = _int_value(version.get("logical_size"))
    if expected_size and len(data) != expected_size:
        raise ValueError(
            f"downloaded size mismatch: expected {expected_size}, got {len(data)}"
        )

    atomic_write_file(final_path, data)
    _report(progress, "done", len(chunks), len(chunks), len(data))
    return final_path


def previous_version_filename(name: str, version: dict[str, Any]) -> str:
    """Return the A20-style side-path filename for a historical version.

    Pattern: ``<stem> (version <YYYY-MM-DD HH-MM>).<ext>``. Falls back to
    ``(version <version_id_prefix>)`` when the manifest lacks a usable
    timestamp so the leaf name is still unique against the current file.
    """
    base = Path(str(name)).name or "version"
    suffix = Path(base).suffix
    stem = base[: -len(suffix)] if suffix else base
    tag = _version_tag(version)
    return f"{stem} (version {tag}){suffix}"


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
    heads = _ensure_all_chunks_present(
        relay=relay,
        vault_id=vault.vault_id,
        vault_access_secret=vault.vault_access_secret,
        chunk_ids=chunk_ids,
    )

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
                    encrypted = _get_chunk_with_retry(
                        relay=relay,
                        vault_id=vault.vault_id,
                        vault_access_secret=vault.vault_access_secret,
                        chunk_id=chunk["chunk_id"],
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


from .vault_atomic import (
    LOCAL_DISK_OVERHEAD_FACTOR,
    atomic_write_chunks as _atomic_write_chunks,
    atomic_write_file as _atomic_write_file,
    fsync_dir as _fsync_dir_helper,
)


def atomic_write_file(destination: Path, data: bytes) -> None:
    """Re-export from :mod:`vault_atomic` for back-compat callers."""
    _atomic_write_file(destination, data)


def atomic_write_chunks(destination: Path, chunks: Iterable[bytes]) -> int:
    """Re-export from :mod:`vault_atomic` for back-compat callers."""
    return _atomic_write_chunks(destination, chunks)


def vault_chunk_cache_path(cache_dir: Path, vault_id: str, chunk_id: str) -> Path:
    canonical = normalize_vault_id(vault_id)
    return Path(cache_dir) / "chunks" / canonical / chunk_id[6:8] / chunk_id


def default_vault_download_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "desktop-connector" / "vault"


# F-D04: per-vault chunk cache cap. Without a bound the cache grew
# until the user's XDG_CACHE_HOME ran out of space — restoring 100 GiB
# once left 100 GiB cached forever. 1 GiB ≈ 512 × 2 MiB chunks at the
# canonical chunk size; the cache stays useful for repeat downloads
# of the recently-touched files (matches Linux page-cache reuse
# semantics) without unbounded growth.
DEFAULT_VAULT_CHUNK_CACHE_MAX_BYTES = 1 * 1024 * 1024 * 1024


def prune_vault_chunk_cache(
    cache_dir: Path,
    vault_id: str,
    *,
    max_bytes: int = DEFAULT_VAULT_CHUNK_CACHE_MAX_BYTES,
) -> int:
    """F-D04: cap the per-vault chunk cache at ``max_bytes``.

    Walks ``<cache_dir>/chunks/<vault_id>/`` once, summing file sizes.
    If under the cap it returns 0 immediately. Otherwise sorts by
    ``st_atime`` ascending (oldest-touched first) and deletes until
    the total drops below the cap. Per-file failures (permission,
    file vanished mid-walk) are logged at debug and skipped — never
    fatal so a partial prune still helps. Returns bytes freed.

    The caller doesn't have to be the download path; tray /
    eviction / disconnect can all invoke this helper to keep the
    cache bounded. ``_store_cached_chunk`` calls it opportunistically
    after every write so the cap is enforced lazily without an
    explicit periodic job.
    """
    canonical = normalize_vault_id(vault_id) if vault_id else vault_id
    root = Path(cache_dir) / "chunks" / canonical
    if not root.exists():
        return 0
    entries: list[tuple[float, Path, int]] = []
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        entries.append((st.st_atime, path, st.st_size))
        total += st.st_size
    if total <= max_bytes:
        return 0
    entries.sort(key=lambda t: t[0])
    freed = 0
    for _atime, path, size in entries:
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= size
            freed += size
        except OSError:
            continue
    log.info(
        "vault.download.chunk_cache_pruned "
        "vault=%s freed_bytes=%d remaining_bytes=%d max_bytes=%d",
        vault_id, freed, total, max_bytes,
    )
    return freed


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
    # F-D10: if the relay's batch HEAD didn't supply *either* a size
    # or a hash there is nothing for us to validate the cached bytes
    # against. AEAD catches ciphertext bit-flips at decrypt time, but
    # a local attacker who swaps the cache file with bytes of a
    # different size would otherwise sail past the size check too —
    # forcing a fresh fetch closes that defense-in-depth gap. Servers
    # always emit at least ``size`` for present chunks, so this branch
    # only fires on an unusual relay bug or a feature-flag downgrade.
    if not expected_size and not expected_hash:
        log.info(
            "vault.download.cache_validation_unavailable "
            "vault=%s chunk=%s",
            vault_id, chunk_id,
        )
        return None
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
    # F-D04: opportunistic prune so the cap is enforced without a
    # periodic job. The fast path is a single ``rglob`` + size sum;
    # only when the per-vault subtree exceeds the cap do we sort
    # by atime and delete oldest. At the 1 GiB default + 2 MiB chunks
    # the prune touches ~512 stats per call — well under 10 ms on
    # mid-tier disks. Failures are swallowed: never let cache
    # bookkeeping break a download.
    try:
        prune_vault_chunk_cache(chunk_cache_dir, vault_id)
    except Exception:  # noqa: BLE001
        log.warning(
            "vault.download.chunk_cache_prune_failed vault=%s",
            vault_id, exc_info=True,
        )


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
        # F-D09 / F-D29: a single corrupt path must NOT abort the whole
        # batch. Skip the entry with a warning so the rest of the folder
        # still downloads.
        try:
            entry_parts = _safe_manifest_path_parts(str(entry.get("path", "")))
        except ValueError as exc:
            log.warning(
                "vault.download.skip_unsafe_path path=%s error=%s",
                str(entry.get("path", ""))[:200], exc,
            )
            continue
        if len(entry_parts) < len(prefix) or tuple(entry_parts[:len(prefix)]) != prefix:
            continue
        relative_parts = tuple(entry_parts[len(prefix):])
        if not relative_parts:
            continue
        relative_path = Path(*relative_parts)
        if relative_path in seen_relative_paths:
            log.warning(
                "vault.download.duplicate_path path=%s",
                str(relative_path),
            )
            continue
        seen_relative_paths.add(relative_path)

        version = _latest_version(entry)
        if version is None:
            log.warning(
                "vault.download.entry_has_no_version path=%s",
                str(entry.get("path", "")),
            )
            continue
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


def _find_version(entry: dict[str, Any], version_id: str) -> dict[str, Any] | None:
    for version in entry.get("versions", []) or []:
        if not isinstance(version, dict):
            continue
        if str(version.get("version_id", "")) == version_id:
            return version
    return None


_VERSION_TAG_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[T ](?P<h>\d{2}):(?P<mi>\d{2})"
)


def _version_tag(version: dict[str, Any]) -> str:
    raw = str(
        version.get("modified_at") or version.get("created_at") or ""
    )
    match = _VERSION_TAG_RE.match(raw)
    if match:
        return (
            f"{match['y']}-{match['m']}-{match['d']} "
            f"{match['h']}-{match['mi']}"
        )
    version_id = str(version.get("version_id") or "")
    return version_id[:12] or "unknown"


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
    required = int(max(0, logical_size) * LOCAL_DISK_OVERHEAD_FACTOR)
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
    required = int(max(0, logical_size) * LOCAL_DISK_OVERHEAD_FACTOR)
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
    """Backwards-compat wrapper around :func:`vault_atomic.fsync_dir`."""
    _fsync_dir_helper(path)


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
