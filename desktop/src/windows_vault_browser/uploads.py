"""UploadsMixin — single-file + folder upload flows + conflict prompt.

Calls into ``..vault_upload`` for the actual streaming upload work.
Quota-exceeded handling is delegated to ``QuotaMixin`` via
``self._handle_quota_exceeded``; resume banner refresh delegates to
``ResumeBannerMixin``. Coupling resolves through MRO at runtime.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..vault_binding_lifecycle import SyncCancelledError
from ..vault_error_messages import humanize
from ..vault_relay_errors import VaultQuotaExceededError
from ..vault_runtime import (
    create_vault_relay,
    open_local_vault_from_grant,
)


class UploadsMixin:
    """Single-file + folder upload entry points."""

    def _start_upload(
        self,
        local_path: Path,
        remote_folder_id: str,
        sub_path: str,
        *,
        override_remote_path: str | None = None,
        upload_mode: str = "new_file_or_version",
    ) -> None:
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        remote_path = override_remote_path or (
            sub_path + "/" + local_path.name if sub_path else local_path.name
        )
        if self.upload_btn is not None:
            self.upload_btn.set_sensitive(False)
        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Preparing upload...")
        self._set_status(f"Uploading {local_path.name}...")

        def report_progress(progress) -> None:
            def update() -> bool:
                if self.progress_bar is None:
                    return False
                total = max(1, int(progress.total_chunks))
                fraction = (
                    1.0 if progress.phase == "done"
                    else progress.completed_chunks / total
                )
                self.progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                self.progress_bar.set_text(
                    f"{progress.completed_chunks}/{progress.total_chunks} chunks"
                )
                return False
            GLib.idle_add(update)

        def worker() -> None:
            try:
                from ..vault_upload import upload_file

                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    current_manifest = vault.fetch_manifest(
                        relay, local_index=self.local_index,
                    )
                    device_id = str(getattr(self.config, "device_id", "") or "0" * 32)
                    result = upload_file(
                        vault=vault,
                        relay=relay,
                        manifest=current_manifest,
                        local_path=local_path,
                        remote_folder_id=remote_folder_id,
                        remote_path=remote_path,
                        author_device_id=device_id,
                        mode=upload_mode,
                        progress=report_progress,
                        local_index=self.local_index,
                        should_continue=lambda: not cancel_event.is_set(),
                    )
                finally:
                    vault.close()
            except SyncCancelledError:
                def cancelled() -> bool:
                    self._disarm_cancel()
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(
                            self._resolve_upload_destination() is not None,
                        )
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status(
                        f"Upload cancelled: {local_path.name}. "
                        "Resume from the bell-banner anytime.",
                    )
                    self._refresh_resume_banner(vault_id)
                    return False
                GLib.idle_add(cancelled)
                return
            except VaultQuotaExceededError as exc:
                def fail() -> bool:
                    self._disarm_cancel()
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(
                            self._resolve_upload_destination() is not None,
                        )
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._handle_quota_exceeded(exc, action="Upload")
                    return False
                GLib.idle_add(fail)
                return
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(
                            self._resolve_upload_destination() is not None,
                        )
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status(f"Upload failed: {error_message}", "error")
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self.state.manifest = result.manifest
                self._disarm_cancel()
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                self.state.selected_file = None
                self._render_all()
                if result.skipped_identical:
                    self._set_status(
                        f"{remote_path} already has identical content — "
                        "no upload needed.",
                        "success",
                    )
                else:
                    self._set_status(
                        f"Uploaded {result.chunks_uploaded} chunks "
                        f"({result.bytes_uploaded} bytes) to {remote_path}.",
                        "success",
                    )
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    def _choose_upload_source(self, _btn: Gtk.Button) -> None:
        destination = self._resolve_upload_destination()
        if destination is None:
            self._set_status("Open a remote folder before uploading.", "error")
            return
        remote_folder_id, sub_path = destination

        file_dialog = Gtk.FileDialog()
        file_dialog.set_title("Upload file to vault")

        def on_source_chosen(file_dialog, result) -> None:
            try:
                gio_file = file_dialog.open_finish(result)
            except GLib.Error:
                return
            if gio_file is None:
                return
            path = gio_file.get_path()
            if not path:
                self._set_status("Choose a local file to upload.", "error")
                return
            local_path = Path(path)
            if not local_path.is_file():
                self._set_status("Selected entry is not a file.", "error")
                return
            self._maybe_prompt_conflict_then_upload(
                local_path, remote_folder_id, sub_path,
            )

        file_dialog.open(parent=self.win, callback=on_source_chosen)

    def _maybe_prompt_conflict_then_upload(
        self,
        local_path: Path,
        remote_folder_id: str,
        sub_path: str,
    ) -> None:
        from ..vault_upload import detect_path_conflict, make_conflict_renamed_path

        remote_path = (
            sub_path + "/" + local_path.name if sub_path else local_path.name
        )
        manifest = self.state.manifest
        if manifest is None or not detect_path_conflict(
            manifest, remote_folder_id, remote_path
        ):
            self._start_upload(local_path, remote_folder_id, sub_path)
            return

        dlg = Adw.AlertDialog(
            heading=f"{remote_path} already exists",
            body=(
                "A file with this name is already in the remote folder. "
                "Choose what to do — identical content is detected automatically "
                "and skipped, so this prompt only appears for new bytes."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("skip", "Skip")
        dlg.add_response("keep_both", "Keep both with rename")
        dlg.add_response("new_version", "Add as new version")
        dlg.set_default_response("new_version")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("new_version", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dialog, response: str) -> None:
            if response == "new_version":
                self._start_upload(local_path, remote_folder_id, sub_path)
            elif response == "keep_both":
                self.config.reload()
                device_name = str(getattr(self.config, "device_name", "") or "device")
                renamed = make_conflict_renamed_path(remote_path, device_name)
                new_sub_parts = [p for p in renamed.split("/") if p][:-1]
                new_sub_path = "/".join(new_sub_parts)
                self._start_upload(
                    local_path,
                    remote_folder_id,
                    new_sub_path,
                    override_remote_path=renamed,
                    upload_mode="new_file_only",
                )
            elif response == "skip":
                self._set_status(f"Skipped uploading {local_path.name}.", "dim-label")
            # "cancel" → fall through and do nothing.

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _start_folder_upload(
        self, local_root: Path, remote_folder_id: str, sub_path: str,
    ) -> None:
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        if self.upload_btn is not None:
            self.upload_btn.set_sensitive(False)
        if self.upload_folder_btn is not None:
            self.upload_folder_btn.set_sensitive(False)
        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Walking folder...")
        self._set_status(f"Uploading folder {local_root.name}...")

        def report_progress(folder_progress) -> None:
            def update() -> bool:
                if self.progress_bar is None:
                    return False
                if folder_progress.bytes_total > 0:
                    fraction = folder_progress.bytes_completed / folder_progress.bytes_total
                elif folder_progress.files_total > 0:
                    fraction = folder_progress.files_completed / max(
                        1, folder_progress.files_total,
                    )
                else:
                    fraction = 1.0
                self.progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                self.progress_bar.set_text(
                    f"{folder_progress.phase}: "
                    f"{folder_progress.files_completed}/{folder_progress.files_total} files"
                )
                return False
            GLib.idle_add(update)

        def worker() -> None:
            try:
                from ..vault_upload import upload_folder

                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    current_manifest = vault.fetch_manifest(
                        relay, local_index=self.local_index,
                    )
                    device_id = str(getattr(self.config, "device_id", "") or "0" * 32)
                    result = upload_folder(
                        vault=vault,
                        relay=relay,
                        manifest=current_manifest,
                        local_root=local_root,
                        remote_folder_id=remote_folder_id,
                        remote_sub_path=sub_path,
                        author_device_id=device_id,
                        progress=report_progress,
                        local_index=self.local_index,
                        should_continue=lambda: not cancel_event.is_set(),
                    )
                finally:
                    vault.close()
            except SyncCancelledError:
                def cancelled() -> bool:
                    self._disarm_cancel()
                    upload_dest = self._resolve_upload_destination()
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(upload_dest is not None)
                    if self.upload_folder_btn is not None:
                        self.upload_folder_btn.set_sensitive(upload_dest is not None)
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status(
                        f"Folder upload cancelled: {local_root.name}.",
                    )
                    return False
                GLib.idle_add(cancelled)
                return
            except VaultQuotaExceededError as exc:
                def fail() -> bool:
                    self._disarm_cancel()
                    upload_dest = self._resolve_upload_destination()
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(upload_dest is not None)
                    if self.upload_folder_btn is not None:
                        self.upload_folder_btn.set_sensitive(upload_dest is not None)
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._handle_quota_exceeded(exc, action="Folder upload")
                    return False
                GLib.idle_add(fail)
                return
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    upload_dest = self._resolve_upload_destination()
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(upload_dest is not None)
                    if self.upload_folder_btn is not None:
                        self.upload_folder_btn.set_sensitive(upload_dest is not None)
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status(
                        f"Folder upload failed: {error_message}", "error",
                    )
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self.state.manifest = result.manifest
                self._disarm_cancel()
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                self.state.selected_file = None
                self._render_all()
                skipped = len(result.skipped)
                self._set_status(
                    f"Uploaded {len(result.uploaded)} files "
                    f"({result.bytes_uploaded} bytes); skipped {skipped}.",
                    "success",
                )
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    def _choose_upload_folder_source(self, _btn: Gtk.Button) -> None:
        destination = self._resolve_upload_destination()
        if destination is None:
            self._set_status("Open a remote folder before uploading.", "error")
            return
        remote_folder_id, sub_path = destination

        file_dialog = Gtk.FileDialog()
        file_dialog.set_title("Upload folder to vault")

        def on_source_chosen(file_dialog, result) -> None:
            try:
                gio_file = file_dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if gio_file is None:
                return
            path = gio_file.get_path()
            if not path:
                self._set_status("Choose a local folder to upload.", "error")
                return
            local_root = Path(path)
            if not local_root.is_dir():
                self._set_status("Selected entry is not a folder.", "error")
                return
            self._start_folder_upload(local_root, remote_folder_id, sub_path)

        file_dialog.select_folder(parent=self.win, callback=on_source_chosen)
