"""Manifest-walking helpers (folders, versions, chunks, paths).

Pure functions over the decrypted manifest plaintext: locate the remote
folder for a display path, build :class:`_FolderFilePlan`s for a folder
download, pick the latest / specific version, normalize paths, and
format the historical-version side-path tag. Imports from this module
must stay manifest-shape-only — no relay, vault, or filesystem
dependencies.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable

from .types import _FolderFilePlan


log = logging.getLogger(__name__)


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


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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
