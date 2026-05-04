"""Vault import: bundle preview + §D9 merge (T8.3, T8.4).

Pipeline (UI orchestration lives in T8.5):

    read_export_bundle   →  decrypt the bundle's manifest envelope
            ↓
    decide_import_action →  new_vault | merge | refuse
            ↓
    preview_import       →  the §gaps §17 8-field summary the wizard shows
            ↓
    find_conflict_batches→  per-remote-folder lists of colliding paths (§A4)
            ↓
    merge_import_into    →  builds the merged manifest with the user's
                            chosen mode applied per-folder
            ↓
    upload_missing_chunks + CAS-publish (T8.5)

Everything in this module is pure-data — no relay calls, no GTK. The
chunk upload + manifest publish step lives in the wizard so it can
sequence progress reporting and CAS retry against live relay state.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from .vault_manifest import (
    add_or_append_file_version,
    find_file_entry,
    generate_file_entry_id,
    normalize_manifest_path,
    normalize_manifest_plaintext,
)


ImportAction = Literal["new_vault", "merge", "refuse"]
ConflictMode = Literal["overwrite", "skip", "rename"]
DEFAULT_CONFLICT_MODE: ConflictMode = "rename"


@dataclass(frozen=True)
class FolderSummary:
    remote_folder_id: str
    display_name: str
    current_file_count: int
    deleted_file_count: int
    logical_size: int
    ciphertext_size: int


@dataclass(frozen=True)
class ImportPreview:
    """Fields backing the §gaps §17 import preview dialog."""

    bundle_vault_id: str
    bundle_genesis_fingerprint: str   # short hex (12 chars) when known
    fingerprint_status: Literal["new_vault", "matches_active", "different_vault"]
    source_label: str
    bundle_logical_size: int
    bundle_ciphertext_size: int
    folders: list[FolderSummary]
    current_files: int
    total_versions: int
    tombstones: int
    conflicts: int                  # only meaningful for the merge path
    will_change_head: bool
    chunks_total: int
    chunks_already_on_relay: int


@dataclass(frozen=True)
class FolderConflictBatch:
    """Per-remote-folder list of paths that collide with the active vault."""

    remote_folder_id: str
    display_name: str
    conflicting_paths: list[str]


@dataclass(frozen=True)
class ImportMergeResolution:
    """User's per-folder choice from the conflict prompt(s)."""

    per_folder: dict[str, ConflictMode]
    default_for_remaining: ConflictMode | None = None

    def resolve(self, remote_folder_id: str) -> ConflictMode:
        if remote_folder_id in self.per_folder:
            return self.per_folder[remote_folder_id]
        if self.default_for_remaining is not None:
            return self.default_for_remaining
        return DEFAULT_CONFLICT_MODE


@dataclass(frozen=True)
class ImportMergeResult:
    """Outcome of ``merge_import_into``."""

    manifest: dict[str, Any]
    overwritten_paths: list[str]
    skipped_paths: list[str]
    renamed_paths: list[tuple[str, str]]   # (original, renamed)
    new_paths: list[str]
    chunk_ids_referenced: list[str]


# ---------------------------------------------------------------------------
# T8.3 — decision + preview
# ---------------------------------------------------------------------------


def decide_import_action(
    *,
    active_manifest: dict[str, Any] | None,
    active_genesis_fingerprint: str | None,
    bundle_vault_id: str,
    bundle_genesis_fingerprint: str | None,
) -> ImportAction:
    """§D9: identity gate before the merge UX kicks in.

    - **new_vault** when the device has no active vault.
    - **merge** when the device's active vault matches the bundle by
      vault_id *and* genesis fingerprint (when both sides know it).
    - **refuse** when the active vault has a *different* identity —
      §D9 explicitly forbids silent overwrite, the wizard surfaces a
      "this is a different vault" prompt and the import is gated on
      switching vaults first.
    """
    if active_manifest is None:
        return "new_vault"
    active_vault_id = str(active_manifest.get("vault_id", "")).strip()
    if not active_vault_id:
        return "new_vault"
    if active_vault_id != bundle_vault_id:
        return "refuse"
    # Same vault_id is necessary but not sufficient — genesis fingerprint
    # is the cryptographic anchor. If both sides report a fingerprint,
    # they must agree.
    if active_genesis_fingerprint and bundle_genesis_fingerprint:
        if active_genesis_fingerprint != bundle_genesis_fingerprint:
            return "refuse"
    return "merge"


