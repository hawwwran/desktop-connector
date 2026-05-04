"""Clear-folder + clear-vault danger flows (T14.1, T14.2).

Two related operations, both gated by a "type the exact name + fresh
unlock" dialog per §gaps §13:

- :func:`build_clear_folder_manifest` — bulk-tombstones every
  non-deleted file entry in one remote folder. Returns a new manifest
  with `revision = parent.revision + 1` and the tombstones applied;
  the caller publishes via the standard CAS path.
- :func:`build_clear_vault_manifest` — same but across every remote
  folder. The caller still wraps it in the dialog ("type the full
  Vault ID") + fresh-unlock guard.

Both functions are pure: deterministic given the inputs, no I/O. The
fresh-unlock check + dialog typing match live in the GTK layer; this
module owns the manifest mutation contract so it stays unit-testable.

§gaps §13 also requires the type-the-name confirmation step. The
helpers :func:`confirm_folder_clear_text_matches` and
:func:`confirm_vault_clear_text_matches` make the comparison
case-insensitive, whitespace-trimmed, and explicit-fail so a typo in
the dialog can't silently slip through.
"""

from __future__ import annotations

import logging
from typing import Any

from .vault_manifest import (
    normalize_manifest_path,
    normalize_manifest_plaintext,
    tombstone_file_entry,
)


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


def build_clear_folder_manifest(
    manifest: dict[str, Any],
    *,
    remote_folder_id: str,
    author_device_id: str,
    deleted_at: str,
) -> dict[str, Any]:
    """Return a manifest with every active file entry in the folder tombstoned.

    The bump from `manifest["revision"]` to `revision + 1` happens here
    so the caller doesn't have to remember to update parent_revision /
    revision / created_at. ``author_device_id`` is stamped on every
    tombstone AND on the manifest envelope.
    """
    head = normalize_manifest_plaintext(manifest)
    folder = _find_folder(head, remote_folder_id)
    if folder is None:
        raise KeyError(f"remote folder not found: {remote_folder_id}")

    parent_revision = int(head.get("revision", 0))
    new_revision = parent_revision + 1

    paths = _live_entry_paths(folder)
    if not paths:
        # Nothing to do — but still bump the revision so the caller can
        # publish a no-op revision with the audit details (§gaps §13).
        head["parent_revision"] = parent_revision
        head["revision"] = new_revision
        head["created_at"] = deleted_at
        head["author_device_id"] = author_device_id
        return head

    next_manifest = head
    for path in paths:
        next_manifest = tombstone_file_entry(
            next_manifest,
            remote_folder_id=remote_folder_id,
            path=path,
            deleted_at=deleted_at,
            author_device_id=author_device_id,
        )
    next_manifest["parent_revision"] = parent_revision
    next_manifest["revision"] = new_revision
    next_manifest["created_at"] = deleted_at
    next_manifest["author_device_id"] = author_device_id

    log.info(
        "vault.folder.cleared remote_folder_id=%s tombstoned=%d author=%s",
        remote_folder_id, len(paths), author_device_id,
    )
    return next_manifest


def build_clear_vault_manifest(
    manifest: dict[str, Any],
    *,
    author_device_id: str,
    deleted_at: str,
) -> dict[str, Any]:
    """Bulk-tombstone every active file entry across every remote folder.

    The caller is responsible for the dialog gate (typing the full
    Vault ID + fresh-unlock per §gaps §13).
    """
    head = normalize_manifest_plaintext(manifest)
    parent_revision = int(head.get("revision", 0))
    new_revision = parent_revision + 1

    next_manifest = head
    total_tombstoned = 0
    for folder in next_manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        folder_id = str(folder.get("remote_folder_id", ""))
        if not folder_id:
            continue
        paths = _live_entry_paths(folder)
        for path in paths:
            next_manifest = tombstone_file_entry(
                next_manifest,
                remote_folder_id=folder_id,
                path=path,
                deleted_at=deleted_at,
                author_device_id=author_device_id,
            )
            total_tombstoned += 1

    next_manifest["parent_revision"] = parent_revision
    next_manifest["revision"] = new_revision
    next_manifest["created_at"] = deleted_at
    next_manifest["author_device_id"] = author_device_id

    log.info(
        "vault.vault.cleared total_tombstoned=%d author=%s",
        total_tombstoned, author_device_id,
    )
    return next_manifest


def _find_folder(
    manifest: dict[str, Any], remote_folder_id: str,
) -> dict[str, Any] | None:
    for folder in manifest.get("remote_folders", []) or []:
        if (
            isinstance(folder, dict)
            and folder.get("remote_folder_id") == remote_folder_id
        ):
            return folder
    return None


def _live_entry_paths(folder: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for entry in folder.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "file")) != "file":
            continue
        if bool(entry.get("deleted")):
            continue
        path = str(entry.get("path") or "")
        if path:
            out.append(normalize_manifest_path(path))
    return out


__all__ = [
    "VaultClearDangerError",
    "build_clear_folder_manifest",
    "build_clear_vault_manifest",
    "confirm_folder_clear_text_matches",
    "confirm_vault_clear_text_matches",
]
