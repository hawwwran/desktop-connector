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

from .crypto import normalize_vault_id


MANIFEST_SCHEMA = "dc-vault-manifest-v1"
MANIFEST_FORMAT_VERSION = 1
DEFAULT_RETENTION_POLICY = {
    "keep_deleted_days": 30,
    "keep_versions": 10,
}

_REMOTE_FOLDER_ID_RE = re.compile(r"^rf_v1_[a-z2-7]{24}$")
_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"


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


def update_remote_folder_settings(
    manifest: dict[str, Any],
    remote_folder_id: str,
    *,
    new_display_name: str | None = None,
    ignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Return a manifest copy with the folder's editable settings updated.

    Updates ``display_name_enc`` and/or ``ignore_patterns`` for the
    targeted folder. ``None`` for a parameter means "leave that field
    alone" — this lets the configure dialog apply both edits in a
    single CAS publish, while a downstream rename-only call path still
    works (just passes the name).
    """
    out = normalize_manifest_plaintext(manifest)
    if new_display_name is not None:
        name = unicodedata.normalize("NFC", str(new_display_name)).strip()
        if not name:
            raise ValueError("folder name is required")
    else:
        name = None
    for folder in out["remote_folders"]:
        if folder.get("remote_folder_id") == remote_folder_id:
            if name is not None:
                folder["display_name_enc"] = name
            if ignore_patterns is not None:
                folder["ignore_patterns"] = list(ignore_patterns)
            return out
    raise ValueError(f"remote folder not found: {remote_folder_id}")


def canonical_manifest_json(manifest: dict[str, Any]) -> bytes:
    """Canonical JSON bytes for manifest AEAD plaintext."""
    normalized = normalize_manifest_plaintext(manifest)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ManifestRevisionInvariantError(ValueError):
    """Raised when a candidate manifest's revision/parent_revision pair
    violates the §A8 single-bump invariant (``revision ==
    parent_revision + 1`` and both are non-negative integers).

    F-Y21: callers that mutate a manifest body (tombstone, restore,
    add/remove folder, etc.) must explicitly bump the revision pair
    before publishing. Inheriting the parent's pair through
    ``copy.deepcopy`` and forgetting to overwrite is a silent fork-the-
    revision-history bug; checking the invariant at the publish
    boundary forces every code path to go through ``bump_revision`` or
    open-code the bump correctly.
    """


def bump_revision(
    candidate: dict[str, Any],
    *,
    from_parent: dict[str, Any],
) -> dict[str, Any]:
    """Set ``candidate``'s ``revision`` and ``parent_revision`` based on
    ``from_parent`` and return ``candidate`` unchanged otherwise.

    The mutation is in-place to keep with the call-site idiom of
    "transform-then-publish on the same dict". Use this instead of
    open-coding ``next_manifest["revision"] = parent + 1`` so the
    invariant is named in the call site and easier to grep for.
    """
    parent_revision = int(from_parent.get("revision", 0))
    if parent_revision < 0:
        raise ManifestRevisionInvariantError(
            f"parent revision must be non-negative; got {parent_revision}",
        )
    candidate["revision"] = parent_revision + 1
    candidate["parent_revision"] = parent_revision
    return candidate


def assert_publishable_revision(manifest: dict[str, Any]) -> None:
    """Raise :class:`ManifestRevisionInvariantError` unless the
    revision / parent_revision pair satisfies the §A8 invariant.

    Called from :meth:`Vault.publish_manifest` at the top so a malformed
    bump never reaches the relay. Genesis manifests (``revision == 1``,
    ``parent_revision == 0``) pass; every subsequent revision must be
    ``parent_revision + 1`` exactly. Negative or non-integer values
    fail too — they signal a deepcopy-from-encrypted-payload that
    skipped normalization.
    """
    try:
        revision = int(manifest.get("revision", -1))
        parent_revision = int(manifest.get("parent_revision", -1))
    except (TypeError, ValueError) as exc:
        raise ManifestRevisionInvariantError(
            f"revision pair must be integers; got "
            f"revision={manifest.get('revision')!r} "
            f"parent_revision={manifest.get('parent_revision')!r}",
        ) from exc
    if revision < 1:
        raise ManifestRevisionInvariantError(
            f"revision must be >= 1; got {revision}",
        )
    if parent_revision < 0:
        raise ManifestRevisionInvariantError(
            f"parent_revision must be non-negative; got {parent_revision}",
        )
    if revision != parent_revision + 1:
        raise ManifestRevisionInvariantError(
            f"revision must be parent_revision + 1; "
            f"got revision={revision} parent_revision={parent_revision}",
        )


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


def _merge_folder_entries(
    *,
    local_folder: dict[str, Any],
    parent_folder: dict[str, Any] | None,
    out_folder: dict[str, Any],
) -> None:
    """Folder-scoped §D4 merge: rebuild ``out_folder.entries`` from
    ``server_head`` + new versions/entries pulled from
    ``local_attempt``.

    Used today by ``merge_shard_with_remote_head`` (the sharded §D4
    merge for the CAS-retry path). The legacy unified-shape
    ``merge_with_remote_head`` was dropped in §3.6; the helper stays
    because the shard variant synthesizes folder-wrappers around its
    entry lists and reuses this body.
    """
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
            continue

        if out_entry is None:
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

        existing_version_ids = {
            str(v.get("version_id", ""))
            for v in out_entry.get("versions", [])
            if isinstance(v, dict)
        }
        for v in local_versions_new:
            if str(v.get("version_id", "")) in existing_version_ids:
                continue
            out_entry.setdefault("versions", []).append(copy.deepcopy(v))

        # F-D07: re-resolve ``latest_version_id`` only when the entry
        # is still live; a tombstoned entry keeps the server's
        # pre-tombstone latest so eviction's preserve-latest pass
        # doesn't pin chunks belonging to a deleted file.
        if not bool(out_entry.get("deleted")):
            out_entry["latest_version_id"] = _resolve_latest_version_id(
                [v for v in out_entry.get("versions", []) if isinstance(v, dict)]
            )
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
    """`<stem> (imported)` then `(imported N)` if the rename collides.

    Review §2.L3 — the loop tops out at N=10_000 because the search
    space is bounded (a folder with that many `(imported N)` siblings
    of one file is well past the point where automatic renaming is
    the right answer). Hitting the cap surfaces ``RuntimeError`` rather
    than silently overwriting; the import wizard catches it and
    surfaces it to the user.
    """
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


# ====================================================================
#  Sharded manifest model (Phase C of the manifest-sharding rollout)
# ====================================================================
#
# The wire spec moves from one envelope per vault to a small **root**
# envelope (vault metadata + folder pointers) plus one **shard**
# envelope per remote folder. The helpers below are the in-memory dict
# shape that mirrors the new envelopes.
#
# Phase H step 7f: production code uses the sharded helpers below
# exclusively. The legacy unified-shape builders (``make_manifest``,
# ``make_remote_folder``, ``tombstone_file_entry``, ``find_file_entry``)
# were dropped 2026-05-19; ``assemble_unified_manifest`` remains the
# bridge for any test or read-only call site that still wants the
# legacy single-envelope dict shape from the sharded primitives.
#
# Spec: docs/protocol/vault-v1.md §6.4–§6.8,
#       docs/protocol/vault-v1-formats.md §10.A / §10.B.

ROOT_SCHEMA  = "dc-vault-root-v1"
SHARD_SCHEMA = "dc-vault-shard-v1"


def make_root_manifest(
    *,
    vault_id: str,
    root_revision: int,
    parent_root_revision: int,
    created_at: str,
    author_device_id: str,
    retention_policy: dict[str, int] | None = None,
    remote_folders: list[dict[str, Any]] | None = None,
    operation_log_tail: list[dict[str, Any]] | None = None,
    archived_op_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a normalized v1 root manifest plaintext object.

    The root carries vault-wide metadata only: folder pointers with
    their per-shard hash chain anchors, retention defaults, and the
    vault-scoped op log. File entries live in per-folder shards
    (``make_folder_shard``).
    """
    root = {
        "schema": ROOT_SCHEMA,
        "vault_id": normalize_vault_id(vault_id),
        "root_revision": int(root_revision),
        "parent_root_revision": int(parent_root_revision),
        "created_at": str(created_at),
        "author_device_id": str(author_device_id),
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "retention_policy": copy.deepcopy(retention_policy or DEFAULT_RETENTION_POLICY),
        "remote_folders": list(remote_folders or []),
        "operation_log_tail": list(operation_log_tail or []),
        "archived_op_segments": list(archived_op_segments or []),
    }
    return normalize_root_manifest_plaintext(root)


def make_root_folder_pointer(
    *,
    remote_folder_id: str,
    display_name_enc: str,
    created_at: str,
    created_by_device_id: str,
    shard_revision: int = 0,
    shard_hash: str = "",
    retention_policy: dict[str, int] | None = None,
    ignore_patterns: list[str] | None = None,
    state: str = "active",
) -> dict[str, Any]:
    """Build a root-manifest folder pointer (no ``entries`` field).

    The shard_revision / shard_hash pair anchors the §10.C hash chain:
    after every per-folder publish the pointer's hash MUST equal
    ``sha256(shard_envelope_bytes)`` for the referenced shard. The
    defaults (``0`` / ``""``) are valid only on a brand-new folder
    pointer that has not yet had a shard published — ``make_folder_shard``
    + ``publish_shard_with_root`` (Phase D) fill them in atomically.
    """
    pointer: dict[str, Any] = {
        "remote_folder_id": remote_folder_id,
        "display_name_enc": unicodedata.normalize("NFC", display_name_enc),
        "created_at": created_at,
        "created_by_device_id": created_by_device_id,
        "state": state,
        "retention_policy": copy.deepcopy(retention_policy or DEFAULT_RETENTION_POLICY),
        "ignore_patterns": list(ignore_patterns or []),
        "shard_revision": int(shard_revision),
        "shard_hash": str(shard_hash),
    }
    _validate_root_folder_pointer(pointer)
    return pointer


def make_folder_shard(
    *,
    vault_id: str,
    remote_folder_id: str,
    shard_revision: int,
    parent_shard_revision: int,
    created_at: str,
    author_device_id: str,
    entries: list[dict[str, Any]] | None = None,
    operation_log_tail: list[dict[str, Any]] | None = None,
    archived_op_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a normalized v1 folder shard plaintext object."""
    if not _REMOTE_FOLDER_ID_RE.match(remote_folder_id):
        raise ValueError("remote_folder_id must match ^rf_v1_[a-z2-7]{24}$")
    shard = {
        "schema": SHARD_SCHEMA,
        "vault_id": normalize_vault_id(vault_id),
        "remote_folder_id": remote_folder_id,
        "shard_revision": int(shard_revision),
        "parent_shard_revision": int(parent_shard_revision),
        "created_at": str(created_at),
        "author_device_id": str(author_device_id),
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "entries": list(entries or []),
        "operation_log_tail": list(operation_log_tail or []),
        "archived_op_segments": list(archived_op_segments or []),
    }
    return normalize_shard_plaintext(shard)


def normalize_root_manifest_plaintext(root: dict[str, Any]) -> dict[str, Any]:
    """Return a v1-compatible root manifest copy.

    Same defaults-shape pattern as ``normalize_manifest_plaintext`` but
    drops the per-folder ``entries`` field (which now lives in shards)
    and demands the ``shard_revision`` + ``shard_hash`` pair on every
    folder pointer. Defaults to ``0`` / ``""`` so a freshly-built root
    listing a newly-added folder normalizes cleanly before the first
    shard publish.
    """
    if not isinstance(root, dict):
        raise ValueError("root manifest plaintext must be an object")

    out = copy.deepcopy(root)
    out.setdefault("schema", ROOT_SCHEMA)
    out.setdefault("manifest_format_version", MANIFEST_FORMAT_VERSION)
    out.setdefault("operation_log_tail", [])
    out.setdefault("archived_op_segments", [])
    out.setdefault("retention_policy", copy.deepcopy(DEFAULT_RETENTION_POLICY))

    folders = out.get("remote_folders", [])
    if folders is None:
        folders = []
    if not isinstance(folders, list):
        raise ValueError("remote_folders must be a list")

    created_at = str(out.get("created_at", ""))
    author_device_id = str(out.get("author_device_id", ""))
    out["remote_folders"] = [
        _normalize_root_folder_pointer(
            folder,
            default_created_at=created_at,
            default_created_by_device_id=author_device_id,
        )
        for folder in folders
    ]
    out["retention_policy"] = _normalize_retention_policy(out["retention_policy"])
    return out


def normalize_shard_plaintext(shard: dict[str, Any]) -> dict[str, Any]:
    """Return a v1-compatible shard copy.

    ``entries`` may be missing on a brand-new shard — normalized to
    ``[]``. Entry-shape semantics are byte-identical to the legacy
    single-manifest ``remote_folders[*].entries`` shape (file_id, path,
    versions[], chunks[], deletion flags, etc.) so the helpers stay
    portable.
    """
    if not isinstance(shard, dict):
        raise ValueError("shard plaintext must be an object")

    out = copy.deepcopy(shard)
    out.setdefault("schema", SHARD_SCHEMA)
    out.setdefault("manifest_format_version", MANIFEST_FORMAT_VERSION)
    out.setdefault("operation_log_tail", [])
    out.setdefault("archived_op_segments", [])
    out.setdefault("entries", [])
    entries = out["entries"]
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ValueError("shard entries must be a list")
    out["entries"] = list(entries)
    return out


def canonical_root_json(root: dict[str, Any]) -> bytes:
    """Canonical JSON bytes for root AEAD plaintext."""
    normalized = normalize_root_manifest_plaintext(root)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_shard_json(shard: dict[str, Any]) -> bytes:
    """Canonical JSON bytes for shard AEAD plaintext."""
    normalized = normalize_shard_plaintext(shard)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")


def bump_root_revision(
    candidate: dict[str, Any],
    *,
    from_parent: dict[str, Any],
) -> dict[str, Any]:
    """``bump_revision`` analogue for the root chain (§A8 invariant)."""
    parent_revision = int(from_parent.get("root_revision", 0))
    if parent_revision < 0:
        raise ManifestRevisionInvariantError(
            f"parent root_revision must be non-negative; got {parent_revision}",
        )
    candidate["root_revision"] = parent_revision + 1
    candidate["parent_root_revision"] = parent_revision
    return candidate


def bump_shard_revision(
    candidate: dict[str, Any],
    *,
    from_parent: dict[str, Any],
) -> dict[str, Any]:
    """``bump_revision`` analogue for a shard chain (§A8 invariant)."""
    parent_revision = int(from_parent.get("shard_revision", 0))
    if parent_revision < 0:
        raise ManifestRevisionInvariantError(
            f"parent shard_revision must be non-negative; got {parent_revision}",
        )
    candidate["shard_revision"] = parent_revision + 1
    candidate["parent_shard_revision"] = parent_revision
    return candidate


def assert_publishable_root_revision(root: dict[str, Any]) -> None:
    """Same §A8 single-bump invariant, on the root chain."""
    try:
        revision = int(root.get("root_revision", -1))
        parent_revision = int(root.get("parent_root_revision", -1))
    except (TypeError, ValueError) as exc:
        raise ManifestRevisionInvariantError(
            f"root revision pair must be integers; got "
            f"root_revision={root.get('root_revision')!r} "
            f"parent_root_revision={root.get('parent_root_revision')!r}",
        ) from exc
    if revision < 1:
        raise ManifestRevisionInvariantError(
            f"root_revision must be >= 1; got {revision}",
        )
    if parent_revision < 0:
        raise ManifestRevisionInvariantError(
            f"parent_root_revision must be non-negative; got {parent_revision}",
        )
    if revision != parent_revision + 1:
        raise ManifestRevisionInvariantError(
            f"root_revision must be parent_root_revision + 1; "
            f"got root_revision={revision} parent_root_revision={parent_revision}",
        )


def assert_publishable_shard_revision(shard: dict[str, Any]) -> None:
    """Same §A8 single-bump invariant, on a shard chain."""
    try:
        revision = int(shard.get("shard_revision", -1))
        parent_revision = int(shard.get("parent_shard_revision", -1))
    except (TypeError, ValueError) as exc:
        raise ManifestRevisionInvariantError(
            f"shard revision pair must be integers; got "
            f"shard_revision={shard.get('shard_revision')!r} "
            f"parent_shard_revision={shard.get('parent_shard_revision')!r}",
        ) from exc
    if revision < 1:
        raise ManifestRevisionInvariantError(
            f"shard_revision must be >= 1; got {revision}",
        )
    if parent_revision < 0:
        raise ManifestRevisionInvariantError(
            f"parent_shard_revision must be non-negative; got {parent_revision}",
        )
    if revision != parent_revision + 1:
        raise ManifestRevisionInvariantError(
            f"shard_revision must be parent_shard_revision + 1; "
            f"got shard_revision={revision} parent_shard_revision={parent_revision}",
        )


def assemble_unified_manifest(
    root: dict[str, Any],
    shards_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compose a legacy-shaped vault-wide manifest from a root + shard map.

    Soft migration surface for callers that haven't been ported to the
    sharded model yet (Phase D + E migrate them one by one; Phase H
    removes this helper). The returned dict matches the byte shape the
    pre-sharding ``Vault.fetch_manifest`` produced: one
    ``remote_folders[]`` array, each entry carrying its own
    ``entries[]`` array merged from the matching shard.

    Folders whose shard is missing from ``shards_by_id`` render as
    pointers with ``entries: []`` — useful when only one binding's
    shard has been fetched and the caller wants the full vault view
    without the round-trip to fetch every other shard.

    The unified ``operation_log_tail`` field merges the root tail with
    every available shard's tail, sorted by
    ``(ts, device_id, revision)`` ascending so the consumer side sees a
    deterministic timeline regardless of fetch order. Shards missing
    from ``shards_by_id`` contribute no entries (per the same lazy-fetch
    semantics that drive the ``entries: []`` fallback above).
    """
    root_n = normalize_root_manifest_plaintext(root)
    merged_tail: list[dict[str, Any]] = list(root_n["operation_log_tail"])
    unified = {
        "schema": MANIFEST_SCHEMA,
        "vault_id": root_n["vault_id"],
        "revision": int(root_n["root_revision"]),
        "parent_revision": int(root_n["parent_root_revision"]),
        "created_at": root_n["created_at"],
        "author_device_id": root_n["author_device_id"],
        "manifest_format_version": int(root_n["manifest_format_version"]),
        "remote_folders": [],
        "operation_log_tail": [],
        "archived_op_segments": list(root_n["archived_op_segments"]),
    }
    for pointer in root_n["remote_folders"]:
        rf_id = str(pointer["remote_folder_id"])
        folder_entry = {
            "remote_folder_id": rf_id,
            "display_name_enc": pointer["display_name_enc"],
            "created_at": pointer["created_at"],
            "created_by_device_id": pointer["created_by_device_id"],
            "state": pointer["state"],
            "retention_policy": copy.deepcopy(pointer["retention_policy"]),
            "ignore_patterns": list(pointer["ignore_patterns"]),
            "entries": [],
        }
        shard = shards_by_id.get(rf_id)
        if shard is not None:
            shard_n = normalize_shard_plaintext(shard)
            folder_entry["entries"] = copy.deepcopy(shard_n["entries"])
            merged_tail.extend(copy.deepcopy(shard_n["operation_log_tail"]))
        unified["remote_folders"].append(folder_entry)
    merged_tail.sort(key=_op_log_sort_key)
    unified["operation_log_tail"] = merged_tail
    return normalize_manifest_plaintext(unified)


def _op_log_sort_key(entry: dict[str, Any]) -> tuple[int, str, int]:
    """Deterministic tie-break order for the unified op-log timeline.

    Burst uploads from one device share a wall-clock second; sorting by
    ``ts`` alone would shuffle them on every re-fetch. Tie-break first
    on author ``device_id`` (lexicographic, lower-cased 32-hex) and
    finally on ``revision`` so entries authored by the same device
    in the same second land in revision order.
    """
    try:
        ts = int(entry.get("ts", 0))
    except (TypeError, ValueError):
        ts = 0
    try:
        revision = int(entry.get("revision", 0))
    except (TypeError, ValueError):
        revision = 0
    device_id = str(entry.get("device_id", ""))
    return (ts, device_id, revision)


# --------------------------------------------------------------------
#  Shard-scoped entry helpers (mirrors the legacy folder-walking helpers
#  but operates on a single shard dict, no folder loop).
# --------------------------------------------------------------------


def find_file_entry_in_shard(
    shard: dict[str, Any],
    path: str,
) -> dict[str, Any] | None:
    """Return the file entry at ``path`` inside ``shard``, or None."""
    normalized_path = normalize_manifest_path(path)
    for entry in shard.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if unicodedata.normalize("NFC", str(entry.get("path", ""))) == normalized_path:
            return entry
    return None


def add_or_append_file_version_in_shard(
    shard: dict[str, Any],
    *,
    path: str,
    version: dict[str, Any],
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Shard-scoped variant of ``add_or_append_file_version``.

    Same idempotent-publish semantics (F-D05) as the legacy helper —
    re-publishing the same ``version_id`` is a no-op that returns the
    shard unchanged. Caller owns ``shard_revision`` + ``parent_shard_revision``
    bookkeeping (use ``bump_shard_revision`` before publishing).
    """
    if not _FILE_VERSION_ID_RE.match(str(version.get("version_id", ""))):
        raise ValueError("version.version_id must match ^fv_v1_[a-z2-7]{24}$")

    out = normalize_shard_plaintext(shard)
    normalized_path = normalize_manifest_path(path)

    target = None
    for entry in out["entries"]:
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
        out["entries"].append(target)
    else:
        target["versions"] = [
            v for v in target.get("versions", []) if isinstance(v, dict)
        ]
        version_id_str = str(new_version.get("version_id", ""))
        existing_ids = [str(v.get("version_id", "")) for v in target["versions"]]
        if version_id_str and version_id_str in existing_ids:
            target["latest_version_id"] = version_id_str
            target["deleted"] = False
            target.pop("deleted_at", None)
            target.pop("recoverable_until", None)
            return out
        target["versions"].append(new_version)
        target["latest_version_id"] = new_version["version_id"]
        target["deleted"] = False
        target.pop("deleted_at", None)
        target.pop("recoverable_until", None)
    return out


def merge_local_version_into_shard(
    server_shard: dict[str, Any],
    *,
    parent_shard: dict[str, Any] | None,
    entry_id: str,
    path: str,
    version: dict[str, Any],
) -> dict[str, Any]:
    """§D4 CAS merge for the sharded path: rebuild a local upload's
    single-version contribution on top of ``server_shard``.

    Mirrors :func:`_merge_folder_entries` but scoped to one shard +
    one local version. Two distinct cases:

    * **No entry with this ``entry_id`` on the server**: it's a "new
      file at path P" from local. If the server already has a live
      entry at the same ``path``, the local entry is renamed via
      ``_imported_rename`` per §D4 row 1.
    * **Entry exists on the server**: it's a "new version" from
      local. Append the new version if not already present; re-resolve
      ``latest_version_id`` via the §D4 tie-break
      ``(modified_at, sha256(author_device_id))``.

    Returns the merged shard. ``shard_revision`` / ``parent_shard_revision``
    bookkeeping stays with the caller.
    """
    if not _FILE_VERSION_ID_RE.match(str(version.get("version_id", ""))):
        raise ValueError("version.version_id must match ^fv_v1_[a-z2-7]{24}$")
    if not _FILE_ENTRY_ID_RE.match(entry_id):
        raise ValueError("entry_id must match ^fe_v1_[a-z2-7]{24}$")

    out = normalize_shard_plaintext(server_shard)
    normalized_path = normalize_manifest_path(path)
    # ``parent_shard`` is accepted in the signature to mirror the legacy
    # ``_merge_folder_entries`` shape (which used it to filter
    # ``local_versions_new`` across multiple local entries). The single-
    # version helper doesn't iterate multiple local entries, so the
    # filter is unnecessary; deduplication of an already-present
    # ``version_id`` is handled inline below by ``server_version_ids``.
    _ = parent_shard  # retained-for-callsite-compat; no behavior

    new_version = copy.deepcopy(version)
    target = None
    for entry in out["entries"]:
        if isinstance(entry, dict) and str(entry.get("entry_id", "")) == entry_id:
            target = entry
            break

    if target is None:
        # "New file at path P" from local. Rename on path collision
        # with a live server-side entry.
        existing_paths = {
            unicodedata.normalize("NFC", str(e.get("path", "")))
            for e in out["entries"]
            if isinstance(e, dict) and not bool(e.get("deleted"))
        }
        landed_path = normalized_path
        if landed_path in existing_paths:
            landed_path = _imported_rename(landed_path, existing_paths)
        out["entries"].append({
            "entry_id": entry_id,
            "type": "file",
            "path": landed_path,
            "deleted": False,
            "latest_version_id": new_version["version_id"],
            "versions": [new_version],
        })
        return out

    # Existing entry on server: append local's new version if not already
    # present, then tie-break latest_version_id per §D4.
    target["versions"] = [
        v for v in target.get("versions", []) if isinstance(v, dict)
    ]
    server_version_ids = {
        str(v.get("version_id", "")) for v in target["versions"]
    }
    if str(new_version.get("version_id", "")) not in server_version_ids:
        target["versions"].append(new_version)
    # Re-resolve latest_version_id only when the entry is still live —
    # F-D07 says tombstoned entries keep their pre-tombstone latest.
    if not bool(target.get("deleted")):
        target["latest_version_id"] = _resolve_latest_version_id(
            [v for v in target["versions"] if isinstance(v, dict)]
        )
        target["deleted"] = False
        target.pop("deleted_at", None)
        target.pop("recoverable_until", None)
    return out


def tombstone_file_entry_in_shard(
    shard: dict[str, Any],
    *,
    path: str,
    deleted_at: str,
    author_device_id: str,
    folder_retention_policy: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Mark a file as soft-deleted in the shard.

    ``folder_retention_policy`` is supplied by the caller (it lives in
    the root pointer, not the shard plaintext) so the helper can
    compute the display-only ``recoverable_until``. Passing ``None``
    falls back to the default retention policy.
    """
    out = normalize_shard_plaintext(shard)
    normalized_path = normalize_manifest_path(path)
    target = None
    for entry in out["entries"]:
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
    policy = folder_retention_policy or DEFAULT_RETENTION_POLICY
    try:
        keep_days = int(policy.get("keep_deleted_days", DEFAULT_RETENTION_POLICY["keep_deleted_days"]))
    except (TypeError, ValueError):
        keep_days = int(DEFAULT_RETENTION_POLICY["keep_deleted_days"])
    horizon = compute_recoverable_until(str(deleted_at), keep_days)
    if horizon:
        target["recoverable_until"] = horizon
    return out


def restore_file_entry_in_shard(
    shard: dict[str, Any],
    *,
    path: str,
    new_version: dict[str, Any],
    author_device_id: str,
) -> dict[str, Any]:
    """Restore a tombstoned file *or* promote a previous version (T7.4)."""
    if not _FILE_VERSION_ID_RE.match(str(new_version.get("version_id", ""))):
        raise ValueError("new_version.version_id must match ^fv_v1_[a-z2-7]{24}$")

    out = normalize_shard_plaintext(shard)
    normalized_path = normalize_manifest_path(path)
    target = None
    for entry in out["entries"]:
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


def tombstone_files_under_in_shard(
    shard: dict[str, Any],
    *,
    path_prefix: str,
    deleted_at: str,
    author_device_id: str,
    folder_retention_policy: dict[str, int] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Bulk soft-delete in the shard: tombstone every file at-or-under ``path_prefix``."""
    out = normalize_shard_plaintext(shard)
    prefix_normalized = (
        normalize_manifest_path(path_prefix) if path_prefix else ""
    )
    policy = folder_retention_policy or DEFAULT_RETENTION_POLICY
    try:
        keep_days = int(policy.get("keep_deleted_days", DEFAULT_RETENTION_POLICY["keep_deleted_days"]))
    except (TypeError, ValueError):
        keep_days = int(DEFAULT_RETENTION_POLICY["keep_deleted_days"])

    tombstoned: list[str] = []
    for entry in out["entries"]:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if bool(entry.get("deleted")):
            continue
        path = unicodedata.normalize("NFC", str(entry.get("path", "")))
        if prefix_normalized:
            if not (path == prefix_normalized or path.startswith(prefix_normalized + "/")):
                continue
        entry["deleted"] = True
        entry["deleted_at"] = str(deleted_at)
        entry["deleted_by_device_id"] = str(author_device_id)
        horizon = compute_recoverable_until(str(deleted_at), keep_days)
        if horizon:
            entry["recoverable_until"] = horizon
        tombstoned.append(path)
    return out, tombstoned


def merge_shard_with_remote_head(
    *,
    parent: dict[str, Any],
    local_attempt: dict[str, Any],
    server_head: dict[str, Any],
    author_device_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """§D4 CAS merge, shard-scoped variant.

    ``parent`` / ``local_attempt`` / ``server_head`` are all shard dicts
    for the same ``(vault_id, remote_folder_id)``. Output's
    ``shard_revision`` is ``server_head.shard_revision + 1`` so the
    caller can immediately CAS-publish without further bookkeeping.
    """
    parent_n = normalize_shard_plaintext(parent)
    local_n  = normalize_shard_plaintext(local_attempt)
    server_n = normalize_shard_plaintext(server_head)

    server_revision = int(server_n.get("shard_revision", 0))
    out = copy.deepcopy(server_n)
    out["shard_revision"] = server_revision + 1
    out["parent_shard_revision"] = server_revision
    out["created_at"] = str(now or _now_rfc3339_default())
    out["author_device_id"] = str(author_device_id)

    # Reuse the same merge_folder_entries shape the legacy code uses by
    # synthesizing fake folder-wrappers around the entry lists.
    fake_parent_folder = {
        "remote_folder_id": parent_n.get("remote_folder_id", ""),
        "entries": parent_n.get("entries", []),
    }
    fake_local_folder = {
        "remote_folder_id": local_n.get("remote_folder_id", ""),
        "entries": local_n.get("entries", []),
    }
    fake_out_folder = {
        "remote_folder_id": out.get("remote_folder_id", ""),
        "entries": out.get("entries", []),
    }
    _merge_folder_entries(
        local_folder=fake_local_folder,
        parent_folder=fake_parent_folder,
        out_folder=fake_out_folder,
    )
    out["entries"] = fake_out_folder["entries"]
    return out


def _validate_root_folder_pointer(pointer: dict[str, Any]) -> None:
    required = (
        "remote_folder_id",
        "display_name_enc",
        "created_at",
        "created_by_device_id",
        "retention_policy",
        "ignore_patterns",
        "state",
        "shard_revision",
        "shard_hash",
    )
    for key in required:
        if key not in pointer:
            raise ValueError(f"root folder pointer missing required field: {key}")
    if not _REMOTE_FOLDER_ID_RE.match(str(pointer["remote_folder_id"])):
        raise ValueError("remote_folder_id must match ^rf_v1_[a-z2-7]{24}$")
    if not _DEVICE_ID_RE.match(str(pointer["created_by_device_id"])):
        raise ValueError("created_by_device_id must be 32 lowercase hex chars")
    if int(pointer["shard_revision"]) < 0:
        raise ValueError("shard_revision must be non-negative")


def _normalize_root_folder_pointer(
    pointer: dict[str, Any],
    *,
    default_created_at: str | None = None,
    default_created_by_device_id: str | None = None,
) -> dict[str, Any]:
    """Normalize a root-manifest folder pointer (no ``entries``)."""
    if not isinstance(pointer, dict):
        raise ValueError("root folder pointer must be an object")

    out = copy.deepcopy(pointer)
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
    out.setdefault("shard_revision", 0)
    out.setdefault("shard_hash", "")

    # Drop legacy `entries` if a caller hands in a folder dict from the
    # pre-sharding shape; it has no meaning on the root.
    out.pop("entries", None)

    _validate_root_folder_pointer(out)
    out["display_name_enc"] = unicodedata.normalize("NFC", str(out["display_name_enc"]))
    out["created_at"] = str(out["created_at"])
    out["created_by_device_id"] = str(out["created_by_device_id"])
    out["retention_policy"] = _normalize_retention_policy(out["retention_policy"])
    out["ignore_patterns"] = _normalize_string_list(out["ignore_patterns"], "ignore_patterns")
    out["state"] = str(out["state"])
    out["shard_revision"] = int(out["shard_revision"])
    out["shard_hash"] = str(out["shard_hash"])
    return out