def preview_import(
    *,
    bundle_manifest: dict[str, Any],
    bundle_vault_id: str,
    active_manifest: dict[str, Any] | None,
    source_label: str,
    chunks_already_on_relay: int,
    bundle_genesis_fingerprint: str | None = None,
    active_genesis_fingerprint: str | None = None,
) -> ImportPreview:
    """Compute the 8 §gaps §17 fields without talking to the relay."""
    folders, totals = _summarize_folders(bundle_manifest)
    chunks_total = _count_unique_chunks(bundle_manifest)
    fingerprint_status: Literal[
        "new_vault", "matches_active", "different_vault",
    ]
    decision = decide_import_action(
        active_manifest=active_manifest,
        active_genesis_fingerprint=active_genesis_fingerprint,
        bundle_vault_id=bundle_vault_id,
        bundle_genesis_fingerprint=bundle_genesis_fingerprint,
    )
    if decision == "new_vault":
        fingerprint_status = "new_vault"
    elif decision == "merge":
        fingerprint_status = "matches_active"
    else:
        fingerprint_status = "different_vault"

    if active_manifest is not None and decision == "merge":
        conflicts = sum(
            len(batch.conflicting_paths)
            for batch in find_conflict_batches(
                active_manifest=active_manifest,
                bundle_manifest=bundle_manifest,
            )
        )
        will_change_head = _bundle_overrides_head(
            active_manifest=active_manifest, bundle_manifest=bundle_manifest,
        )
    else:
        conflicts = 0
        will_change_head = bool(totals["current_files"])

    short_fp = (bundle_genesis_fingerprint or "")[:12]

    return ImportPreview(
        bundle_vault_id=bundle_vault_id,
        bundle_genesis_fingerprint=short_fp,
        fingerprint_status=fingerprint_status,
        source_label=source_label,
        bundle_logical_size=totals["logical_size"],
        bundle_ciphertext_size=totals["ciphertext_size"],
        folders=folders,
        current_files=totals["current_files"],
        total_versions=totals["versions"],
        tombstones=totals["tombstones"],
        conflicts=conflicts,
        will_change_head=will_change_head,
        chunks_total=chunks_total,
        chunks_already_on_relay=int(chunks_already_on_relay),
    )


# ---------------------------------------------------------------------------
# T8.4 — §D9 merge
# ---------------------------------------------------------------------------


def find_conflict_batches(
    *,
    active_manifest: dict[str, Any],
    bundle_manifest: dict[str, Any],
) -> list[FolderConflictBatch]:
    """Per-folder conflict batches per §A4.

    Definition of conflict (§D9): same logical path **with a current
    (non-tombstoned) version on both sides**. Tombstone-only sides
    don't trigger the conflict prompt — they merge into history
    automatically.
    """
    active = normalize_manifest_plaintext(active_manifest)
    bundle = normalize_manifest_plaintext(bundle_manifest)

    active_paths_by_folder: dict[str, set[str]] = {}
    for folder in active.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        rid = str(folder.get("remote_folder_id", ""))
        active_paths_by_folder[rid] = {
            unicodedata.normalize("NFC", str(e.get("path", "")))
            for e in folder.get("entries", []) or []
            if isinstance(e, dict)
            and str(e.get("type", "file")) == "file"
            and not bool(e.get("deleted"))
        }

    batches: list[FolderConflictBatch] = []
    for folder in bundle.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        rid = str(folder.get("remote_folder_id", ""))
        active_paths = active_paths_by_folder.get(rid, set())
        if not active_paths:
            continue  # folder doesn't exist on active side → no conflicts
        conflicting: list[str] = []
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "file")) != "file":
                continue
            if bool(entry.get("deleted")):
                continue
            path = unicodedata.normalize("NFC", str(entry.get("path", "")))
            if path in active_paths:
                conflicting.append(path)
        if conflicting:
            batches.append(FolderConflictBatch(
                remote_folder_id=rid,
                display_name=str(folder.get("display_name_enc", "")),
                conflicting_paths=sorted(conflicting),
            ))
    return batches


