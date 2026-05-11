"""Remote-folder mutations on top of the Vault publish flow.

Each method fetches the current head, mutates exactly one field, and
CAS-publishes the next revision. All three share the same shape:
fetch → bump revision + parent_revision + author + timestamp →
manifest helper → ``self.publish_manifest``.
"""

from .manifest import (
    add_remote_folder as manifest_add_remote_folder,
    generate_remote_folder_id,
    make_remote_folder,
    rename_remote_folder as manifest_rename_remote_folder,
)
from .canonical import _now_rfc3339
from .protocols import RelayProtocol


class RemoteFoldersMixin:
    def add_remote_folder(
        self,
        relay: RelayProtocol,
        *,
        display_name: str,
        ignore_patterns: list[str],
        author_device_id: str,
        created_at: str | None = None,
        remote_folder_id: str | None = None,
        local_index=None,
    ) -> dict:
        """Fetch head, append one remote folder, and publish a new revision."""
        name = str(display_name).strip()
        if not name:
            raise ValueError("folder name is required")

        current = self.fetch_manifest(relay, local_index=local_index)
        parent_revision = int(current["revision"])
        timestamp = created_at or _now_rfc3339()
        next_manifest = dict(current)
        next_manifest["revision"] = parent_revision + 1
        next_manifest["parent_revision"] = parent_revision
        next_manifest["created_at"] = timestamp
        next_manifest["author_device_id"] = str(author_device_id)

        folder = make_remote_folder(
            remote_folder_id=remote_folder_id or generate_remote_folder_id(),
            display_name_enc=name,
            created_at=timestamp,
            created_by_device_id=str(author_device_id),
            ignore_patterns=ignore_patterns,
        )
        updated = manifest_add_remote_folder(next_manifest, folder)
        return self.publish_manifest(relay, updated, local_index=local_index)

    def rename_remote_folder(
        self,
        relay: RelayProtocol,
        *,
        remote_folder_id: str,
        new_display_name: str,
        author_device_id: str,
        created_at: str | None = None,
        local_index=None,
    ) -> dict:
        """Fetch head, flip one folder's display_name_enc, CAS-publish (T4.5)."""
        name = str(new_display_name).strip()
        if not name:
            raise ValueError("folder name is required")

        current = self.fetch_manifest(relay, local_index=local_index)
        parent_revision = int(current["revision"])
        timestamp = created_at or _now_rfc3339()
        next_manifest = dict(current)
        next_manifest["revision"] = parent_revision + 1
        next_manifest["parent_revision"] = parent_revision
        next_manifest["created_at"] = timestamp
        next_manifest["author_device_id"] = str(author_device_id)

        updated = manifest_rename_remote_folder(next_manifest, remote_folder_id, name)
        return self.publish_manifest(relay, updated, local_index=local_index)

    def update_remote_folder_settings(
        self,
        relay: RelayProtocol,
        *,
        remote_folder_id: str,
        author_device_id: str,
        new_display_name: str | None = None,
        ignore_patterns: list[str] | None = None,
        created_at: str | None = None,
        local_index=None,
    ) -> dict:
        """Fetch head, edit display_name_enc and/or ignore_patterns, CAS-publish.

        Used by the Folders tab's Configure dialog so the user can
        change the folder's name and ignore patterns after creation —
        previously the patterns were locked in at first init.
        """
        from .manifest import update_remote_folder_settings as _update

        if new_display_name is None and ignore_patterns is None:
            raise ValueError(
                "update_remote_folder_settings: nothing to change",
            )

        current = self.fetch_manifest(relay, local_index=local_index)
        parent_revision = int(current["revision"])
        timestamp = created_at or _now_rfc3339()
        next_manifest = dict(current)
        next_manifest["revision"] = parent_revision + 1
        next_manifest["parent_revision"] = parent_revision
        next_manifest["created_at"] = timestamp
        next_manifest["author_device_id"] = str(author_device_id)

        updated = _update(
            next_manifest, remote_folder_id,
            new_display_name=new_display_name,
            ignore_patterns=ignore_patterns,
        )
        return self.publish_manifest(relay, updated, local_index=local_index)
