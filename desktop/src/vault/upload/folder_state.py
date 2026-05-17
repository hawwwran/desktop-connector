"""Sharded folder state helpers shared by the upload paths.

Mirrors the same shape ``binding/sync.py`` uses (``_BindingFolderState``,
``_fetch_folder_state``) — the helpers live here so the upload module
can read + publish the sharded manifest without importing from binding
(which would create a circular import: binding imports from upload).
The two copies are intentional transitional duplication; step 7 will
collapse them to a single shared module once the legacy unified-manifest
fallback is gone.
"""

from dataclasses import dataclass
from typing import Any

from ..manifest import make_folder_shard
from .protocols import UploadVault


@dataclass
class FolderState:
    """The vault root + a single binding's folder shard plaintext.

    A publish via ``publish_shard_with_root`` advances both the root
    revision (vault-wide) and this folder's shard revision (per-folder)
    atomically; the dataclass keeps them paired through the upload
    paths' CAS-retry loop.
    """
    root: dict[str, Any]
    shard: dict[str, Any]


def find_root_folder_pointer(
    root: dict[str, Any], remote_folder_id: str,
) -> dict[str, Any] | None:
    for pointer in root.get("remote_folders", []) or []:
        if isinstance(pointer, dict) and pointer.get("remote_folder_id") == remote_folder_id:
            return pointer
    return None


def fetch_folder_state(
    vault: UploadVault,
    relay: Any,
    remote_folder_id: str,
    author_device_id: str,
) -> FolderState:
    """Fetch the vault root + one binding's folder shard.

    Runs the §10.C hash-chain check on the shard via
    ``expected_shard_hash``. For a freshly-added folder pointer with no
    published shard yet (``shard_hash == ""``), synthesizes an empty
    shard at revision 0; the first publish bumps to revision 1.
    """
    root = vault.fetch_root_manifest(relay)
    pointer = find_root_folder_pointer(root, remote_folder_id)
    if pointer is None:
        raise ValueError(
            f"remote folder {remote_folder_id!r} has no pointer in the vault "
            "root (publish a root with the folder pointer before uploading)",
        )
    expected_hash = str(pointer.get("shard_hash", ""))
    if expected_hash == "":
        shard = make_folder_shard(
            vault_id=str(root.get("vault_id", "")),
            remote_folder_id=remote_folder_id,
            shard_revision=0,
            parent_shard_revision=0,
            created_at=str(pointer.get("created_at", "")),
            author_device_id=str(pointer.get("created_by_device_id", author_device_id)),
        )
    else:
        shard = vault.fetch_folder_shard(
            relay, remote_folder_id, expected_shard_hash=expected_hash,
        )
    return FolderState(root=root, shard=shard)
