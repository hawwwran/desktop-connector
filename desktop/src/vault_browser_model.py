"""Pure helpers for the Vault browser window.

T5 keeps remote browsing client-side: the relay returns only the
encrypted manifest, and the desktop decrypts/tree-walks it locally.
This module intentionally has no GTK dependency so the path handling
and manifest interpretation stay unit-testable.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .vault_crypto import (
    aead_decrypt,
    build_manifest_aad,
    derive_subkey,
    normalize_vault_id,
)
from .vault_manifest import normalize_manifest_plaintext


class ManifestVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...


def decrypt_manifest(vault: ManifestVault, ciphertext: bytes | bytearray) -> dict[str, Any]:
    """Decrypt one manifest envelope with ``vault``'s in-memory key."""
    if vault.master_key is None:
        raise ValueError("vault is closed")
    envelope = bytes(ciphertext)
    if len(envelope) < 85 + 16:
        raise ValueError("manifest ciphertext too short")

    # F-C13 / spec §7: stop before AEAD when the format version is
    # unknown. v2 envelopes must surface as upgrade-prompt material,
    # not silent ciphertext-failure.
    if envelope[0] != 1:
        raise ValueError(
            f"vault_format_version_unsupported: manifest format_version={envelope[0]}"
        )
    envelope_vault_id = envelope[1:13].decode("ascii")
    expected_vault_id = normalize_vault_id(vault.vault_id)
    if envelope_vault_id != expected_vault_id:
        raise ValueError("manifest vault_id mismatch")

    revision = int.from_bytes(envelope[13:21], "big")
    parent_revision = int.from_bytes(envelope[21:29], "big")
    author_device_id = envelope[29:61].decode("ascii")
    nonce = envelope[61:85]
    encrypted_body = envelope[85:]

    subkey = derive_subkey("dc-vault-v1/manifest", vault.master_key)
    aad = build_manifest_aad(
        vault_id=expected_vault_id,
        revision=revision,
        parent_revision=parent_revision,
        author_device_id=author_device_id,
    )
    plaintext = aead_decrypt(encrypted_body, subkey, nonce, aad)
    return normalize_manifest_plaintext(json.loads(plaintext.decode("utf-8")))


