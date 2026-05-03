"""Vault usage calculations from decrypted manifests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .vault_manifest import normalize_manifest_plaintext


@dataclass(frozen=True)
class VaultUsageSummary:
    by_folder: dict[str, dict[str, int]]
    whole_vault_stored_bytes: int


def calculate_vault_usage(manifest: dict[str, Any]) -> VaultUsageSummary:
    """Calculate descriptive per-folder usage from a decrypted manifest.

    Whole-vault quota enforcement remains relay-authoritative. The
    whole-vault value here is only the client's unique-chunk view of
    the decrypted manifest and exists so tests can verify the T4.4
    dedup policy.
    """
    normalized = normalize_manifest_plaintext(manifest)
    by_folder: dict[str, dict[str, int]] = {}
    whole_vault_chunks: dict[str, int] = {}

    for folder in normalized["remote_folders"]:
        folder_id = str(folder["remote_folder_id"])
        current_chunks: dict[str, int] = {}
        history_chunks: dict[str, int] = {}
        current_logical_bytes = 0

        for entry in _iter_entries(folder):
            versions = _versions_by_id(entry)
            latest = _latest_version(entry, versions)
            is_current_entry = not bool(entry.get("deleted")) and latest is not None

            if is_current_entry and latest is not None:
                current_logical_bytes += _int_value(latest.get("logical_size"))
                _add_version_chunks(current_chunks, latest, whole_vault_chunks)

            latest_id = str(latest.get("version_id", "")) if latest else ""
            for version in versions.values():
                version_id = str(version.get("version_id", ""))
                if is_current_entry and version_id == latest_id:
                    continue
                _add_version_chunks(history_chunks, version, whole_vault_chunks)

        for chunk_id in current_chunks:
            history_chunks.pop(chunk_id, None)

        by_folder[folder_id] = {
            "current_bytes": current_logical_bytes,
            "stored_bytes": sum(current_chunks.values()),
            "history_bytes": sum(history_chunks.values()),
        }

    return VaultUsageSummary(
        by_folder=by_folder,
        whole_vault_stored_bytes=sum(whole_vault_chunks.values()),
    )


def _iter_entries(folder: dict[str, Any]) -> list[dict[str, Any]]:
    entries = folder.get("entries", [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _versions_by_id(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    versions = entry.get("versions", [])
    if not isinstance(versions, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for index, version in enumerate(versions):
        if not isinstance(version, dict):
            continue
        version_id = str(version.get("version_id") or f"__index_{index}")
        out[version_id] = version
    return out


def _latest_version(
    entry: dict[str, Any],
    versions: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    latest_id = str(entry.get("latest_version_id") or "")
    if latest_id and latest_id in versions:
        return versions[latest_id]
    if versions:
        return list(versions.values())[-1]
    return None


def _add_version_chunks(
    target: dict[str, int],
    version: dict[str, Any],
    whole_vault_chunks: dict[str, int],
) -> None:
    chunks = version.get("chunks", [])
    if not isinstance(chunks, list):
        return
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or f"__anonymous_{id(version)}_{index}")
        size = _int_value(chunk.get("ciphertext_size"))
        target.setdefault(chunk_id, size)
        whole_vault_chunks.setdefault(chunk_id, size)


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
