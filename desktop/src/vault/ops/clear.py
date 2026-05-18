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

    Review §4.H3: loop the root fetch until it's stable. A concurrent
    device that added a folder mid-clear must still get tombstoned;
    pre-fix the single up-front fetch meant such folders were left
    live and the audit event under-reported. The loop terminates
    because (a) any folder we already cleared is idempotent on
    re-clear, (b) a malicious device that keeps adding folders would
    eventually hit the relay's create-rate-limit (review §1.H1).
    """
    total = 0
    seen_folders: set[str] = set()
    max_passes = 8  # defensive cap; in practice 1-2 passes suffice
    for pass_index in range(max_passes):
        root = vault.fetch_root_manifest(relay)
        new_folders: list[str] = []
        for pointer in root.get("remote_folders", []) or []:
            if not isinstance(pointer, dict):
                continue
            folder_id = str(pointer.get("remote_folder_id") or "")
            if not folder_id or folder_id in seen_folders:
                continue
            new_folders.append(folder_id)
        if not new_folders:
            break
        for folder_id in new_folders:
            seen_folders.add(folder_id)
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
    else:
        # Loop hit the defensive cap. Log so an operator can spot a
        # device that's racing the clear by spamming folder creates.
        log.warning(
            "vault.vault.clear_pass_cap_hit folders_seen=%d cap=%d",
            len(seen_folders), max_passes,
        )
    log.info(
        "vault.vault.cleared total_tombstoned=%d folders=%d author=%s",
        total, len(seen_folders), author_device_id,
    )
    return total


__all__ = [
    "VaultClearDangerError",
    "clear_folder",
    "clear_vault",
    "confirm_folder_clear_text_matches",
    "confirm_vault_clear_text_matches",
]
