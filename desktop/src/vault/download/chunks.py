"""Per-chunk fetch + decrypt machinery (F-D11 retry, AEAD).

Owns the §6.9 ``vault_chunk_missing`` retry budget for both the batch
HEAD probe and the single-chunk GET, plus the AEAD decrypt that
verifies size + tag against the manifest's chunk record. The
``_chunk_missing_sleep`` test seam stays module-level: tests must
``mock.patch("src.vault.download.chunks._chunk_missing_sleep", …)``
to drop wall-clock cost without disturbing the retry counter.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..binding.lifecycle import SyncCancelledError
from ..crypto import (
    aead_decrypt,
    build_chunk_aad,
    derive_subkey,
)
from ..relay_errors import VaultChunkMissingError
from .manifest import _int_value
from .types import ChunkRelay, DownloadVault


log = logging.getLogger(__name__)


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
    exc: VaultChunkMissingError,
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