def merge_import_into(
    *,
    active_manifest: dict[str, Any],
    bundle_manifest: dict[str, Any],
    resolution: ImportMergeResolution,
    author_device_id: str,
    now: str | None = None,
) -> ImportMergeResult:
    """Apply each bundle entry to ``active_manifest`` per the chosen modes.

    Walks the bundle folder by folder; for each entry:

    - **No active entry at this path**: append as a brand-new file.
    - **Active entry, no conflict (just history merge)**: append the
      bundle's versions to the active entry's history, leaving the
      active ``latest_version_id`` alone (tombstones merge here too —
      tombstone wins, bundle history preserved as restorable per §D5).
    - **Conflict** (active has live version, bundle has live version):
      pick mode from ``resolution``:
        - ``overwrite``: bundle version becomes the new latest, active
          history retained.
        - ``skip``: active stays current, bundle versions archived as
          history.
        - ``rename``: bundle entry lands at ``<path> (conflict imported
          <YYYY-MM-DD HH-MM>)<ext>`` as a fresh entry (§A20).
    """
    timestamp = str(now or _now_rfc3339())
    out = normalize_manifest_plaintext(active_manifest)
    bundle = normalize_manifest_plaintext(bundle_manifest)

    overwritten: list[str] = []
    skipped: list[str] = []
    renamed: list[tuple[str, str]] = []
    new_paths: list[str] = []

    for bundle_folder in bundle.get("remote_folders", []) or []:
        if not isinstance(bundle_folder, dict):
            continue
        rid = str(bundle_folder.get("remote_folder_id", ""))
        if not rid:
            continue
        active_folder = _find_or_clone_folder(out, bundle_folder)
        active_paths = {
            unicodedata.normalize("NFC", str(e.get("path", ""))): e
            for e in active_folder.get("entries", []) or []
            if isinstance(e, dict)
            and str(e.get("type", "file")) == "file"
        }
        mode = resolution.resolve(rid)

        for bundle_entry in bundle_folder.get("entries", []) or []:
            if not isinstance(bundle_entry, dict):
                continue
            if str(bundle_entry.get("type", "file")) != "file":
                continue
            bundle_path = unicodedata.normalize("NFC", str(bundle_entry.get("path", "")))
            active_entry = active_paths.get(bundle_path)

            if active_entry is None:
                # No active entry → straight import.
                new_entry = copy.deepcopy(bundle_entry)
                active_folder.setdefault("entries", []).append(new_entry)
                active_paths[bundle_path] = new_entry
                new_paths.append(_full_path(bundle_folder, bundle_path))
                continue

            active_live = not bool(active_entry.get("deleted"))
            bundle_live = not bool(bundle_entry.get("deleted"))

            if not active_live or not bundle_live:
                # No live-vs-live conflict — append bundle versions as
                # restorable history; tombstone state stays whatever
                # the active side decided.
                _merge_versions_into(active_entry, bundle_entry, keep_active_latest=True)
                continue

            if mode == "overwrite":
                _merge_versions_into(
                    active_entry, bundle_entry, keep_active_latest=False,
                )
                overwritten.append(_full_path(bundle_folder, bundle_path))
            elif mode == "skip":
                _merge_versions_into(
                    active_entry, bundle_entry, keep_active_latest=True,
                )
                skipped.append(_full_path(bundle_folder, bundle_path))
            else:  # rename
                renamed_path = _conflict_imported_path(bundle_path, timestamp)
                while renamed_path in active_paths:
                    renamed_path = _conflict_imported_path(renamed_path, timestamp)
                new_entry = copy.deepcopy(bundle_entry)
                new_entry["path"] = renamed_path
                # Fresh entry_id so the renamed copy has its own identity.
                new_entry["entry_id"] = generate_file_entry_id()
                active_folder.setdefault("entries", []).append(new_entry)
                active_paths[renamed_path] = new_entry
                renamed.append(
                    (
                        _full_path(bundle_folder, bundle_path),
                        _full_path(bundle_folder, renamed_path),
                    )
                )

    parent_revision = int(out.get("revision", 0))
    out["parent_revision"] = parent_revision
    out["revision"] = parent_revision + 1
    out["created_at"] = timestamp
    out["author_device_id"] = str(author_device_id)

    chunk_ids = sorted(_collect_chunk_ids(out))
    return ImportMergeResult(
        manifest=out,
        overwritten_paths=sorted(overwritten),
        skipped_paths=sorted(skipped),
        renamed_paths=sorted(renamed),
        new_paths=sorted(new_paths),
        chunk_ids_referenced=chunk_ids,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize_folders(manifest: dict[str, Any]) -> tuple[list[FolderSummary], dict[str, int]]:
    folders: list[FolderSummary] = []
    totals = {
        "logical_size": 0,
        "ciphertext_size": 0,
        "current_files": 0,
        "versions": 0,
        "tombstones": 0,
    }
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        current_count = 0
        deleted_count = 0
        folder_logical = 0
        folder_ciphertext = 0
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "file")) != "file":
                continue
            versions = [
                v for v in entry.get("versions", []) or []
                if isinstance(v, dict)
            ]
            totals["versions"] += len(versions)
            if bool(entry.get("deleted")):
                deleted_count += 1
                totals["tombstones"] += 1
            else:
                current_count += 1
                totals["current_files"] += 1
            latest_id = str(entry.get("latest_version_id") or "")
            latest = next(
                (v for v in versions if str(v.get("version_id", "")) == latest_id),
                versions[-1] if versions else None,
            )
            if latest is not None:
                folder_logical += int(latest.get("logical_size", 0) or 0)
                folder_ciphertext += int(latest.get("ciphertext_size", 0) or 0)
        folders.append(FolderSummary(
            remote_folder_id=str(folder.get("remote_folder_id", "")),
            display_name=str(folder.get("display_name_enc", "")),
            current_file_count=current_count,
            deleted_file_count=deleted_count,
            logical_size=folder_logical,
            ciphertext_size=folder_ciphertext,
        ))
        totals["logical_size"] += folder_logical
        totals["ciphertext_size"] += folder_ciphertext
    return folders, totals


