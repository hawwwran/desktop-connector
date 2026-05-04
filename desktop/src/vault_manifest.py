"""Vault manifest plaintext helpers.

T4 starts making the encrypted manifest a product data model rather
than just an opaque T2 vector payload. This module owns the v1
plaintext shape before the bytes are canonical-JSON encoded and passed
to the manifest AEAD envelope.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from .vault_crypto import normalize_vault_id


MANIFEST_SCHEMA = "dc-vault-manifest-v1"
MANIFEST_FORMAT_VERSION = 1
DEFAULT_RETENTION_POLICY = {
    "keep_deleted_days": 30,
    "keep_versions": 10,
}

_REMOTE_FOLDER_ID_RE = re.compile(r"^rf_v1_[a-z2-7]{24}$")
_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"


def make_manifest(
    *,
    vault_id: str,
    revision: int,
    parent_revision: int,
    created_at: str,
    author_device_id: str,
    remote_folders: list[dict[str, Any]] | None = None,
    operation_log_tail: list[dict[str, Any]] | None = None,
    archived_op_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a normalized v1 manifest plaintext object."""
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "vault_id": normalize_vault_id(vault_id),
        "revision": int(revision),
        "parent_revision": int(parent_revision),
        "created_at": str(created_at),
        "author_device_id": str(author_device_id),
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "remote_folders": list(remote_folders or []),
        "operation_log_tail": list(operation_log_tail or []),
        "archived_op_segments": list(archived_op_segments or []),
    }
    return normalize_manifest_plaintext(manifest)


