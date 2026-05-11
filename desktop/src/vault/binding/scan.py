"""Filesystem catch-up scan for Sync now.

The OS-level watcher (``vault_filesystem_watcher``) lives in the tray
process. The Vault Settings window runs as a subprocess and creates new
bindings without IPC'ing the tray, so a freshly-bound folder is briefly
unwatched until the tray's next ``_ensure_vault_watcher_runtime`` pass.
A daemon restart leaves the same gap — anything that lands on disk
while no watcher is up never reaches the pending-ops queue.

This module's :func:`scan_for_local_changes` walks the binding root and
enqueues upload/delete ops for drift between disk and the
``vault_local_entries`` cache. Called at the top of
``flush_and_sync_binding`` so "Sync now" actually picks up changes the
watcher missed, instead of returning "nothing to do".

The walk is path-stable: it uses the same coalesce key the watcher
uses, so a still-pending watcher enqueue and a scan-side enqueue for
the same path collapse into one row.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable

from .bindings import VaultBinding, VaultBindingsStore, normalize_relative_path


log = logging.getLogger(__name__)


def scan_for_local_changes(
    *,
    store: VaultBindingsStore,
    binding: VaultBinding,
    ignore_dotfiles: bool = True,
) -> int:
    """Walk the binding's local root and enqueue ops for disk/cache drift.

    Returns the number of ops enqueued. ``0`` means cache is in sync
    with disk (and the queue's existing rows already cover any other
    pending work).
    """
    local_root = Path(binding.local_path)
    if not local_root.is_dir():
        log.warning(
            "vault.sync.scan_skip_missing_root binding=%s path=%s",
            binding.binding_id, local_root,
        )
        return 0

    now_int = int(time.time())
    seen_paths: set[str] = set()
    enqueued = 0

    for absolute in _walk_local(local_root, ignore_dotfiles=ignore_dotfiles):
        try:
            stat = absolute.stat()
        except OSError:
            continue
        if not _is_regular_file(stat):
            continue
        try:
            relative = normalize_relative_path(
                absolute.relative_to(local_root).as_posix()
            )
        except ValueError:
            continue
        seen_paths.add(relative)
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)

        try:
            entry = store.get_local_entry(binding.binding_id, relative)
        except Exception:  # noqa: BLE001
            log.exception(
                "vault.sync.scan_lookup_failed binding=%s path=%s",
                binding.binding_id, relative,
            )
            entry = None

        needs_upload = (
            entry is None
            or not entry.content_fingerprint
            or entry.size_bytes != size
            or entry.mtime_ns != mtime_ns
        )
        if needs_upload:
            store.coalesce_op(
                binding_id=binding.binding_id,
                op_type="upload",
                relative_path=relative,
                now=now_int,
            )
            enqueued += 1

    # Tombstone path: cached entries with a content_fingerprint (i.e.
    # files we have actually synced before) that are no longer on disk.
    # ``extra`` rows (fingerprint == "") that vanish are silent — they
    # were never replicated upstream, so there's nothing to tombstone.
    try:
        cached_entries = store.list_local_entries(binding.binding_id)
    except Exception:  # noqa: BLE001
        log.exception(
            "vault.sync.scan_list_entries_failed binding=%s",
            binding.binding_id,
        )
        cached_entries = []
    for entry in cached_entries:
        if entry.relative_path in seen_paths:
            continue
        if not entry.content_fingerprint:
            continue
        store.coalesce_op(
            binding_id=binding.binding_id,
            op_type="delete",
            relative_path=entry.relative_path,
            now=now_int,
        )
        enqueued += 1

    if enqueued:
        log.info(
            "vault.sync.scan_enqueued binding=%s count=%d",
            binding.binding_id, enqueued,
        )
    return enqueued


def _walk_local(root: Path, *, ignore_dotfiles: bool) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        if ignore_dotfiles:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if ignore_dotfiles and name.startswith("."):
                continue
            yield Path(dirpath) / name


def _is_regular_file(stat_result: os.stat_result) -> bool:
    import stat as _stat

    return _stat.S_ISREG(stat_result.st_mode)


__all__ = ["scan_for_local_changes"]
