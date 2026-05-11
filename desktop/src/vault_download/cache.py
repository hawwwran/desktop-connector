"""Per-vault chunk cache layout, reads, writes, and pruning (F-D04/F-D10).

Cache files live at ``<cache_dir>/chunks/<vault_id>/<chunk_id[6:8]>/<chunk_id>``.
``_load_cached_chunk`` validates against the relay's batch HEAD reply
(size + hash) so a swapped local file is rejected; ``_store_cached_chunk``
writes atomically via :func:`paths.atomic_write_file` and triggers an
opportunistic :func:`prune_vault_chunk_cache`.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from ..vault.crypto import normalize_vault_id
from .manifest import _int_value
from .paths import atomic_write_file


log = logging.getLogger(__name__)


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
