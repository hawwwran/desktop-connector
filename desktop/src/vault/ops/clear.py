"""Clear-folder + clear-vault danger flows (T14.1, T14.2).

Two related operations, both gated by a "type the exact name + fresh
unlock" dialog per §gaps §13:

- :func:`clear_folder` — bulk-tombstones every non-deleted file entry
  in one remote folder by publishing a single per-folder shard
  revision (delegates to :func:`ops.delete.delete_folder_contents`
  with an empty ``path_prefix``).
- :func:`clear_vault` — same but iterates every remote folder in the
  vault root, publishing one shard revision per folder. Each per-
  folder publish is its own CAS attempt; on partial failure the
  caller can retry, and a re-run becomes a no-op on already-cleared
  folders.

The fresh-unlock check + dialog typing match live in the GTK layer;
this module owns the orchestration so it stays unit-testable.

§gaps §13 also requires the type-the-name confirmation step. The
helpers :func:`confirm_folder_clear_text_matches` and
:func:`confirm_vault_clear_text_matches` make the comparison
case-insensitive, whitespace-trimmed, and explicit-fail so a typo in
the dialog can't silently slip through.
"""

from __future__ import annotations

import logging
from typing import Any

from .delete import DeleteVault, delete_folder_contents


log = logging.getLogger(__name__)


class VaultClearDangerError(ValueError):
    """Raised when the dangerous-clear precondition isn't satisfied."""


def confirm_folder_clear_text_matches(
    typed: str, expected_folder_name: str,
) -> bool:
    """True iff the user typed the exact folder name (trimmed, exact case)."""
    if not isinstance(typed, str) or not isinstance(expected_folder_name, str):
        return False
    return typed.strip() == expected_folder_name.strip()


def confirm_vault_clear_text_matches(
    typed: str, expected_vault_id_dashed: str,
) -> bool:
    """True iff the user typed the full dashed Vault ID (case-insensitive)."""
    if not isinstance(typed, str) or not isinstance(expected_vault_id_dashed, str):
        return False
    return typed.strip().upper() == expected_vault_id_dashed.strip().upper()


def clear_folder(
    *,
    vault: DeleteVault,
    relay: Any,
    remote_folder_id: str,
    author_device_id: str,
    deleted_at: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Tombstone every live file in ``remote_folder_id``.

    Returns ``(published_unified_manifest, paths_tombstoned)``. The
    second element is the list of paths that were actually tombstoned
    in the winning CAS attempt — useful for the UI count.
    """
    return delete_folder_contents(
        vault=vault,
        relay=relay,
        manifest={},  # ignored on the sharded path
        remote_folder_id=remote_folder_id,
        path_prefix="",
        author_device_id=author_device_id,
        deleted_at=deleted_at,
    )


def clear_vault(
    *,
    vault: DeleteVault,
    relay: Any,
    author_device_id: str,
    deleted_at: str | None = None,
) -> int:
    """Tombstone every live file in every folder.

    One sharded publish per folder. Returns the total number of paths
    tombstoned across all folders. A partial failure mid-loop leaves
    earlier folders cleared and later ones untouched; re-running is
    safe — already-tombstoned entries are skipped by
    ``tombstone_files_under_in_shard``.
    """
    root = vault.fetch_root_manifest(relay)
    total = 0
    for pointer in root.get("remote_folders", []) or []:
        if not isinstance(pointer, dict):
            continue
        folder_id = str(pointer.get("remote_folder_id") or "")
        if not folder_id:
            continue
        _published, tombstoned = delete_folder_contents(
            vault=vault,
            relay=relay,
            manifest={},
            remote_folder_id=folder_id,
            path_prefix="",
            author_device_id=author_device_id,
            deleted_at=deleted_at,
        )
        total += len(tombstoned)
    log.info(
        "vault.vault.cleared total_tombstoned=%d author=%s",
        total, author_device_id,
    )
    return total


__all__ = [
    "VaultClearDangerError",
    "clear_folder",
    "clear_vault",
    "confirm_folder_clear_text_matches",
    "confirm_vault_clear_text_matches",
]
