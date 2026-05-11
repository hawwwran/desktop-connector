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
from ..vault.ui.time_format import format_local


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
                    current_manifest = vault.fetch_manifest(
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
        from ..vault.ops.delete import delete_folder_contents

        target_label = sub_path or "this remote folder's contents"
        dlg = Adw.AlertDialog(
            heading=f"Delete contents of {target_label}?",
            body=(
                "Every file under this path becomes a tombstone. Previous "
                "versions stay until eviction or retention claims them."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete folder contents")
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

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
                label=f"Deleting contents of {target_label}",
                mutate=mutate,
            )

        dlg.connect("response", on_response)
        dlg.present(self.win)

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
