"""Destination resolution + atomic writes + disk-space preflight.

The download flows route every filesystem touch through this module:
``resolve_*_destination`` chooses the final path under the existing-file
policy, ``atomic_write_*`` are thin re-exports of :mod:`vault_atomic`
helpers preserved for back-compat, and ``_preflight_*`` enforces the
:data:`vault_atomic.LOCAL_DISK_OVERHEAD_FACTOR` headroom before any
network work begins.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from ..vault_atomic import (
    LOCAL_DISK_OVERHEAD_FACTOR,
    atomic_write_chunks as _atomic_write_chunks,
    atomic_write_file as _atomic_write_file,
    fsync_dir as _fsync_dir_helper,
)
from .types import (
    DownloadCancelled,
    ExistingDestinationError,
    ExistingFilePolicy,
    VaultLocalDiskFullError,
)


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
    """Re-export from :mod:`vault_atomic` for back-compat callers."""
    _atomic_write_file(destination, data)


def atomic_write_chunks(destination: Path, chunks: Iterable[bytes]) -> int:
    """Re-export from :mod:`vault_atomic` for back-compat callers."""
    return _atomic_write_chunks(destination, chunks)


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


def _fsync_dir(path: Path) -> None:
    """Backwards-compat wrapper around :func:`vault_atomic.fsync_dir`."""
    _fsync_dir_helper(path)
