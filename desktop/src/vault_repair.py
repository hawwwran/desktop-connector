"""Vault repair helper (T17.4).

Two actions per spec:

- :func:`mark_broken_in_next_revision` produces a new manifest
  revision in which every entry referencing a broken chunk_id is
  surgically purged from the live tree. The op-log records the
  intent so the repair is auditable, and the (parent_revision,
  revision) chain stays linked. Versions on a corrupted entry are
  filtered: if a single chunk in a multi-chunk version is broken,
  that whole version is removed; if every version becomes empty the
  whole entry is tombstoned (deleted=True) — the bytes are gone but
  the path keeps history.

- :func:`plan_restore_from_export` composes a "restore *only* the
  broken items" plan for the T8 import flow. It returns the subset
  of remote_folder_id/path tuples whose ``broken_paths`` set asked
  for; T8 import (run by the caller) wraps this and lands the bytes
  back from the user-provided protected bundle.

This module is pure logic — no I/O. The CAS publish + activity-log
write live in the caller. Tests drive synthetic manifests + plans
without touching disk.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable


log = logging.getLogger(__name__)


@dataclass
class RepairPlan:
    """One row per (remote_folder_id, path) the repair will act on."""
    remote_folder_id: str
    path: str
    action: str  # 'purge_versions' | 'tombstone_entry' | 'restore_from_export'
    broken_chunk_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class RepairResult:
    manifest: dict[str, Any]
    plans: list[RepairPlan] = field(default_factory=list)


def mark_broken_in_next_revision(
    manifest: dict[str, Any],
    *,
    broken_chunk_ids: Iterable[str],
    author_device_id: str,
    repaired_at: str,
) -> RepairResult:
    """Build the post-repair manifest with every broken-chunk-bearing
    version stripped from the live tree.

    - A version whose ``chunks[]`` references *any* broken chunk_id is
      removed in its entirety. Reasoning: a partial chunk loss yields a
      corrupted plaintext, never a recoverable file.
    - If an entry's remaining versions list is empty, the entry is
      tombstoned (deleted=True) so the path keeps history but the
      live tree no longer points at unrecoverable bytes.
    - Otherwise, the entry's ``latest_version_id`` is reassigned to the
      newest surviving version (the last in the now-shorter list).
    """
    broken_set = set(broken_chunk_ids)
    head = copy.deepcopy(manifest)
    parent_revision = int(head.get("revision", 0))
    head["parent_revision"] = parent_revision
    head["revision"] = parent_revision + 1
    head["created_at"] = repaired_at
    head["author_device_id"] = author_device_id

    plans: list[RepairPlan] = []

    for folder in head.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        new_entries: list[dict[str, Any]] = []
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "file")) != "file":
                new_entries.append(entry)
                continue
            path = str(entry.get("path") or "")
            kept_versions: list[dict[str, Any]] = []
            removed_chunks: list[str] = []
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                chunks = [
                    c for c in version.get("chunks", []) or []
                    if isinstance(c, dict)
                ]
                hits = [
                    str(c.get("chunk_id", "")) for c in chunks
                    if str(c.get("chunk_id", "")) in broken_set
                ]
                if hits:
                    removed_chunks.extend(hits)
                    continue
                kept_versions.append(version)

            if not removed_chunks:
                new_entries.append(entry)
                continue

            if not kept_versions:
                # Entry's only versions all referenced broken chunks
                # → tombstone the entry per §17.4 "purges from live
                # tree, retains in op-log".
                tombstone = copy.deepcopy(entry)
                tombstone["versions"] = list(entry.get("versions", []) or [])
                tombstone["deleted"] = True
                tombstone["deleted_at"] = repaired_at
                tombstone["deleted_by_device_id"] = author_device_id
                tombstone["repair_reason"] = "broken_chunks"
                new_entries.append(tombstone)
                plans.append(RepairPlan(
                    remote_folder_id=folder_id, path=path,
                    action="tombstone_entry",
                    broken_chunk_ids=tuple(removed_chunks),
                    detail="every version referenced broken chunks",
                ))
            else:
                # Some versions survive — drop the broken ones, retain
                # the rest, refresh latest_version_id.
                purged = copy.deepcopy(entry)
                purged["versions"] = kept_versions
                purged["latest_version_id"] = str(
                    kept_versions[-1].get("version_id", ""),
                )
                new_entries.append(purged)
                plans.append(RepairPlan(
                    remote_folder_id=folder_id, path=path,
                    action="purge_versions",
                    broken_chunk_ids=tuple(removed_chunks),
                    detail=f"purged {len(removed_chunks)} broken-chunk version(s)",
                ))
        folder["entries"] = new_entries

    if plans:
        log.info(
            "vault.repair.marked_broken count=%d author=%s revision=%d",
            len(plans), author_device_id, head["revision"],
        )

    return RepairResult(manifest=head, plans=plans)


def plan_restore_from_export(
    *,
    broken_paths: Iterable[tuple[str, str]],
) -> list[RepairPlan]:
    """Compose a list of restore-from-export plans for the broken paths.

    The caller passes ``(remote_folder_id, path)`` tuples — usually the
    paths the integrity check flagged. Each becomes a RepairPlan with
    ``action='restore_from_export'``; the actual byte-importing happens
    in the T8 import flow, which the caller drives with these plans
    pointing at a protected bundle the user supplies.
    """
    out: list[RepairPlan] = []
    for folder_id, path in broken_paths:
        out.append(RepairPlan(
            remote_folder_id=str(folder_id),
            path=str(path),
            action="restore_from_export",
            detail="awaiting bytes from a protected bundle",
        ))
    return out


__all__ = [
    "RepairPlan",
    "RepairResult",
    "mark_broken_in_next_revision",
    "plan_restore_from_export",
]
