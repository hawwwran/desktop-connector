"""Atomic-write primitives + temp-file GC (T11.1, §gaps §11).

The vault's local-loop touches the user's disk via three paths:

- **Baseline + restore + sync** download a remote file and want
  "either the old version or the new version" semantics — never a
  half-written file.
- **Manifest + grant + export** persist their own JSON files; same
  contract.
- **Chunk-cache** stores decrypted plaintext chunks during download
  for resumable extraction.

All three reuse the same temp-file pattern:

    write to <name>.dc-temp-<rand>  →  fsync  →  os.replace  →  fsync(parent)

A power loss between fsync and rename leaves the temp file behind. A
GC sweep at startup removes any ``*.dc-temp-*`` older than 24 hours
so an unbounded crash backlog can't pile up — the live download path
finishes within seconds, so anything lingering past a day is
guaranteed orphaned.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Iterable


log = logging.getLogger(__name__)


TEMP_SUFFIX = ".dc-temp-"
DEFAULT_MAX_AGE_S = 24 * 60 * 60  # §gaps §11

# `<original>.dc-temp-<lowercase hex>` — match conservatively so we
# only sweep files we actually wrote.
_TEMP_FILENAME_RE = re.compile(r"\.dc-temp-[0-9a-f]{1,64}$")


def atomic_write_file(destination: Path, data: bytes) -> int:
    """Power-loss-safe write of ``data`` to ``destination``.

    Returns the byte count actually written (always ``len(data)`` on
    success).
    """
    return atomic_write_chunks(destination, (data,))


def atomic_write_chunks(destination: Path, chunks: Iterable[bytes]) -> int:
    """Stream ``chunks`` into ``destination`` via a sibling temp file.

    Sequence:

    1. ``mkdir -p`` the parent directory.
    2. Open ``<dest>.dc-temp-<uuid>`` exclusively.
    3. Write each chunk, ``flush()`` + ``fsync()`` so the bytes hit disk.
    4. ``os.replace(tmp, dest)`` (atomic rename even if ``dest`` exists).
    5. ``fsync()`` the parent directory so the rename itself is durable.

    On any error the temp file is unlinked.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(
        f"{destination.name}{TEMP_SUFFIX}{uuid.uuid4().hex}"
    )
    written = 0
    try:
        with open(tmp, "wb") as fh:
            for chunk in chunks:
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, destination)
        fsync_dir(destination.parent)
        return written
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(
                "vault.atomic.temp_unlink_failed path=%s error=%s", tmp, exc,
            )


def fsync_dir(path: Path) -> None:
    """Best-effort directory fsync (no-op on systems that refuse it)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some filesystems (FAT, FUSE without sync support) refuse this.
        pass
    finally:
        os.close(fd)


def sweep_orphan_temp_files(
    root: Path,
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_S,
    now: float | None = None,
) -> list[Path]:
    """Remove ``*.dc-temp-<hex>`` files older than ``max_age_seconds``.

    Walks ``root`` recursively. Returns the list of paths actually
    removed. Errors on individual files are logged and skipped — a
    missing or unreadable orphan must never abort startup.
    """
    root = Path(root)
    if not root.exists():
        return []
    cutoff = (time.time() if now is None else float(now)) - float(max_age_seconds)
    removed: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not _TEMP_FILENAME_RE.search(name):
                continue
            path = Path(dirpath) / name
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                log.warning(
                    "vault.atomic.sweep_stat_failed path=%s error=%s",
                    path, exc,
                )
                continue
            if mtime > cutoff:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning(
                    "vault.atomic.sweep_unlink_failed path=%s error=%s",
                    path, exc,
                )
                continue
            removed.append(path)
    if removed:
        log.info(
            "vault.atomic.sweep_removed root=%s count=%d",
            root, len(removed),
        )
    return removed


__all__ = [
    "DEFAULT_MAX_AGE_S",
    "TEMP_SUFFIX",
    "atomic_write_chunks",
    "atomic_write_file",
    "fsync_dir",
    "sweep_orphan_temp_files",
]
