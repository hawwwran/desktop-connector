"""Remote-folder mutations on top of the Vault publish flow.

Each method fetches the current root manifest, mutates exactly one
folder pointer (add / rename / settings), and CAS-publishes the next
root revision via ``publish_root_manifest``. All three share the
same shape: fetch root → bump revision + parent_revision + author +
timestamp → mutate pointer → publish_root_manifest. Returns a
synthesized unified manifest (no shards walked — folder-set + per-
folder metadata is the only thing the caller needs after these ops)
so existing callers' ``result["revision"]`` / ``result["remote_folders"]``
access keeps working.
"""

import copy

from .manifest import (
    assemble_unified_manifest,
    generate_remote_folder_id,
    make_root_folder_pointer,
    normalize_root_manifest_plaintext,
)
from .canonical import _now_rfc3339
from .protocols import RelayProtocol
from .state.op_log import append_op_log_entries, maybe_genesis_followup_entries


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
        """Fetch root, append one remote folder pointer, publish_root_manifest.

        Review §6.M2 — refuse a display_name that collides with an
        existing active (non-deleted) remote folder. Pre-fix two
        "Documents" folders could silently coexist; the UI dropdowns
        rendering them by name became ambiguous (the user couldn't
        tell which copy they were targeting). Comparison is
        case-insensitive + whitespace-trimmed to match what the
        eye sees. ``deleted=True`` pointers don't block — a folder
        can be tombstoned and a fresh one created under the same
        name.
        """
        name = str(display_name).strip()
        if not name:
            raise ValueError("folder name is required")

        timestamp = created_at or _now_rfc3339()
        new_pointer = make_root_folder_pointer(
            remote_folder_id=remote_folder_id or generate_remote_folder_id(),
            display_name_enc=name,
            created_at=timestamp,
            created_by_device_id=str(author_device_id),
            ignore_patterns=ignore_patterns,
        )

        normalized_name = name.casefold()

        def mutate(root: dict) -> dict:
            out = normalize_root_manifest_plaintext(root)
            existing_active_names: set[str] = set()
            for pointer in out["remote_folders"]:
                if not isinstance(pointer, dict):
                    continue
                if pointer.get("deleted"):
                    continue
                existing_name = str(pointer.get("display_name_enc", "")).strip()
                if existing_name:
                    existing_active_names.add(existing_name.casefold())
            if normalized_name in existing_active_names:
                raise ValueError(
                    f"a remote folder named {name!r} already exists; "
                    "rename or remove it before adding another."
                )
            out["remote_folders"] = list(out["remote_folders"]) + [new_pointer]
            return out

        return self._mutate_root_and_publish(
            relay,
            mutate=mutate,
            author_device_id=author_device_id,
            timestamp=timestamp,
            local_index=local_index,
        )

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
        """Fetch root, flip one folder's display_name_enc, CAS-publish (T4.5)."""
        name = str(new_display_name).strip()
        if not name:
            raise ValueError("folder name is required")
        timestamp = created_at or _now_rfc3339()

        def mutate(root: dict) -> dict:
            out = normalize_root_manifest_plaintext(root)
            found = False
            new_pointers = []
            for p in out["remote_folders"]:
                if p.get("remote_folder_id") == remote_folder_id:
                    new_pointers.append(_pointer_with(p, display_name_enc=name))
                    found = True
                else:
                    new_pointers.append(p)
            if not found:
                raise ValueError(f"unknown remote folder: {remote_folder_id}")
            out["remote_folders"] = new_pointers
            return out

        return self._mutate_root_and_publish(
            relay,
            mutate=mutate,
            author_device_id=author_device_id,
            timestamp=timestamp,
            local_index=local_index,
        )

    def ensure_folder_pointers_exist(
        self,
        relay: RelayProtocol,
        *,
        pointers: list[dict],
        author_device_id: str,
        created_at: str | None = None,
        local_index=None,
    ) -> dict:
        """Append any pointers whose ``remote_folder_id`` is missing from
        the active root; idempotent for pointers already present.

        Used by the vault import flow: ``merge_import_into`` may produce
        bundle-only folders (no pointer in the active root yet), and the
        per-folder shard publish requires the pointer to exist first.
        ``ensure_folder_pointers_exist`` runs once before the per-folder
        publish loop with the full set of merged folders; under
        concurrent imports the CAS-conflict path re-reads the root and
        re-checks which pointers still need adding (so a racing peer's
        partial creation is naturally absorbed).

        Returns a synthesized unified manifest (no shards walked).
        """
        if not pointers:
            return self.fetch_unified_manifest(relay, local_index=local_index)
        timestamp = created_at or _now_rfc3339()

        def mutate(root: dict) -> dict:
            out = normalize_root_manifest_plaintext(root)
            existing_ids = {
                str(p.get("remote_folder_id", ""))
                for p in out["remote_folders"]
                if isinstance(p, dict)
            }
            additions = [
                copy.deepcopy(ptr)
                for ptr in pointers
                if isinstance(ptr, dict)
                and str(ptr.get("remote_folder_id", "")) not in existing_ids
            ]
            if not additions:
                return out
            out["remote_folders"] = list(out["remote_folders"]) + additions
            return out

        return self._mutate_root_and_publish(
            relay,
            mutate=mutate,
            author_device_id=author_device_id,
            timestamp=timestamp,
            local_index=local_index,
        )

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
        """Fetch root, edit display_name_enc and/or ignore_patterns, CAS-publish.

        Used by the Folders tab's Configure dialog so the user can
        change the folder's name and ignore patterns after creation —
        previously the patterns were locked in at first init.
        """
        if new_display_name is None and ignore_patterns is None:
            raise ValueError(
                "update_remote_folder_settings: nothing to change",
            )
        timestamp = created_at or _now_rfc3339()
        normalized_name = (
            str(new_display_name).strip() if new_display_name is not None else None
        )
        if normalized_name == "":
            raise ValueError("folder name is required")

        def mutate(root: dict) -> dict:
            out = normalize_root_manifest_plaintext(root)
            found = False
            new_pointers = []
            for p in out["remote_folders"]:
                if p.get("remote_folder_id") != remote_folder_id:
                    new_pointers.append(p)
                    continue
                found = True
                edits: dict = {}
                if normalized_name is not None:
                    edits["display_name_enc"] = normalized_name
                if ignore_patterns is not None:
                    edits["ignore_patterns"] = list(ignore_patterns)
                new_pointers.append(_pointer_with(p, **edits))
            if not found:
                raise ValueError(f"unknown remote folder: {remote_folder_id}")
            out["remote_folders"] = new_pointers
            return out

        return self._mutate_root_and_publish(
            relay,
            mutate=mutate,
            author_device_id=author_device_id,
            timestamp=timestamp,
            local_index=local_index,
        )

    # ------------------------------------------------------------------

    def _mutate_root_and_publish(
        self,
        relay: RelayProtocol,
        *,
        mutate,
        author_device_id: str,
        timestamp: str,
        local_index,
    ) -> dict:
        """Phase H step 7a: fetch root, apply ``mutate``, bump revision,
        publish via ``publish_root_manifest``. Refreshes the local-index
        remote-folders cache from the published root so callers' next
        ``list_remote_folders`` read picks up the change immediately.
        Returns a synthesized unified manifest assembled from the
        published root with empty per-folder shard views — folder-
        mutating ops never read shard contents, and callers that need
        entry data fetch them separately via ``fetch_folder_shard``.

        WARNING: callers MUST NOT compute usage / file-count / any
        per-folder aggregation from the returned manifest — every
        folder's ``entries`` list is empty. Use
        ``vault.fetch_unified_manifest`` (or an async usage-refresh
        path) when entry data is needed.
        """
        current_root = self.fetch_root_manifest(relay, local_index=local_index)
        parent_root_revision = int(current_root.get("root_revision", 0))
        new_root_revision = parent_root_revision + 1
        candidate = mutate(current_root)
        candidate["root_revision"] = new_root_revision
        candidate["parent_root_revision"] = parent_root_revision
        candidate["created_at"] = timestamp
        candidate["author_device_id"] = str(author_device_id)
        # Plan D5: the genesis envelope is built with operation_log_tail=[];
        # the vault.create row lands on the first follow-up root publish.
        # `_mutate_root_and_publish` is the common first-followup path
        # (binding a folder during vault-onboard) so the row reliably
        # appears on the second revision rather than waiting until an
        # upload eventually fires.
        create_entries = maybe_genesis_followup_entries(
            current_root,
            new_revision=new_root_revision,
            device_id=author_device_id,
        )
        if create_entries:
            candidate["operation_log_tail"] = append_op_log_entries(
                candidate.get("operation_log_tail"),
                create_entries,
            )

        published_root = self.publish_root_manifest(relay, candidate, local_index=local_index)
        unified = assemble_unified_manifest(published_root, {})
        if local_index is not None:
            local_index.refresh_remote_folders_cache(unified)
        return unified


def _pointer_with(pointer: dict, **edits) -> dict:
    out = copy.deepcopy(pointer)
    out.update(edits)
    return out