def make_remote_folder(
    *,
    remote_folder_id: str,
    display_name_enc: str,
    created_at: str,
    created_by_device_id: str,
    retention_policy: dict[str, int] | None = None,
    ignore_patterns: list[str] | None = None,
    state: str = "active",
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a normalized remote-folder manifest entry.

    ``display_name_enc`` is an opaque client string inside the encrypted
    manifest. T4.5 renames mutate only this field.
    """
    folder: dict[str, Any] = {
        "remote_folder_id": remote_folder_id,
        "display_name_enc": unicodedata.normalize("NFC", display_name_enc),
        "created_at": created_at,
        "created_by_device_id": created_by_device_id,
        "retention_policy": copy.deepcopy(retention_policy or DEFAULT_RETENTION_POLICY),
        "ignore_patterns": list(ignore_patterns or []),
        "state": state,
    }
    if entries is not None:
        folder["entries"] = list(entries)
    return normalize_remote_folder(folder)


def generate_remote_folder_id() -> str:
    """Generate ``rf_v1_<24 lowercase base32>`` remote folder ids."""
    raw = secrets.token_bytes(15)
    out = []
    bits = 0
    buf = 0
    for byte in raw:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32_LOWER[(buf >> bits) & 0x1f])
    return "rf_v1_" + "".join(out[:24])


def normalize_manifest_plaintext(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a v1-compatible manifest copy.

    Compatibility rule for early T2/T3 material: manifests without a
    ``remote_folders`` field decrypt as an empty remote-folder list.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest plaintext must be an object")

    out = copy.deepcopy(manifest)
    out.setdefault("schema", MANIFEST_SCHEMA)
    out.setdefault("manifest_format_version", MANIFEST_FORMAT_VERSION)
    out.setdefault("operation_log_tail", [])
    out.setdefault("archived_op_segments", [])

    folders = out.get("remote_folders", [])
    if folders is None:
        folders = []
    if not isinstance(folders, list):
        raise ValueError("manifest remote_folders must be a list")

    created_at = str(out.get("created_at", ""))
    author_device_id = str(out.get("author_device_id", ""))
    out["remote_folders"] = [
        normalize_remote_folder(
            folder,
            default_created_at=created_at,
            default_created_by_device_id=author_device_id,
        )
        for folder in folders
    ]
    return out


def normalize_remote_folder(
    folder: dict[str, Any],
    *,
    default_created_at: str | None = None,
    default_created_by_device_id: str | None = None,
) -> dict[str, Any]:
    """Normalize one remote-folder entry to the T4.1 field names."""
    if not isinstance(folder, dict):
        raise ValueError("remote folder must be an object")

    out = copy.deepcopy(folder)

    # Early vectors used name/retention. Convert them when encountered
    # so those decrypted manifests still become a T4.1 shape.
    if "display_name_enc" not in out and "name" in out:
        out["display_name_enc"] = str(out["name"])
    out.pop("name", None)

    if "retention_policy" not in out and "retention" in out:
        out["retention_policy"] = out["retention"]
    out.pop("retention", None)

    if "created_at" not in out and default_created_at is not None:
        out["created_at"] = default_created_at
    if "created_by_device_id" not in out and default_created_by_device_id is not None:
        out["created_by_device_id"] = default_created_by_device_id
    out.setdefault("retention_policy", copy.deepcopy(DEFAULT_RETENTION_POLICY))
    out.setdefault("ignore_patterns", [])
    out.setdefault("state", "active")

    _validate_remote_folder(out)
    out["display_name_enc"] = unicodedata.normalize("NFC", str(out["display_name_enc"]))
    out["created_at"] = str(out["created_at"])
    out["created_by_device_id"] = str(out["created_by_device_id"])
    out["retention_policy"] = _normalize_retention_policy(out["retention_policy"])
    out["ignore_patterns"] = _normalize_string_list(out["ignore_patterns"], "ignore_patterns")
    out["state"] = str(out["state"])
    return out


def add_remote_folder(manifest: dict[str, Any], folder: dict[str, Any]) -> dict[str, Any]:
    """Return a manifest copy with ``folder`` appended.

    The caller owns revision/parent_revision values before calling this
    helper; this function only mutates the plaintext folder set.
    """
    out = normalize_manifest_plaintext(manifest)
    normalized_folder = normalize_remote_folder(
        folder,
        default_created_at=str(out.get("created_at", "")),
        default_created_by_device_id=str(out.get("author_device_id", "")),
    )
    existing = {f["remote_folder_id"] for f in out["remote_folders"]}
    if normalized_folder["remote_folder_id"] in existing:
        raise ValueError(f"remote folder already exists: {normalized_folder['remote_folder_id']}")
    out["remote_folders"].append(normalized_folder)
    return out


def remove_remote_folder(manifest: dict[str, Any], remote_folder_id: str) -> dict[str, Any]:
    """Return a manifest copy with ``remote_folder_id`` removed."""
    out = normalize_manifest_plaintext(manifest)
    before = len(out["remote_folders"])
    out["remote_folders"] = [
        f for f in out["remote_folders"]
        if f.get("remote_folder_id") != remote_folder_id
    ]
    if len(out["remote_folders"]) == before:
        raise ValueError(f"remote folder not found: {remote_folder_id}")
    return out


def rename_remote_folder(
    manifest: dict[str, Any],
    remote_folder_id: str,
    new_display_name: str,
) -> dict[str, Any]:
    """Return a manifest copy with ``display_name_enc`` updated for one folder.

    Per §D6, rename touches **only** the encrypted display name; binding,
    retention, ignore patterns, state, and the per-device local-path map
    are unaffected.
    """
    out = normalize_manifest_plaintext(manifest)
    name = unicodedata.normalize("NFC", str(new_display_name)).strip()
    if not name:
        raise ValueError("folder name is required")
    for folder in out["remote_folders"]:
        if folder.get("remote_folder_id") == remote_folder_id:
            folder["display_name_enc"] = name
            return out
    raise ValueError(f"remote folder not found: {remote_folder_id}")


def canonical_manifest_json(manifest: dict[str, Any]) -> bytes:
    """Canonical JSON bytes for manifest AEAD plaintext."""
    normalized = normalize_manifest_plaintext(manifest)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")


_FILE_ENTRY_ID_RE = re.compile(r"^fe_v1_[a-z2-7]{24}$")
_FILE_VERSION_ID_RE = re.compile(r"^fv_v1_[a-z2-7]{24}$")


def generate_file_entry_id() -> str:
    """Generate ``fe_v1_<24 lowercase base32>`` per A19."""
    return "fe_v1_" + _random_base32_lower(24)


def generate_file_version_id() -> str:
    """Generate ``fv_v1_<24 lowercase base32>`` per A19."""
    return "fv_v1_" + _random_base32_lower(24)


def normalize_manifest_path(path: str) -> str:
    """Return a NFC-normalized, forward-slash, no-leading/trailing-slash path.

    Manifest entries use ``/``-separated paths relative to the remote
    folder root. Empty path components and ``..`` are rejected so a
    crafted manifest can't smuggle a traversal that lands a real
    download outside its target directory.
    """
    raw = unicodedata.normalize("NFC", str(path)).replace("\\", "/")
    parts = [p for p in raw.split("/") if p]
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError(f"unsafe manifest path: {path!r}")
    if not parts:
        raise ValueError("manifest path is empty")
    return "/".join(parts)


def find_file_entry(
    manifest: dict[str, Any],
    remote_folder_id: str,
    path: str,
) -> dict[str, Any] | None:
    """Return the file entry at ``path`` inside ``remote_folder_id``, or None.

    Match is on the normalized path. Deleted entries are returned too —
    callers wanting to ignore tombstones should check ``entry["deleted"]``
    themselves; T7 needs to find tombstones to restore.
    """
    normalized_path = normalize_manifest_path(path)
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        if folder.get("remote_folder_id") != remote_folder_id:
            continue
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "file")) != "file":
                continue
            entry_path = unicodedata.normalize("NFC", str(entry.get("path", "")))
            if entry_path == normalized_path:
                return entry
    return None


def add_or_append_file_version(
    manifest: dict[str, Any],
    *,
    remote_folder_id: str,
    path: str,
    version: dict[str, Any],
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Mutate ``manifest`` so ``path`` has ``version`` as its latest version.

    If no entry exists at ``path``, a new file entry is created with a
    fresh ``entry_id`` (or the caller-provided one). If an entry exists,
    ``version`` is appended and ``latest_version_id`` flips to point at
    it. The entry's ``deleted`` flag is cleared on append — re-uploading
    over a tombstone restores the file (matches §D5 / T7.4 semantics).

    Returns a new normalized manifest dict; callers own revision +
    parent_revision bookkeeping.
    """
    if not _FILE_VERSION_ID_RE.match(str(version.get("version_id", ""))):
        raise ValueError("version.version_id must match ^fv_v1_[a-z2-7]{24}$")

    out = normalize_manifest_plaintext(manifest)
    normalized_path = normalize_manifest_path(path)

    folder = None
    for candidate in out["remote_folders"]:
        if candidate.get("remote_folder_id") == remote_folder_id:
            folder = candidate
            break
    if folder is None:
        raise ValueError(f"remote folder not found: {remote_folder_id}")

    folder.setdefault("entries", [])
    target = None
    for entry in folder["entries"]:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if unicodedata.normalize("NFC", str(entry.get("path", ""))) == normalized_path:
            target = entry
            break

    new_version = copy.deepcopy(version)
    if target is None:
        new_entry_id = entry_id or generate_file_entry_id()
        if not _FILE_ENTRY_ID_RE.match(new_entry_id):
            raise ValueError("entry_id must match ^fe_v1_[a-z2-7]{24}$")
        target = {
            "entry_id": new_entry_id,
            "type": "file",
            "path": normalized_path,
            "deleted": False,
            "latest_version_id": new_version["version_id"],
            "versions": [new_version],
        }
        folder["entries"].append(target)
    else:
        target["versions"] = [
            v for v in target.get("versions", []) if isinstance(v, dict)
        ]
        target["versions"].append(new_version)
        target["latest_version_id"] = new_version["version_id"]
        target["deleted"] = False
        target.pop("deleted_at", None)
        target.pop("recoverable_until", None)
    return out


def compute_recoverable_until(deleted_at: str, keep_deleted_days: int) -> str:
    """T7.6: ``deleted_at + keep_deleted_days * 86400`` formatted as RFC 3339.

    Per §D5/§A8 the *authoritative* recoverable_until check is performed
    server-side at GC plan time (server clock). This helper is for
    display-only — same formula, applied to the manifest's
    client-supplied ``deleted_at``. A clock-skewed client cannot
    accelerate or delay purge with it because the server ignores the
    field when deciding what to evict.

    Returns ``""`` if ``deleted_at`` is missing or unparseable so the
    UI can fall back to "no deadline shown" gracefully.
    """
    raw = str(deleted_at or "").strip()
    if not raw:
        return ""
    try:
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        when = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    horizon = when + timedelta(days=max(0, int(keep_deleted_days)))
    return horizon.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tombstone_file_entry(
    manifest: dict[str, Any],
    *,
    remote_folder_id: str,
    path: str,
    deleted_at: str,
    author_device_id: str,
) -> dict[str, Any]:
    """Mark the file entry at ``(remote_folder_id, path)`` as soft-deleted.

    Per §D5/§A8 the manifest carries a client-supplied ``deleted_at`` for
    display only; the server computes ``recoverable_until`` from its own
    clock at GC plan time so a clock-skewed client cannot accelerate or
    delay purge. Versions and chunks stay in place — the tombstone just
    flips ``deleted=True`` and stamps the deletion's authoring device.

    Raises ``KeyError`` if the entry doesn't exist; tombstoning an
    already-deleted entry refreshes ``deleted_at`` (re-deleting is a
    no-op semantically but updates the audit fields).
    """
    out = normalize_manifest_plaintext(manifest)
    normalized_path = normalize_manifest_path(path)
    folder = None
    for candidate in out["remote_folders"]:
        if candidate.get("remote_folder_id") == remote_folder_id:
            folder = candidate
            break
    if folder is None:
        raise KeyError(f"remote folder not found: {remote_folder_id}")

    target = None
    for entry in folder.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if unicodedata.normalize("NFC", str(entry.get("path", ""))) == normalized_path:
            target = entry
            break
    if target is None:
        raise KeyError(f"file not found: {path}")

    target["deleted"] = True
    target["deleted_at"] = str(deleted_at)
    target["deleted_by_device_id"] = str(author_device_id)
    keep_days = _retention_keep_days(folder)
    horizon = compute_recoverable_until(str(deleted_at), keep_days)
    if horizon:
        target["recoverable_until"] = horizon
    return out


def _retention_keep_days(folder: dict[str, Any]) -> int:
    policy = folder.get("retention_policy")
    if not isinstance(policy, dict):
        return int(DEFAULT_RETENTION_POLICY["keep_deleted_days"])
    try:
        return int(policy.get("keep_deleted_days", DEFAULT_RETENTION_POLICY["keep_deleted_days"]))
    except (TypeError, ValueError):
        return int(DEFAULT_RETENTION_POLICY["keep_deleted_days"])


def tombstone_files_under(
    manifest: dict[str, Any],
    *,
    remote_folder_id: str,
    path_prefix: str,
    deleted_at: str,
    author_device_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Bulk soft-delete (T7.2): tombstone every file at-or-under ``path_prefix``.

    ``path_prefix == ""`` means "the entire remote folder root". Already-
    deleted entries are left untouched (we don't overwrite their
    original ``deleted_at``). Returns ``(manifest, paths_tombstoned)``
    so the UI can report exactly what changed.
    """
    out = normalize_manifest_plaintext(manifest)
    folder = None
    for candidate in out["remote_folders"]:
        if candidate.get("remote_folder_id") == remote_folder_id:
            folder = candidate
            break
    if folder is None:
        raise KeyError(f"remote folder not found: {remote_folder_id}")

    if path_prefix:
        prefix = normalize_manifest_path(path_prefix)
        prefix_with_slash = prefix + "/"
    else:
        prefix = ""
        prefix_with_slash = ""

    keep_days = _retention_keep_days(folder)
    horizon = compute_recoverable_until(str(deleted_at), keep_days)
    tombstoned: list[str] = []
    for entry in folder.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if bool(entry.get("deleted")):
            continue
        entry_path = unicodedata.normalize("NFC", str(entry.get("path", "")))
        if prefix and entry_path != prefix and not entry_path.startswith(prefix_with_slash):
            continue
        entry["deleted"] = True
        entry["deleted_at"] = str(deleted_at)
        entry["deleted_by_device_id"] = str(author_device_id)
        if horizon:
            entry["recoverable_until"] = horizon
        tombstoned.append(entry_path)
    return out, tombstoned


def restore_file_entry(
    manifest: dict[str, Any],
    *,
    remote_folder_id: str,
    path: str,
    new_version: dict[str, Any],
    author_device_id: str,
) -> dict[str, Any]:
    """T7.4: Restore a tombstoned file *or* promote a previous version.

    ``new_version`` is a freshly-built version dict whose ``chunks``
    list references existing chunk_ids (no new ciphertext required).
    Caller picks ``new_version["version_id"]`` and the chunk references
    from any historical version; this helper just wires it as the
    latest version, clears the tombstone, and records the restorer's
    device_id for the audit trail.
    """
    if not _FILE_VERSION_ID_RE.match(str(new_version.get("version_id", ""))):
        raise ValueError("new_version.version_id must match ^fv_v1_[a-z2-7]{24}$")

    out = normalize_manifest_plaintext(manifest)
    normalized_path = normalize_manifest_path(path)
    folder = None
    for candidate in out["remote_folders"]:
        if candidate.get("remote_folder_id") == remote_folder_id:
            folder = candidate
            break
    if folder is None:
        raise KeyError(f"remote folder not found: {remote_folder_id}")

    target = None
    for entry in folder.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if unicodedata.normalize("NFC", str(entry.get("path", ""))) == normalized_path:
            target = entry
            break
    if target is None:
        raise KeyError(f"file not found: {path}")

    versions = [v for v in target.get("versions", []) if isinstance(v, dict)]
    versions.append(copy.deepcopy(new_version))
    target["versions"] = versions
    target["latest_version_id"] = str(new_version["version_id"])
    target["deleted"] = False
    target.pop("deleted_at", None)
    target.pop("deleted_by_device_id", None)
    target.pop("recoverable_until", None)
    target["restored_by_device_id"] = str(author_device_id)
    return out


def merge_with_remote_head(
    *,
    parent: dict[str, Any],
    local_attempt: dict[str, Any],
    server_head: dict[str, Any],
    author_device_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """§D4 CAS merge: rebuild ``local_attempt`` on top of ``server_head``.

    Implements the deterministic per-op rules from §D4 that T6 cares
    about (file uploads): "new file at path P", "new version of
    existing file F", and the latest-version tie-breaker by
    ``(modified_at, sha256(author_device_id))`` lex order. Other op
    types — soft-delete, restore, rename, hard-purge — are not
    exercised by T6 and intentionally land as a passthrough copy of
    ``local_attempt``'s entry, to be revisited when T7 layers tombstone
    semantics on top.

    The output's ``revision`` is ``server_head.revision + 1`` and
    ``parent_revision`` is ``server_head.revision``, so the caller can
    immediately CAS-publish without further bookkeeping.
    """
    parent_n = normalize_manifest_plaintext(parent)
    local_n = normalize_manifest_plaintext(local_attempt)
    server_n = normalize_manifest_plaintext(server_head)

    server_revision = int(server_n.get("revision", 0))
    out = copy.deepcopy(server_n)
    out["revision"] = server_revision + 1
    out["parent_revision"] = server_revision
    out["created_at"] = str(now or _now_rfc3339_default())
    out["author_device_id"] = str(author_device_id)

    parent_folders = {
        str(f.get("remote_folder_id", "")): f for f in parent_n.get("remote_folders", [])
    }
    local_folders = {
        str(f.get("remote_folder_id", "")): f for f in local_n.get("remote_folders", [])
    }
    out_folders_by_id: dict[str, dict[str, Any]] = {
        str(f.get("remote_folder_id", "")): f for f in out["remote_folders"]
    }

    for folder_id, local_folder in local_folders.items():
        parent_folder = parent_folders.get(folder_id)
        out_folder = out_folders_by_id.get(folder_id)
        if out_folder is None:
            out["remote_folders"].append(copy.deepcopy(local_folder))
            out_folders_by_id[folder_id] = out["remote_folders"][-1]
            continue
        _merge_folder_entries(
            local_folder=local_folder,
            parent_folder=parent_folder,
            out_folder=out_folder,
        )

    return out


def _merge_folder_entries(
    *,
    local_folder: dict[str, Any],
    parent_folder: dict[str, Any] | None,
    out_folder: dict[str, Any],
) -> None:
    parent_entries: dict[str, dict[str, Any]] = {
        str(e.get("entry_id", "")): e
        for e in (parent_folder or {}).get("entries", [])
        if isinstance(e, dict)
    }
    out_folder.setdefault("entries", [])
    out_entries_by_id: dict[str, dict[str, Any]] = {
        str(e.get("entry_id", "")): e
        for e in out_folder["entries"]
        if isinstance(e, dict)
    }

    local_entries = local_folder.get("entries", []) or []
    for local_entry in local_entries:
        if not isinstance(local_entry, dict):
            continue
        entry_id = str(local_entry.get("entry_id", ""))
        if not entry_id:
            continue
        parent_entry = parent_entries.get(entry_id)
        out_entry = out_entries_by_id.get(entry_id)

        parent_version_ids = {
            str(v.get("version_id", ""))
            for v in (parent_entry or {}).get("versions", [])
            if isinstance(v, dict)
        }
        local_versions_new = [
            v for v in local_entry.get("versions", []) or []
            if isinstance(v, dict) and str(v.get("version_id", "")) not in parent_version_ids
        ]
        if not local_versions_new and parent_entry is not None:
            # Local didn't actually add anything new; ignore.
            continue

        if out_entry is None:
            # New file from local. Check for path collision against the
            # server head and rename per the §D4 "Upload new file at path P"
            # row.
            local_path = unicodedata.normalize("NFC", str(local_entry.get("path", "")))
            existing_paths = {
                unicodedata.normalize("NFC", str(e.get("path", "")))
                for e in out_folder["entries"]
                if isinstance(e, dict) and not bool(e.get("deleted"))
            }
            if local_path in existing_paths:
                renamed = _imported_rename(local_path, existing_paths)
                new_entry = copy.deepcopy(local_entry)
                new_entry["path"] = renamed
                out_folder["entries"].append(new_entry)
            else:
                out_folder["entries"].append(copy.deepcopy(local_entry))
            continue

        # Same entry exists on both sides; append the new local versions
        # and resolve latest_version_id deterministically per §D4.
        existing_version_ids = {
            str(v.get("version_id", ""))
            for v in out_entry.get("versions", [])
            if isinstance(v, dict)
        }
        for v in local_versions_new:
            if str(v.get("version_id", "")) in existing_version_ids:
                continue
            out_entry.setdefault("versions", []).append(copy.deepcopy(v))
        out_entry["latest_version_id"] = _resolve_latest_version_id(
            [v for v in out_entry.get("versions", []) if isinstance(v, dict)]
        )
        # If the server tombstoned this entry, the tombstone wins per §D4
        # row 3: versions land as restorable history but `deleted` stays.
        if not bool(out_entry.get("deleted")):
            out_entry["deleted"] = False


def _resolve_latest_version_id(versions: list[dict[str, Any]]) -> str:
    """§D4 tie-breaker: latest by (modified_at, sha256(author_device_id))."""
    if not versions:
        return ""

    def sort_key(version: dict[str, Any]) -> tuple[str, bytes]:
        ts = str(version.get("modified_at") or version.get("created_at") or "")
        author = str(version.get("author_device_id") or "")
        return (ts, hashlib.sha256(author.encode("utf-8")).digest())

    winner = max(versions, key=sort_key)
    return str(winner.get("version_id", ""))


def _imported_rename(path: str, existing_paths: set[str]) -> str:
    """`<stem> (imported)` then `(imported N)` if the rename collides."""
    parts = path.split("/")
    leaf = parts[-1]
    parent = "/".join(parts[:-1])
    dot = leaf.rfind(".")
    stem, ext = (leaf[:dot], leaf[dot:]) if dot > 0 else (leaf, "")
    candidate = f"{stem} (imported){ext}"
    full = f"{parent}/{candidate}" if parent else candidate
    if full not in existing_paths:
        return full
    for n in range(2, 10_000):
        candidate = f"{stem} (imported {n}){ext}"
        full = f"{parent}/{candidate}" if parent else candidate
        if full not in existing_paths:
            return full
    raise RuntimeError(f"could not pick an imported-rename slot for {path}")


def _now_rfc3339_default() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _random_base32_lower(chars: int) -> str:
    """Generate ``chars`` lowercase base32 characters from secure random bytes."""
    needed_bytes = (chars * 5 + 7) // 8
    raw = secrets.token_bytes(needed_bytes)
    out = []
    bits = 0
    buf = 0
    for byte in raw:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32_LOWER[(buf >> bits) & 0x1f])
    return "".join(out[:chars])


def _validate_remote_folder(folder: dict[str, Any]) -> None:
    required = (
        "remote_folder_id",
        "display_name_enc",
        "created_at",
        "created_by_device_id",
        "retention_policy",
        "ignore_patterns",
        "state",
    )
    for key in required:
        if key not in folder:
            raise ValueError(f"remote folder missing required field: {key}")
    if not _REMOTE_FOLDER_ID_RE.match(str(folder["remote_folder_id"])):
        raise ValueError("remote_folder_id must match ^rf_v1_[a-z2-7]{24}$")
    if not _DEVICE_ID_RE.match(str(folder["created_by_device_id"])):
        raise ValueError("created_by_device_id must be 32 lowercase hex chars")


def _normalize_retention_policy(policy: Any) -> dict[str, int]:
    if not isinstance(policy, dict):
        raise ValueError("retention_policy must be an object")
    out = {
        "keep_deleted_days": int(policy.get("keep_deleted_days", DEFAULT_RETENTION_POLICY["keep_deleted_days"])),
        "keep_versions": int(policy.get("keep_versions", DEFAULT_RETENTION_POLICY["keep_versions"])),
    }
    if out["keep_deleted_days"] < 0:
        raise ValueError("retention_policy.keep_deleted_days must be non-negative")
    if out["keep_versions"] < 0:
        raise ValueError("retention_policy.keep_versions must be non-negative")
    return out


def _normalize_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return [unicodedata.normalize("NFC", str(item)) for item in value]
