"""Shared helpers for the vault test suite.

Lives at ``tests/protocol/_vault_helpers.py`` so it sits alongside
``_paths.py`` and follows the same "underscore prefix = test-internal"
convention. Imports from this module never touch real vault state —
the helpers are pure builders / lookups against the unified manifest
shape that the legacy ``make_manifest`` + ``make_remote_folder``
helpers used to produce, and that ``assemble_unified_manifest``
still produces today from the sharded primitives.

After the §3.5 sweep dropped the four legacy unified-shape builders,
8 test files had identical or near-identical local copies of these
helpers. Centralising here keeps the migration mechanical (each
test file's local helper turns into a one-line import or a thin
wrapper) without baking the unified shape back into the production
``manifest.py`` module.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    find_file_entry_in_shard,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)


def entry_in_unified(
    manifest: dict[str, Any],
    remote_folder_id: str,
    path: str,
) -> dict[str, Any] | None:
    """Walk a unified-shape manifest to ``remote_folder_id``'s folder
    and look up the file entry at ``path`` via the shard helper.

    Drop-in replacement for the dropped ``find_file_entry`` API.
    Returns the folder's entry dict or ``None`` if either the folder
    or the path is absent.
    """
    folder = next(
        (
            f for f in manifest.get("remote_folders", []) or []
            if f.get("remote_folder_id") == remote_folder_id
        ),
        None,
    )
    if folder is None:
        return None
    return find_file_entry_in_shard(folder, path)


def empty_single_folder_unified(
    *,
    vault_id: str,
    remote_folder_id: str,
    author_device_id: str,
    created_at: str,
    display_name_enc: str = "Documents",
) -> dict[str, Any]:
    """Build an empty single-folder unified manifest via the sharded
    primitives + ``assemble_unified_manifest``.

    Drop-in replacement for the most common ``_empty_manifest`` /
    ``_empty_unified`` body across test files. Always builds a fresh
    genesis revision (root_revision=1 / shard_revision=1).
    """
    root = make_root_manifest(
        vault_id=vault_id,
        root_revision=1, parent_root_revision=0,
        created_at=created_at,
        author_device_id=author_device_id,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=remote_folder_id,
                display_name_enc=display_name_enc,
                created_at=created_at,
                created_by_device_id=author_device_id,
            ),
        ],
    )
    shard = make_folder_shard(
        vault_id=vault_id, remote_folder_id=remote_folder_id,
        shard_revision=1, parent_shard_revision=0,
        created_at=created_at,
        author_device_id=author_device_id,
        entries=[],
    )
    return assemble_unified_manifest(root, {remote_folder_id: shard})
