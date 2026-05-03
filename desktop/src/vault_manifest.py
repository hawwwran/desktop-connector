"""Vault manifest plaintext helpers.

T4 starts making the encrypted manifest a product data model rather
than just an opaque T2 vector payload. This module owns the v1
plaintext shape before the bytes are canonical-JSON encoded and passed
to the manifest AEAD envelope.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import unicodedata
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