def list_folder(
    manifest: dict[str, Any],
    path: str,
    *,
    include_deleted: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return immediate subfolders and files for a browser path.

    ``path`` is a display path: ``""`` lists remote folders,
    ``"Documents"`` lists that remote folder's root, and
    ``"Documents/Invoices"`` lists a nested directory inside it.
    Deleted entries are hidden by default.
    """
    normalized = normalize_manifest_plaintext(manifest)
    parts = _split_path(path)
    if not parts:
        folders = [
            _folder_row(
                name=_folder_name(folder),
                path=_folder_name(folder),
                folder=folder,
                relative_path="",
            )
            for folder in _active_remote_folders(normalized)
        ]
        return _sort_rows(folders), []

    folder = _find_remote_folder(normalized, parts[0])
    if folder is None:
        raise KeyError(f"folder not found: {parts[0]}")

    relative_parts = parts[1:]
    entries = _iter_entries(folder, include_deleted=include_deleted)
    prefix = "/".join(relative_parts)
    prefix_with_slash = prefix + "/" if prefix else ""

    child_folder_names: set[str] = set()
    files: list[dict[str, Any]] = []

    for entry in entries:
        entry_parts = _split_path(str(entry.get("path", "")))
        if len(entry_parts) < len(relative_parts):
            continue
        if entry_parts[:len(relative_parts)] != relative_parts:
            continue
        remainder = entry_parts[len(relative_parts):]
        if not remainder:
            continue
        if len(remainder) > 1:
            child_folder_names.add(remainder[0])
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        files.append(_file_row(folder, entry, prefix_with_slash + remainder[0]))

    folder_rows = [
        _folder_row(
            name=name,
            path="/".join([parts[0], *relative_parts, name]),
            folder=folder,
            relative_path="/".join([*relative_parts, name]),
        )
        for name in child_folder_names
    ]
    return _sort_rows(folder_rows), _sort_rows(files)


def get_file(
    manifest: dict[str, Any],
    path: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any]:
    """Return one file entry by display path."""
    normalized = normalize_manifest_plaintext(manifest)
    parts = _split_path(path)
    if len(parts) < 2:
        raise KeyError(f"file not found: {path}")
    folder = _find_remote_folder(normalized, parts[0])
    if folder is None:
        raise KeyError(f"folder not found: {parts[0]}")
    relative_path = "/".join(parts[1:])
    for entry in _iter_entries(folder, include_deleted=include_deleted):
        if str(entry.get("type", "file")) != "file":
            continue
        if "/".join(_split_path(str(entry.get("path", "")))) == relative_path:
            return entry
    raise KeyError(f"file not found: {path}")


def list_versions(
    manifest: dict[str, Any],
    path: str,
    *,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Return one row per stored version of the file at ``path``.

    Rows are ordered newest-first by ``(modified_at, version_id)`` so the
    current/latest version is row 0. The latest version (the one
    referenced by ``latest_version_id``) is flagged ``is_current``;
    every other row is a previous version. Deleted-file entries are
    hidden by default; with ``include_deleted=True`` the version list is
    returned with the entry's ``deleted`` flag surfaced as ``is_deleted``
    on every row.
    """
    entry = get_file(manifest, path, include_deleted=include_deleted)
    versions = _versions(entry)
    latest_id = str(entry.get("latest_version_id") or "")
    name = _split_path(str(entry.get("path", "")))[-1] if entry.get("path") else ""

    rows: list[dict[str, Any]] = []
    for version in versions:
        version_id = str(version.get("version_id") or "")
        modified = str(
            version.get("modified_at") or version.get("created_at") or ""
        )
        rows.append({
            "version_id": version_id,
            "is_current": bool(version_id) and version_id == latest_id,
            "is_deleted": bool(entry.get("deleted")),
            "modified": modified,
            "created": str(version.get("created_at") or ""),
            "size": _int_value(version.get("logical_size")),
            "stored_size": _int_value(version.get("ciphertext_size")),
            "author_device_id": str(version.get("author_device_id") or ""),
            "name": name,
            "entry_id": str(entry.get("entry_id") or ""),
        })

    rows.sort(
        key=lambda row: (str(row["modified"]), str(row["version_id"])),
        reverse=True,
    )
    return rows


def _active_remote_folders(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    folders = manifest.get("remote_folders", [])
    if not isinstance(folders, list):
        return []
    return [
        folder for folder in folders
        if isinstance(folder, dict) and str(folder.get("state", "active")) == "active"
    ]


def _find_remote_folder(manifest: dict[str, Any], display_name: str) -> dict[str, Any] | None:
    for folder in _active_remote_folders(manifest):
        if _folder_name(folder) == display_name:
            return folder
    return None


def _iter_entries(
    folder: dict[str, Any],
    *,
    include_deleted: bool,
) -> list[dict[str, Any]]:
    entries = folder.get("entries", [])
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not include_deleted and bool(entry.get("deleted")):
            continue
        out.append(entry)
    return out


def _file_row(
    folder: dict[str, Any],
    entry: dict[str, Any],
    relative_path: str,
) -> dict[str, Any]:
    latest = _latest_version(entry)
    full_path = "/".join([_folder_name(folder), relative_path])
    name = _split_path(relative_path)[-1] if _split_path(relative_path) else relative_path
    return {
        "kind": "file",
        "name": name,
        "path": full_path,
        "remote_folder_id": str(folder.get("remote_folder_id", "")),
        "relative_path": relative_path,
        "entry": entry,
        "entry_id": str(entry.get("entry_id", "")),
        "size": _int_value(latest.get("logical_size") if latest else 0),
        "stored_size": _int_value(latest.get("ciphertext_size") if latest else 0),
        "modified": str(
            (latest or {}).get("modified_at")
            or (latest or {}).get("created_at")
            or entry.get("modified_at")
            or ""
        ),
        "versions": len(_versions(entry)),
        "status": "Deleted" if bool(entry.get("deleted")) else "Current",
        "latest_version_id": str(entry.get("latest_version_id") or ""),
        "deleted": bool(entry.get("deleted")),
        "deleted_at": str(entry.get("deleted_at") or ""),
        "recoverable_until": str(entry.get("recoverable_until") or ""),
    }


def _folder_row(
    *,
    name: str,
    path: str,
    folder: dict[str, Any],
    relative_path: str,
) -> dict[str, Any]:
    return {
        "kind": "folder",
        "name": name,
        "path": path,
        "remote_folder_id": str(folder.get("remote_folder_id", "")),
        "relative_path": relative_path,
        "status": "Folder",
    }


def _folder_name(folder: dict[str, Any]) -> str:
    return str(folder.get("display_name_enc") or "(unnamed folder)")


def _latest_version(entry: dict[str, Any]) -> dict[str, Any] | None:
    versions = _versions(entry)
    latest_id = str(entry.get("latest_version_id") or "")
    if latest_id:
        for version in versions:
            if str(version.get("version_id", "")) == latest_id:
                return version
    if versions:
        return versions[-1]
    return None


def _versions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    versions = entry.get("versions", [])
    if not isinstance(versions, list):
        return []
    return [version for version in versions if isinstance(version, dict)]


def _split_path(path: str) -> list[str]:
    return [part for part in str(path).replace("\\", "/").split("/") if part and part != "."]


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get("name", "")).casefold())


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
