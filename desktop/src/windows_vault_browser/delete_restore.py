"""DeleteRestoreMixin — soft-delete + restore-version flows.

``_run_delete_worker`` is the shared worker that publishes a single
manifest mutation (delete file / delete folder / restore version);
each ``_confirm_*`` method gates on a destructive AlertDialog before
calling it.
"""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..vault.error_messages import humanize
from ..vault.binding.runtime import (
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault.ops.clear import confirm_folder_clear_text_matches
from ..vault.ui.time_format import format_local
from ..windows_vault.fresh_unlock_prompt import require_fresh_unlock_or_prompt


class DeleteRestoreMixin:
    """Soft-delete + restore-from-version. All publish via
    ``_run_delete_worker`` so the worker thread shape is identical."""

    def _run_delete_worker(
        self,
        *,
        label: str,
        mutate: Callable[[dict], dict],
    ) -> None:
        """Execute ``mutate(current_manifest)`` in a worker thread.

        ``mutate`` is expected to fetch the latest manifest, call into
        :mod:`vault_delete`, and return the published manifest.
        Thread-safe UI updates land via ``GLib.idle_add``.
        """
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        if self.delete_btn is not None:
            self.delete_btn.set_sensitive(False)
        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        # F-U03: delete is a single manifest mutation — no chunk loop
        # to interrupt — so progress is shown without a Cancel button.
        self._show_progress_no_cancel()
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text(label)
        self._set_status(f"{label}...")

        def worker() -> None:
            try:
                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    current_manifest = vault.fetch_unified_manifest(
                        relay, local_index=self.local_index,
                    )
                    published = mutate({
                        "vault": vault,
                        "relay": relay,
                        "manifest": current_manifest,
                    })
                finally:
                    vault.close()
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._render_all(f"{label} failed: {error_message}", "error")
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self.state.manifest = published
                self.state.selected_file = None
                self._disarm_cancel()
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                self._render_all(f"{label} succeeded.", "success")
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_delete_file(self, file_row: dict) -> None:
        from ..vault.ops.delete import delete_file

        remote_folder_id = str(file_row.get("remote_folder_id") or "")
        relative_path = str(file_row.get("relative_path") or "")
        if not remote_folder_id or not relative_path:
            self._set_status(
                "Cannot delete: missing folder/path metadata.", "error",
            )
            return

        display_path = str(file_row.get("path") or relative_path)
        dlg = Adw.AlertDialog(
            heading=f"Delete {display_path}?",
            body=(
                "This removes the file from the current remote view. Previous "
                "versions are kept for the retention period and can be restored."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dialog, response: str) -> None:
            if response != "delete":
                return
            self.config.reload()
            device_id = str(getattr(self.config, "device_id", "") or "0" * 32)

            def mutate(ctx: dict) -> dict:
                return delete_file(
                    vault=ctx["vault"], relay=ctx["relay"],
                    manifest=ctx["manifest"],
                    remote_folder_id=remote_folder_id,
                    remote_path=relative_path,
                    author_device_id=device_id,
                    local_index=self.local_index,
                )
            self._run_delete_worker(
                label=f"Deleting {display_path}",
                mutate=mutate,
            )

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _confirm_delete_folder(
        self, remote_folder_id: str, sub_path: str,
    ) -> None:
        """Vault Browser per-row "Delete folder contents" gate.

        Review §6.C4: this is the same destructive backend as the
        Danger tab's "Clear folder" (both call ``delete_folder_contents``
        which writes a tombstone per file under ``path_prefix``).
        The spec at ``docs/vault-architecture.md`` §13's destructive-
        action ledger requires typed-confirm + fresh-unlock on this
        op unconditionally — the pre-fix browser surface skipped both.
        Mirror tab_danger.py's clear-folder flow: fresh-unlock first,
        then an AlertDialog with a typed-confirm Entry seeded by the
        folder label.
        """
        from ..vault.ops.delete import delete_folder_contents

        # The §D9 typed-confirm uses the user-visible folder name, not
        # the rf_v1_… id. ``sub_path`` is empty when the user invokes
        # "Delete folder contents" on the folder root; in that case we
        # need the remote-folder display label. Fall back to the
        # remote-folder id when the model can't resolve the label so
        # we never present an empty confirm string (which would let
        # the user pass the gate with an empty entry).
        display_label = sub_path or self._resolve_folder_display_name(
            remote_folder_id,
        ) or remote_folder_id

        def proceed() -> None:
            self._open_delete_folder_confirm_dialog(
                remote_folder_id=remote_folder_id,
                sub_path=sub_path,
                display_label=display_label,
            )

        require_fresh_unlock_or_prompt(
            self.win,
            config=self.config,
            operation_label=f"delete contents of {display_label!r}",
            on_success=proceed,
        )

    def _open_delete_folder_confirm_dialog(
        self,
        *,
        remote_folder_id: str,
        sub_path: str,
        display_label: str,
    ) -> None:
        from ..vault.ops.delete import delete_folder_contents

        dlg = Adw.AlertDialog(
            heading=f"Delete contents of {display_label}?",
            body=(
                "⚠ Every file under this path becomes a tombstone. "
                "Previous versions stay until eviction or retention "
                "claims them. The folder binding itself stays "
                f"connected. Type {display_label!r} to confirm."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", f"Delete contents of {display_label}")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_enabled("delete", False)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        confirm_entry = Gtk.Entry(placeholder_text=display_label)

        def on_typed(_entry) -> None:
            ok = confirm_folder_clear_text_matches(
                confirm_entry.get_text(), display_label,
            )
            dlg.set_response_enabled("delete", ok)
        confirm_entry.connect("changed", on_typed)
        dlg.set_extra_child(confirm_entry)

        def on_response(_dialog, response: str) -> None:
            if response != "delete":
                return
            self.config.reload()
            device_id = str(getattr(self.config, "device_id", "") or "0" * 32)

            def mutate(ctx: dict) -> dict:
                published, _tombstoned = delete_folder_contents(
                    vault=ctx["vault"], relay=ctx["relay"],
                    manifest=ctx["manifest"],
                    remote_folder_id=remote_folder_id,
                    path_prefix=sub_path,
                    author_device_id=device_id,
                    local_index=self.local_index,
                )
                return published
            self._run_delete_worker(
                label=f"Deleting contents of {display_label}",
                mutate=mutate,
            )

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _resolve_folder_display_name(self, remote_folder_id: str) -> str | None:
        """Look up the user-visible folder name from the cached
        manifest so the typed-confirm Entry presents a label the user
        recognizes. Returns ``None`` when the manifest can't be
        consulted (caller falls back to the rf_v1_… id)."""
        try:
            manifest = getattr(self.state, "manifest", None) or {}
            for folder in manifest.get("remote_folders", []):
                if str(folder.get("remote_folder_id") or "") == remote_folder_id:
                    name = str(folder.get("display_name_enc") or "").strip()
                    if name:
                        return name
        except Exception:  # noqa: BLE001
            return None
        return None

    def _confirm_restore_version(self, file_row: dict, version: dict) -> None:
        from ..vault.ops.delete import restore_version_to_current

        remote_folder_id = str(file_row.get("remote_folder_id") or "")
        relative_path = str(file_row.get("relative_path") or "")
        source_version_id = str(version.get("version_id") or "")
        if not remote_folder_id or not relative_path or not source_version_id:
            self._set_status("Cannot restore: missing metadata.", "error")
            return

        display_path = str(file_row.get("path") or relative_path)
        modified = format_local(version.get("modified")) or "?"
        heading = f"Restore {display_path} to {modified}?"
        body = (
            "A new version will be added on top, pointing at this version's "
            "stored chunks. The previous current version stays in history."
        )
        if bool(file_row.get("deleted")):
            body = (
                "This file is currently deleted. Restoring lifts the tombstone "
                "and adds a new version on top of the chosen one."
            )
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("restore", "Restore")
        # F-U05: restore is rare and a relay-mutating action; default
        # to Cancel so bare Enter doesn't auto-confirm.
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dialog, response: str) -> None:
            if response != "restore":
                return
            self.config.reload()
            device_id = str(getattr(self.config, "device_id", "") or "0" * 32)

            def mutate(ctx: dict) -> dict:
                return restore_version_to_current(
                    vault=ctx["vault"], relay=ctx["relay"],
                    manifest=ctx["manifest"],
                    remote_folder_id=remote_folder_id,
                    remote_path=relative_path,
                    source_version_id=source_version_id,
                    author_device_id=device_id,
                    local_index=self.local_index,
                )
            self._run_delete_worker(
                label=f"Restoring {display_path}",
                mutate=mutate,
            )

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _confirm_restore_file(self, file_row: dict) -> None:
        """Restore a tombstoned file by promoting its latest version.

        The new version inherits the old version's chunk references —
        no new uploads — and clears the ``deleted`` flag. Restoring
        any file under a previously "all deleted" folder automatically
        un-deletes that folder in the browser view, since the
        ``deleted`` flag on folder rows is computed live from the
        descendants' tombstone state.
        """
        from ..vault.ops.delete import restore_version_to_current

        remote_folder_id = str(file_row.get("remote_folder_id") or "")
        relative_path = str(file_row.get("relative_path") or "")
        latest_version_id = str(file_row.get("latest_version_id") or "")
        if not remote_folder_id or not relative_path or not latest_version_id:
            self._set_status("Cannot restore: missing metadata.", "error")
            return

        display_path = str(file_row.get("path") or relative_path)
        dlg = Adw.AlertDialog(
            heading=f"Restore {display_path}?",
            body=(
                "Lifts the tombstone and re-publishes the file's last "
                "known version. Stored chunks are reused — no extra "
                "upload."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("restore", "Restore")
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dialog, response: str) -> None:
            if response != "restore":
                return
            self.config.reload()
            device_id = str(getattr(self.config, "device_id", "") or "0" * 32)

            def mutate(ctx: dict) -> dict:
                return restore_version_to_current(
                    vault=ctx["vault"], relay=ctx["relay"],
                    manifest=ctx["manifest"],
                    remote_folder_id=remote_folder_id,
                    remote_path=relative_path,
                    source_version_id=latest_version_id,
                    author_device_id=device_id,
                    local_index=self.local_index,
                )
            self._run_delete_worker(
                label=f"Restoring {display_path}",
                mutate=mutate,
            )

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _confirm_restore_folder(
        self, remote_folder_id: str, sub_path: str,
    ) -> None:
        """Bulk-restore every tombstoned file under ``sub_path``.

        Mirrors :func:`_confirm_delete_folder`. One sharded publish
        flips every tombstoned descendant back to live; the browser's
        ``deleted`` flag on folder rows is computed from the
        descendants, so the parent chain un-dims automatically.
        """
        from ..vault.ops.delete import restore_folder_contents

        target_label = sub_path or "this remote folder's contents"
        dlg = Adw.AlertDialog(
            heading=f"Restore contents of {target_label}?",
            body=(
                "Every tombstoned file under this path is re-published "
                "from its last-known version. Stored chunks are reused — "
                "no extra upload."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("restore", "Restore folder contents")
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dialog, response: str) -> None:
            if response != "restore":
                return
            self.config.reload()
            device_id = str(getattr(self.config, "device_id", "") or "0" * 32)

            def mutate(ctx: dict) -> dict:
                published, _restored = restore_folder_contents(
                    vault=ctx["vault"], relay=ctx["relay"],
                    manifest=ctx["manifest"],
                    remote_folder_id=remote_folder_id,
                    path_prefix=sub_path,
                    author_device_id=device_id,
                    local_index=self.local_index,
                )
                return published
            self._run_delete_worker(
                label=f"Restoring contents of {target_label}",
                mutate=mutate,
            )

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _confirm_and_delete(self, _btn: Gtk.Button) -> None:
        file_row = self.state.selected_file
        destination = self._resolve_upload_destination()
        if file_row and not bool(file_row.get("deleted")):
            self._confirm_delete_file(dict(file_row))
            return
        if destination is None:
            self._set_status(
                "Open a remote folder or select a file before deleting.",
                "error",
            )
            return
        remote_folder_id, sub_path = destination
        self._confirm_delete_folder(remote_folder_id, sub_path)