def _count_unique_chunks(manifest: dict[str, Any]) -> int:
    return len(_collect_chunk_ids(manifest))


def _collect_chunk_ids(manifest: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = str(chunk.get("chunk_id") or "")
                    if cid:
                        seen.add(cid)
    return seen


def _bundle_overrides_head(
    *,
    active_manifest: dict[str, Any],
    bundle_manifest: dict[str, Any],
) -> bool:
    """True iff merging the bundle would change at least one file's
    visible 'current' version (i.e. some bundle entry has a live
    version at a path the active vault doesn't, or the live versions
    differ)."""
    active_live: dict[tuple[str, str], str] = {}
    for folder in active_manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        rid = str(folder.get("remote_folder_id", ""))
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or bool(entry.get("deleted")):
                continue
            path = unicodedata.normalize("NFC", str(entry.get("path", "")))
            active_live[(rid, path)] = str(entry.get("latest_version_id") or "")

    for folder in bundle_manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        rid = str(folder.get("remote_folder_id", ""))
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or bool(entry.get("deleted")):
                continue
            path = unicodedata.normalize("NFC", str(entry.get("path", "")))
            key = (rid, path)
            bundle_latest = str(entry.get("latest_version_id") or "")
            if key not in active_live:
                return True
            if active_live[key] != bundle_latest:
                return True
    return False


def _find_or_clone_folder(
    manifest: dict[str, Any],
    bundle_folder: dict[str, Any],
) -> dict[str, Any]:
    rid = str(bundle_folder.get("remote_folder_id", ""))
    for folder in manifest.get("remote_folders", []) or []:
        if isinstance(folder, dict) and str(folder.get("remote_folder_id", "")) == rid:
            return folder
    cloned = copy.deepcopy(bundle_folder)
    cloned.setdefault("entries", [])
    manifest.setdefault("remote_folders", []).append(cloned)
    return cloned


def _merge_versions_into(
    active_entry: dict[str, Any],
    bundle_entry: dict[str, Any],
    *,
    keep_active_latest: bool,
) -> None:
    """Append bundle versions into active entry without losing history.

    ``keep_active_latest=True`` preserves the active entry's
    ``latest_version_id`` (skip / tombstone wins paths). ``False``
    promotes the bundle's latest (overwrite path).
    """
    active_versions = [
        v for v in active_entry.get("versions", []) or []
        if isinstance(v, dict)
    ]
    seen_ids = {str(v.get("version_id", "")) for v in active_versions}
    for v in bundle_entry.get("versions", []) or []:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("version_id", ""))
        if not vid or vid in seen_ids:
            continue
        active_versions.append(copy.deepcopy(v))
        seen_ids.add(vid)
    active_entry["versions"] = active_versions
    if not keep_active_latest:
        bundle_latest = str(bundle_entry.get("latest_version_id") or "")
        if bundle_latest in seen_ids:
            active_entry["latest_version_id"] = bundle_latest
        active_entry["deleted"] = False
        active_entry.pop("deleted_at", None)
        active_entry.pop("deleted_by_device_id", None)
        active_entry.pop("recoverable_until", None)


def _conflict_imported_path(path: str, timestamp: str) -> str:
    """A20 "import" variant: ``<stem> (conflict imported <YYYY-MM-DD HH-MM>)<ext>``."""
    leaf = Path(path).name or "imported"
    parent = "/".join(p for p in path.split("/")[:-1] if p)
    suffix = Path(leaf).suffix
    stem = leaf[: -len(suffix)] if suffix else leaf
    short_ts = _short_timestamp(timestamp)
    new_leaf = f"{stem} (conflict imported {short_ts}){suffix}"
    return f"{parent}/{new_leaf}" if parent else new_leaf


def _short_timestamp(timestamp: str) -> str:
    try:
        normalized = (
            timestamp.replace("Z", "+00:00") if timestamp.endswith("Z") else timestamp
        )
        when = datetime.fromisoformat(normalized)
    except ValueError:
        return timestamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H-%M")


def _full_path(folder: dict[str, Any], path: str) -> str:
    folder_name = str(folder.get("display_name_enc", "")).strip()
    return f"{folder_name}/{path}" if folder_name else path


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "ConflictMode",
    "DEFAULT_CONFLICT_MODE",
    "FolderConflictBatch",
    "FolderSummary",
    "ImportAction",
    "ImportMergeResolution",
    "ImportMergeResult",
    "ImportPreview",
    "decide_import_action",
    "find_conflict_batches",
    "merge_import_into",
    "preview_import",
]
