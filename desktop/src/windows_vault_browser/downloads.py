"""DownloadsMixin — file/folder/version download flows.

Each public ``_choose_*`` method opens a Gtk.FileDialog, then either
delegates to the ``_prompt_existing_*`` overwrite/keep-both branch
or jumps straight to ``_start_*_download`` worker thread.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..vault_binding_lifecycle import SyncCancelledError
from ..vault_download import previous_version_filename
from ..vault_error_messages import humanize
from ..vault_runtime import (
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault.ui.time_format import format_local


class DownloadsMixin:
    """Owns destination selection + worker threads for downloads."""

    def _choose_download_destination(self, _btn: Gtk.Button) -> None:
        file_row = self.state.selected_file
        if not file_row and not self.state.path:
            self._set_status(
                "Open a remote folder before downloading a folder.", "error",
            )
            return
        if not file_row:
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title("Download folder")

            def on_folder_chosen(file_dialog, result) -> None:
                try:
                    gio_file = file_dialog.select_folder_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path = gio_file.get_path()
                if not path:
                    self._set_status("Choose a local folder destination.", "error")
                    return
                destination = (
                    Path(path) / self._download_folder_name(str(self.state.path))
                )
                if destination.exists():
                    self._prompt_existing_destination(destination, is_folder=True)
                else:
                    self._start_download(destination, "fail")

            file_dialog.select_folder(parent=self.win, callback=on_folder_chosen)
            return

        file_dialog = Gtk.FileDialog()
        file_dialog.set_title("Download file")
        file_dialog.set_initial_name(
            str(file_row.get("name") or "vault-download"),
        )

        def on_destination_chosen(file_dialog, result) -> None:
            try:
                gio_file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            if gio_file is None:
                return
            path = gio_file.get_path()
            if not path:
                self._set_status("Choose a local file destination.", "error")
                return
            destination = Path(path)
            if destination.exists():
                self._prompt_existing_destination(destination)
            else:
                self._start_download(destination, "fail")

        file_dialog.save(parent=self.win, callback=on_destination_chosen)

    def _prompt_existing_destination(
        self, destination: Path, *, is_folder: bool = False,
    ) -> None:
        dlg = Adw.AlertDialog(
            heading="Folder exists" if is_folder else "File exists",
            body=(
                "A folder with this name already exists. Overwrite replaces matching "
                "files but keeps unrelated local files."
                if is_folder
                else "Choose how to handle the selected destination."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("keep_both", "Keep both")
        dlg.add_response(
            "overwrite", "Overwrite matching files" if is_folder else "Overwrite",
        )
        dlg.set_default_response("keep_both")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dialog, response: str) -> None:
            if response == "overwrite":
                self._start_download(destination, "overwrite")
            elif response == "keep_both":
                self._start_download(destination, "keep_both")

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _start_download(self, destination: Path, existing_policy: str) -> None:
        file_row = self.state.selected_file
        folder_path = str(self.state.path)
        is_folder_download = file_row is None
        if is_folder_download and not folder_path:
            self._set_status(
                "Open a remote folder before downloading a folder.", "error",
            )
            return
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        selected_path = (
            folder_path if is_folder_download else str(file_row.get("path", ""))
        )
        download_label = "folder" if is_folder_download else selected_path
        if self.download_btn is not None:
            self.download_btn.set_sensitive(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Preparing download...")
        self._set_status(f"Downloading {download_label}...")

        def report_progress(progress) -> None:
            def update_progress() -> bool:
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

            GLib.idle_add(update_progress)

        def worker() -> None:
            try:
                from ..vault_download import (
                    default_vault_download_cache_dir,
                    download_folder,
                    download_latest_file,
                )

                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    current_manifest = vault.fetch_manifest(
                        relay, local_index=self.local_index,
                    )
                    if is_folder_download:
                        final_path = download_folder(
                            vault=vault,
                            relay=relay,
                            manifest=current_manifest,
                            path=selected_path,
                            destination=destination,
                            existing_policy=existing_policy,
                            chunk_cache_dir=default_vault_download_cache_dir(),
                            progress=report_progress,
                        )
                    else:
                        final_path = download_latest_file(
                            vault=vault,
                            relay=relay,
                            manifest=current_manifest,
                            path=selected_path,
                            destination=destination,
                            existing_policy=existing_policy,
                            chunk_cache_dir=default_vault_download_cache_dir(),
                            progress=report_progress,
                            should_continue=lambda: not cancel_event.is_set(),
                        )
                finally:
                    vault.close()
            except SyncCancelledError:
                def cancelled() -> bool:
                    self._disarm_cancel()
                    if self.download_btn is not None:
                        self.download_btn.set_sensitive(
                            self.state.selected_file is not None,
                        )
                    self._set_status(f"Download cancelled: {download_label}.")
                    return False
                GLib.idle_add(cancelled)
                return
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    if self.download_btn is not None:
                        self.download_btn.set_sensitive(
                            bool(self.state.selected_file) or bool(self.state.path),
                        )
                    self._set_status(
                        f"Download failed: {error_message}", "error",
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self.state.manifest = current_manifest
                self._disarm_cancel()
                if self.download_btn is not None:
                    self.download_btn.set_sensitive(
                        bool(self.state.selected_file) or bool(self.state.path),
                    )
                noun = "folder" if is_folder_download else "file"
                self._set_status(f"Downloaded {noun} to {final_path}.", "success")
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    def _choose_version_destination(self, file_row: dict, version: dict) -> None:
        base_name = str(file_row.get("name") or "vault-download")
        initial_name = previous_version_filename(base_name, version)

        file_dialog = Gtk.FileDialog()
        file_dialog.set_title("Download previous version")
        file_dialog.set_initial_name(initial_name)

        def on_destination_chosen(file_dialog, result) -> None:
            try:
                gio_file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            if gio_file is None:
                return
            path = gio_file.get_path()
            if not path:
                self._set_status("Choose a local file destination.", "error")
                return
            destination = Path(path)
            if destination.exists():
                self._prompt_existing_version_destination(file_row, version, destination)
            else:
                self._start_version_download(file_row, version, destination, "fail")

        file_dialog.save(parent=self.win, callback=on_destination_chosen)

    def _prompt_existing_version_destination(
        self, file_row: dict, version: dict, destination: Path,
    ) -> None:
        dlg = Adw.AlertDialog(
            heading="Version file exists",
            body=(
                "A file with this version's side-path name already exists. "
                "Choose how to handle it — the current file is never overwritten."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("keep_both", "Keep both")
        dlg.add_response("overwrite", "Overwrite")
        dlg.set_default_response("keep_both")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dialog, response: str) -> None:
            if response == "overwrite":
                self._start_version_download(
                    file_row, version, destination, "overwrite",
                )
            elif response == "keep_both":
                self._start_version_download(
                    file_row, version, destination, "keep_both",
                )

        dlg.connect("response", on_response)
        dlg.present(self.win)

    def _start_version_download(
        self,
        file_row: dict,
        version: dict,
        destination: Path,
        existing_policy: str,
    ) -> None:
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        file_path = str(file_row.get("path") or "")
        version_id = str(version.get("version_id") or "")
        if not file_path or not version_id:
            self._set_status("Cannot download this version.", "error")
            return

        label = file_row.get("name") or file_path
        modified = format_local(version.get("modified")) or "?"
        if self.download_btn is not None:
            self.download_btn.set_sensitive(False)
        if self.versions_btn is not None:
            self.versions_btn.set_sensitive(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Preparing version download...")
        self._set_status(f"Downloading {label} (version {modified})...")

        def report_progress(progress) -> None:
            def update_progress() -> bool:
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

            GLib.idle_add(update_progress)

        def worker() -> None:
            try:
                from ..vault_download import (
                    default_vault_download_cache_dir,
                    download_version,
                )

                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    current_manifest = vault.fetch_manifest(
                        relay, local_index=self.local_index,
                    )
                    final_path = download_version(
                        vault=vault,
                        relay=relay,
                        manifest=current_manifest,
                        path=file_path,
                        version_id=version_id,
                        destination=destination,
                        existing_policy=existing_policy,
                        chunk_cache_dir=default_vault_download_cache_dir(),
                        progress=report_progress,
                        should_continue=lambda: not cancel_event.is_set(),
                    )
                finally:
                    vault.close()
            except SyncCancelledError:
                def cancelled() -> bool:
                    self._disarm_cancel()
                    if self.download_btn is not None:
                        self.download_btn.set_sensitive(
                            bool(self.state.selected_file) or bool(self.state.path),
                        )
                    if self.versions_btn is not None:
                        self.versions_btn.set_sensitive(
                            bool(self.state.selected_file),
                        )
                    self._set_status(f"Version download cancelled: {label}.")
                    return False
                GLib.idle_add(cancelled)
                return
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    if self.download_btn is not None:
                        self.download_btn.set_sensitive(
                            bool(self.state.selected_file) or bool(self.state.path),
                        )
                    if self.versions_btn is not None:
                        self.versions_btn.set_sensitive(
                            bool(self.state.selected_file),
                        )
                    self._set_status(
                        f"Version download failed: {error_message}", "error",
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self.state.manifest = current_manifest
                self._disarm_cancel()
                if self.download_btn is not None:
                    self.download_btn.set_sensitive(
                        bool(self.state.selected_file) or bool(self.state.path),
                    )
                if self.versions_btn is not None:
                    self.versions_btn.set_sensitive(
                        bool(self.state.selected_file),
                    )
                self._set_status(
                    f"Downloaded version to {final_path}.", "success",
                )
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()
