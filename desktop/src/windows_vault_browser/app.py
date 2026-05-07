"""Vault browser — structural refactor of the original windows_vault_browser.py.

Closures from the original ``on_activate`` body are lifted onto a
``VaultBrowser`` class so each piece of state is reachable via
``self.*`` instead of captured in a nested function. Built up over
five passes (introduction → file list → detail pane → downloads →
uploads/delete/quota/resume) verified side-by-side against the
original module before that module was removed.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..brand import (  # noqa: E402
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..vault_binding_lifecycle import SyncCancelledError  # noqa: E402
from ..vault_browser_model import list_folder, list_versions  # noqa: E402
from ..vault_download import previous_version_filename  # noqa: E402
from ..vault_error_messages import humanize  # noqa: E402
from ..vault_local_index import VaultLocalIndex  # noqa: E402
from ..vault_relay_errors import VaultQuotaExceededError  # noqa: E402
from ..vault_runtime import (  # noqa: E402
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault_time_format import format_local  # noqa: E402
from ..vault_upload import (  # noqa: E402
    default_upload_resume_dir,
    describe_quota_exceeded,
    list_resumable_sessions,
)
from ..windows_common import _format_bytes, _make_app  # noqa: E402
from .state import BrowserState

log = logging.getLogger(__name__)


def show_vault_browser(
    config_dir: Path,
    vault_id_override: str | None = None,
) -> None:
    """Run the vault browser as a subprocess window.

    ``vault_id_override`` (F-U14): optional 12-char canonical vault id.
    When present, every per-action ``self._resolve_vault_id()`` call
    returns this instead of reading ``config['vault']['last_known_id']``.
    Lets a future multi-vault tray repoint the browser at a specific
    vault without rewriting config on disk.
    """
    from ..config import Config

    browser = VaultBrowser(
        config_dir=config_dir,
        config=Config(config_dir),
        vault_id_override=vault_id_override,
    )
    browser.run()


class VaultBrowser:
    """Owns the entire browser window: state, widgets, and async hooks.

    Built incrementally — pass 1 covers the window shell + tree pane +
    manifest refresh. Subsequent passes will add file list, detail
    pane, downloads, uploads, quota, resume, and the cancel/progress
    cluster. The same shape that v1 used (one ``on_activate`` closure
    body + helper closures) is preserved here as ``_on_activate`` +
    a flat list of ``self._render_*`` / ``self._on_*`` methods.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        config,
        vault_id_override: str | None,
    ) -> None:
        from ..vault_window_args import resolve_active_vault_id

        self.config_dir = Path(config_dir)
        self.config = config
        self.vault_id_override = vault_id_override
        self._resolve_vault_id = (
            lambda: resolve_active_vault_id(self.config, self.vault_id_override)
        )
        self.local_index = VaultLocalIndex(self.config_dir)
        self.state = BrowserState()

        # Widget handles populated in ``_on_activate``. ``Optional``
        # everywhere so a method called before activation fails clean
        # rather than NameError-ing a missing closure capture.
        self._app: Adw.Application | None = None
        self.win: Adw.ApplicationWindow | None = None
        self.outer: Gtk.Box | None = None
        self.action_bar: Gtk.Box | None = None
        self.back_btn: Gtk.Button | None = None
        self.forward_btn: Gtk.Button | None = None
        self.refresh_btn: Gtk.Button | None = None
        self.upload_btn: Gtk.Button | None = None
        self.upload_folder_btn: Gtk.Button | None = None
        self.delete_btn: Gtk.Button | None = None
        self.versions_btn: Gtk.Button | None = None
        self.download_btn: Gtk.Button | None = None
        self.show_deleted_toggle: Gtk.CheckButton | None = None
        self.resume_banner: Adw.Banner | None = None
        self.quota_banner: Adw.Banner | None = None
        self.breadcrumb: Gtk.Label | None = None
        self.status_label: Gtk.Label | None = None
        self.progress_box: Gtk.Box | None = None
        self.progress_bar: Gtk.ProgressBar | None = None
        self.cancel_btn: Gtk.Button | None = None
        self.tree_box: Gtk.Box | None = None
        self.list_grid: Gtk.Grid | None = None
        self.detail_box: Gtk.Box | None = None

        # F-U03: workers running long-flow operations (download / etc)
        # register their ``threading.Event`` here before starting and
        # clear it in their ``finally``. The Cancel button reads the
        # slot at click time and sets the event; backend hooks observe
        # ``should_continue() == False`` at the next checkpoint and
        # raise ``SyncCancelledError``.
        self._active_cancel: threading.Event | None = None

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self._app = _make_app()
        self._app.connect("activate", self._on_activate)
        self._app.run([])

    def _on_activate(self, app: Adw.Application) -> None:
        apply_brand_css()
        apply_theme_mode_from_config_dir(self.config_dir)

        self.win = Adw.ApplicationWindow(
            application=app,
            title="Vault",
            default_width=1040,
            default_height=680,
        )
        toolbar = Adw.ToolbarView()
        self.win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        self.outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        toolbar.set_content(self.outer)

        self._build_action_bar()
        self._build_breadcrumb_and_status()
        self._build_panes()

        try:
            apply_pointer_cursors(self.win)
        except Exception:
            log.debug("apply_pointer_cursors failed", exc_info=True)

        # F-LT04: pull the manifest when the window regains focus so a
        # publish from another process (Sync now in Settings, background
        # filesystem watcher) shows up without a manual Refresh click.
        # We skip if the in-flight refresh has the Refresh button
        # disabled — that path will land its own re-render.
        self.win.connect("notify::is-active", self._refresh_on_focus)

        self.win.present()
        # Kick the initial manifest fetch on entry so the user sees the
        # tree populate without a manual click.
        self._refresh_manifest_async()

    def _refresh_on_focus(self, window, _pspec) -> None:
        if not window.get_property("is-active"):
            return
        if self.refresh_btn is None or not self.refresh_btn.get_sensitive():
            return
        self._refresh_manifest_async()

    # ------------------------------------------------------------------ layout
    def _build_action_bar(self) -> None:
        assert self.outer is not None
        self.action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.outer.append(self.action_bar)

        self.back_btn = Gtk.Button(label="Back", css_classes=["pill"])
        self.forward_btn = Gtk.Button(label="Forward", css_classes=["pill"])
        self.refresh_btn = Gtk.Button(label="Refresh", css_classes=["pill"])
        self.upload_btn = Gtk.Button(label="Upload", css_classes=["pill"])
        self.upload_folder_btn = Gtk.Button(
            label="Upload folder", css_classes=["pill"],
        )
        self.delete_btn = Gtk.Button(
            label="Delete", css_classes=["pill", "destructive-action"],
        )
        self.versions_btn = Gtk.Button(label="Versions", css_classes=["pill"])
        self.download_btn = Gtk.Button(
            label="Download", css_classes=["pill", "suggested-action"],
        )

        self.back_btn.connect("clicked", self._on_back_clicked)
        self.forward_btn.connect("clicked", self._on_forward_clicked)
        self.refresh_btn.connect("clicked", lambda _btn: self._refresh_manifest_async())
        self.upload_btn.connect("clicked", self._choose_upload_source)
        self.upload_folder_btn.connect("clicked", self._choose_upload_folder_source)
        self.delete_btn.connect("clicked", self._confirm_and_delete)
        self.download_btn.connect("clicked", self._choose_download_destination)

        self.upload_btn.set_sensitive(False)
        self.upload_folder_btn.set_sensitive(False)
        self.delete_btn.set_sensitive(False)
        self.versions_btn.set_sensitive(False)
        self.download_btn.set_sensitive(False)
        self.upload_btn.set_tooltip_text(
            "Open a remote folder, then click Upload to add a file",
        )
        self.upload_folder_btn.set_tooltip_text(
            "Open a remote folder, then click to upload a local folder recursively",
        )
        self.delete_btn.set_tooltip_text(
            "Soft-delete the selected file or current folder",
        )
        self.versions_btn.set_tooltip_text("Choose a version below to download")
        self.download_btn.set_tooltip_text(
            "Download selected file or current folder",
        )

        for button in (
            self.back_btn,
            self.forward_btn,
            self.refresh_btn,
            self.upload_btn,
            self.upload_folder_btn,
            self.delete_btn,
            self.versions_btn,
            self.download_btn,
        ):
            self.action_bar.append(button)

        self.show_deleted_toggle = Gtk.CheckButton(label="Show deleted")
        self.show_deleted_toggle.set_tooltip_text(
            "Reveal soft-deleted files; they stay until eviction or "
            "retention claims them.",
        )
        self.show_deleted_toggle.connect("toggled", self._on_show_deleted_toggled)
        self.action_bar.append(self.show_deleted_toggle)

        self._update_nav_buttons()

    def _build_breadcrumb_and_status(self) -> None:
        assert self.outer is not None
        # Resume + quota banners sit above the breadcrumb (v1 lines
        # 129–136). Both start hidden; ``_refresh_resume_banner`` and
        # ``_handle_quota_exceeded`` reveal them when their state
        # actually fires.
        self.resume_banner = Adw.Banner.new("")
        self.resume_banner.set_button_label("Resume")
        self.resume_banner.set_revealed(False)
        self.resume_banner.connect("button-clicked", self._start_resume_pending)
        self.outer.append(self.resume_banner)

        self.quota_banner = Adw.Banner.new("")
        self.quota_banner.set_revealed(False)
        self.outer.append(self.quota_banner)

        self.breadcrumb = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.breadcrumb.add_css_class("title-4")
        self.outer.append(self.breadcrumb)

        self.status_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self.outer.append(self.status_label)

        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.progress_box.set_visible(False)
        self.progress_bar = Gtk.ProgressBar(show_text=True, hexpand=True)
        self.progress_box.append(self.progress_bar)
        self.cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
        self.cancel_btn.connect("clicked", self._on_cancel_clicked)
        self.progress_box.append(self.cancel_btn)
        self.outer.append(self.progress_box)

    def _build_panes(self) -> None:
        assert self.outer is not None
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        self.outer.append(paned)

        # Left: tree pane (ported in pass 1).
        tree_scroller = Gtk.ScrolledWindow(min_content_width=160)
        self.tree_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        tree_scroller.set_child(self.tree_box)
        paned.set_start_child(tree_scroller)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(True)

        # Right: split between file list (center) and detail (right).
        # Both panes are placeholders in pass 1.
        right = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_end_child(right)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(True)
        paned.set_position(220)

        list_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.list_grid = Gtk.Grid(
            column_spacing=18,
            row_spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
            hexpand=True,
            vexpand=True,
        )
        list_scroller.set_child(self.list_grid)
        right.set_start_child(list_scroller)
        right.set_resize_start_child(True)
        right.set_shrink_start_child(True)

        detail_scroller = Gtk.ScrolledWindow(min_content_width=200)
        self.detail_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=8,
        )
        detail_scroller.set_child(self.detail_box)
        right.set_end_child(detail_scroller)
        right.set_resize_end_child(False)
        right.set_shrink_end_child(True)
        right.set_position(540)

    # ------------------------------------------------------------------ small helpers
    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    @staticmethod
    def _clear_grid(grid: Gtk.Grid) -> None:
        child = grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            grid.remove(child)
            child = nxt

    def _set_status(self, message: str, css_class: str = "dim-label") -> None:
        if self.status_label is None:
            return
        for klass in ("dim-label", "error", "success"):
            self.status_label.remove_css_class(klass)
        self.status_label.add_css_class(css_class)
        self.status_label.set_label(message)

    def _current_path_label(self) -> str:
        path = str(self.state.path)
        return "Vault" if not path else "Vault / " + path.replace("/", " / ")

    def _update_nav_buttons(self) -> None:
        if self.back_btn is not None:
            self.back_btn.set_sensitive(bool(self.state.back))
        if self.forward_btn is not None:
            self.forward_btn.set_sensitive(bool(self.state.forward))

    # ------------------------------------------------------------------ render
    def _render_all(self, message: str | None = None, css_class: str = "dim-label") -> None:
        if self.breadcrumb is not None:
            self.breadcrumb.set_label(self._current_path_label())
        self._update_nav_buttons()
        self._render_tree()
        self._render_file_list()
        # Download / Versions sensitivity is owned by ``_render_detail``
        # itself (mirrors v1 lines 305 / 332 / 299 / 414) — calling
        # render_detail here reapplies the right state for the current
        # selection-or-folder context.
        self._render_detail(self.state.selected_file)
        # Upload / Delete sensitivity (v1 lines 584–595): both depend
        # on whether the current location is inside a remote folder
        # (so ``_resolve_upload_destination`` returns a tuple); Delete
        # additionally enables when a non-tombstoned file is selected.
        upload_destination = self._resolve_upload_destination()
        if self.upload_btn is not None:
            self.upload_btn.set_sensitive(upload_destination is not None)
        if self.upload_folder_btn is not None:
            self.upload_folder_btn.set_sensitive(upload_destination is not None)
        if self.delete_btn is not None:
            selected_file = self.state.selected_file or {}
            can_delete_file = (
                bool(selected_file)
                and not bool(selected_file.get("deleted"))
            )
            can_delete_folder = upload_destination is not None
            self.delete_btn.set_sensitive(can_delete_file or can_delete_folder)
        if message is not None:
            self._set_status(message, css_class)

    # ------------------------------------------------------------------ upload destination resolver
    def _resolve_upload_destination(self) -> tuple[str, str] | None:
        """Return (remote_folder_id, sub_path) for the current location, or None.

        Returned tuple feeds the upload + delete dispatch — both need
        to know which active remote folder we're inside, plus the
        path remainder under it.
        """
        manifest = self.state.manifest
        path = str(self.state.path or "")
        if not manifest or not path:
            return None
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None
        head, *rest = parts
        for folder in manifest.get("remote_folders", []) or []:
            if not isinstance(folder, dict):
                continue
            if str(folder.get("state", "active")) != "active":
                continue
            if str(folder.get("display_name_enc", "")) == head:
                return str(folder["remote_folder_id"]), "/".join(rest)
        return None

    # ------------------------------------------------------------------ cancel + progress
    def _arm_cancel(self, event: threading.Event) -> None:
        """Worker calls this just before kicking off a long-running backend.

        The Cancel button becomes clickable; on click it sets the
        event and the backend's next ``should_continue`` checkpoint
        raises ``SyncCancelledError``.
        """
        self._active_cancel = event
        if self.cancel_btn is not None:
            self.cancel_btn.set_label("Cancel")
            self.cancel_btn.set_sensitive(True)
            self.cancel_btn.set_visible(True)
        if self.progress_box is not None:
            self.progress_box.set_visible(True)

    def _disarm_cancel(self) -> None:
        """Worker calls this in its ``finally`` (via ``GLib.idle_add``)."""
        self._active_cancel = None
        if self.progress_box is not None:
            self.progress_box.set_visible(False)
        if self.cancel_btn is not None:
            self.cancel_btn.set_label("Cancel")
            self.cancel_btn.set_sensitive(True)
            self.cancel_btn.set_visible(True)

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        event = self._active_cancel
        if event is None:
            return
        event.set()
        if self.cancel_btn is not None:
            self.cancel_btn.set_sensitive(False)
            self.cancel_btn.set_label("Cancelling…")

    def _on_show_deleted_toggled(self, button: Gtk.CheckButton) -> None:
        self.state.show_deleted = bool(button.get_active())
        # v1 line 1820: also clear the selection so a tombstoned row
        # doesn't keep its detail pane after the toggle hides it.
        self.state.selected_file = None
        self._render_all()

    # ------------------------------------------------------------------ download paths
    @staticmethod
    def _download_folder_name(path: str) -> str:
        parts = [
            part for part in str(path).replace("\\", "/").split("/")
            if part and part != "."
        ]
        return parts[-1] if parts else "Vault"

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

    def _render_tree(self) -> None:
        if self.tree_box is None:
            return
        self._clear_box(self.tree_box)
        root = Gtk.Button(label="Vault", halign=Gtk.Align.START)
        root.add_css_class("flat")
        root.connect("clicked", lambda _btn: self._navigate_to(""))
        self.tree_box.append(root)

        manifest = self.state.manifest
        if not manifest:
            return

        def add_path_button(path: str, depth: int) -> None:
            assert self.tree_box is not None
            name = path.split("/")[-1] if path else "Vault"
            button = Gtk.Button(
                label=("  " * depth) + name, halign=Gtk.Align.START,
            )
            button.add_css_class("flat")
            button.connect(
                "clicked", lambda _btn, p=path: self._navigate_to(p),
            )
            self.tree_box.append(button)

        def walk(path: str, depth: int) -> None:
            try:
                children, _files = list_folder(manifest, path)
            except Exception:
                return
            for child in children:
                child_path = str(child["path"])
                add_path_button(child_path, depth)
                walk(child_path, depth + 1)

        walk("", 1)

    # ------------------------------------------------------------------ file list (center pane)
    def _attach_cell(self, widget: Gtk.Widget, col: int, row: int) -> None:
        assert self.list_grid is not None
        self.list_grid.attach(widget, col, row, 1, 1)

    def _attach_label(
        self, text: str, col: int, row: int, *, header: bool = False,
    ) -> None:
        label = Gtk.Label(label=text, xalign=0, hexpand=(col == 0))
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if header:
            label.add_css_class("dim-label")
        self._attach_cell(label, col, row)

    def _select_file(self, file_row: dict) -> None:
        self.state.selected_file = file_row
        self._render_detail(file_row)

    def _render_file_list(self) -> None:
        if self.list_grid is None:
            return
        self._clear_grid(self.list_grid)
        for col, title in enumerate(
            ("Name", "Size", "Modified", "Versions", "Status"),
        ):
            self._attach_label(title, col, 0, header=True)

        manifest = self.state.manifest
        if not manifest:
            self._attach_label("Open or refresh a vault to browse files.", 0, 1)
            return

        include_deleted = self.state.show_deleted
        try:
            folders, files = list_folder(
                manifest, str(self.state.path), include_deleted=include_deleted,
            )
        except Exception as exc:
            self._attach_label(f"Could not list this folder: {exc}", 0, 1)
            return

        row = 1
        for folder in folders:
            button = Gtk.Button(
                label=str(folder["name"]), halign=Gtk.Align.START,
            )
            button.add_css_class("flat")
            button.connect(
                "clicked", lambda _btn, p=folder["path"]: self._navigate_to(str(p)),
            )
            self._attach_cell(button, 0, row)
            self._attach_label("-", 1, row)
            self._attach_label("-", 2, row)
            self._attach_label("-", 3, row)
            self._attach_label("Folder", 4, row)
            row += 1

        for file_row in files:
            deleted = str(file_row.get("status", "")) == "Deleted"
            button = Gtk.Button(
                label=str(file_row["name"]), halign=Gtk.Align.START,
            )
            button.add_css_class("flat")
            if deleted:
                button.add_css_class("dim-label")
            button.connect(
                "clicked", lambda _btn, f=file_row: self._select_file(dict(f)),
            )
            self._attach_cell(button, 0, row)
            size_label = _format_bytes(int(file_row.get("size", 0)))
            self._attach_label(size_label, 1, row)
            self._attach_label(format_local(file_row.get("modified")) or "-", 2, row)
            self._attach_label(str(file_row.get("versions", 0)), 3, row)
            status_label = str(file_row.get("status", ""))
            if deleted:
                recoverable = format_local(file_row.get("recoverable_until"))
                if recoverable:
                    status_label = f"Deleted — recoverable until {recoverable}"
            self._attach_label(status_label, 4, row)
            row += 1

        if row == 1:
            if self.state.path:
                self._attach_label(
                    "Folder is empty — drag files here or click Upload", 0, 1,
                )
            else:
                self._attach_label("No remote folders yet.", 0, 1)

    # ------------------------------------------------------------------ detail pane (right)
    def _render_detail(self, file_row: dict | None) -> None:
        if self.detail_box is None:
            return
        self._clear_box(self.detail_box)
        # v1 line 299: clear Versions sensitivity at the top of every
        # render; the versions section flips it back on if/when it
        # finds history. Same shape preserved here.
        if self.versions_btn is not None:
            self.versions_btn.set_sensitive(False)

        if not file_row:
            self.detail_box.append(Gtk.Label(
                label="Details", xalign=0, css_classes=["title-3"],
            ))
            current_path = str(self.state.path)
            if self.download_btn is not None:
                self.download_btn.set_sensitive(bool(current_path))
            if current_path:
                self.detail_box.append(Gtk.Label(
                    label="Current folder",
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                ))
                self.detail_box.append(Gtk.Label(
                    label=current_path,
                    xalign=0,
                    wrap=True,
                ))
                self.detail_box.append(Gtk.Label(
                    label="Download saves this folder recursively.",
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                ))
                return
            self.detail_box.append(Gtk.Label(
                label="No file selected.",
                xalign=0,
                wrap=True,
                css_classes=["dim-label"],
            ))
            return
        # File row branch: download is always available (v1 line 332).
        if self.download_btn is not None:
            self.download_btn.set_sensitive(True)

        heading_label = Gtk.Label(
            label=str(file_row.get("name", "")) or "(unnamed)",
            xalign=0,
            css_classes=["title-3"],
        )
        heading_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        heading_label.set_tooltip_text(str(file_row.get("name", "")))
        self.detail_box.append(heading_label)

        pairs = [
            ("Path", str(file_row.get("path", "")) or "-", True, True),
            ("Logical size", _format_bytes(int(file_row.get("size", 0))), False, False),
            ("Remote stored size", _format_bytes(int(file_row.get("stored_size", 0))), False, False),
            ("Modified", format_local(file_row.get("modified")) or "-", False, False),
            ("Current version", str(file_row.get("latest_version_id", "")) or "-", True, True),
            ("Versions", str(file_row.get("versions", 0)), False, False),
            ("Status", str(file_row.get("status", "")), False, False),
        ]
        for label_text, value_text, ellipsize, monospace in pairs:
            pair = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            pair.set_margin_bottom(6)
            key = Gtk.Label(
                label=label_text, xalign=0, css_classes=["dim-label", "caption"],
            )
            val_classes = ["monospace"] if monospace else []
            val = Gtk.Label(label=value_text, xalign=0, css_classes=val_classes)
            val.set_selectable(True)
            if ellipsize:
                val.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                val.set_tooltip_text(value_text)
            else:
                val.set_wrap(True)
                val.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            pair.append(key)
            pair.append(val)
            self.detail_box.append(pair)

        self._render_versions_section(file_row)

    def _render_versions_section(self, file_row: dict) -> None:
        assert self.detail_box is not None
        manifest = self.state.manifest
        if not manifest:
            return
        try:
            versions = list_versions(
                manifest,
                str(file_row.get("path", "")),
                include_deleted=bool(file_row.get("deleted")),
            )
        except Exception:
            versions = []

        self.detail_box.append(Gtk.Label(
            label="Versions", xalign=0, css_classes=["title-3"],
        ))
        if bool(file_row.get("deleted")):
            deleted_at_local = format_local(file_row.get("deleted_at"))
            recoverable_local = format_local(file_row.get("recoverable_until"))
            tombstone_label = Gtk.Label(
                label=(
                    f"Deleted {deleted_at_local}".strip()
                    + (
                        f" — recoverable until {recoverable_local}"
                        if recoverable_local else ""
                    )
                ),
                xalign=0,
                wrap=True,
                css_classes=["dim-label"],
            )
            self.detail_box.append(tombstone_label)
        if not versions:
            self.detail_box.append(Gtk.Label(
                label="No version history yet.",
                xalign=0,
                wrap=True,
                css_classes=["dim-label"],
            ))
            return

        # v1 line 414: at least one version → enable the Versions
        # toolbar button so its tooltip ("Choose a version below…")
        # is reachable.
        if self.versions_btn is not None:
            self.versions_btn.set_sensitive(True)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        self.detail_box.append(grid)
        for col, header in enumerate(
            ("", "Modified", "Device", "Size", "Status", ""),
        ):
            grid.attach(
                Gtk.Label(label=header, xalign=0, css_classes=["dim-label"]),
                col, 0, 1, 1,
            )

        entry_deleted = bool(file_row.get("deleted"))
        for row_index, version in enumerate(versions, start=1):
            # Per-version download icon — pass 4 wires this. Restore
            # button below stays placeholder-disabled until pass 5.
            download_icon_btn = Gtk.Button.new_from_icon_name("document-save-symbolic")
            download_icon_btn.add_css_class("flat")
            download_icon_btn.set_tooltip_text("Download this version")
            download_icon_btn.connect(
                "clicked",
                lambda _b, v=dict(version), f=dict(file_row):
                    self._choose_version_destination(f, v),
            )
            if version.get("is_current") and not entry_deleted:
                download_icon_btn.set_sensitive(False)
            grid.attach(download_icon_btn, 0, row_index, 1, 1)

            modified = format_local(version.get("modified")) or "-"
            grid.attach(Gtk.Label(label=modified, xalign=0), 1, row_index, 1, 1)
            device = str(version.get("author_device_id") or "")
            grid.attach(
                Gtk.Label(label=device[:12] if device else "-", xalign=0),
                2, row_index, 1, 1,
            )
            size_label = _format_bytes(int(version.get("size", 0) or 0))
            grid.attach(Gtk.Label(label=size_label, xalign=0), 3, row_index, 1, 1)
            if entry_deleted and version.get("is_current"):
                status_label = "Latest (deleted)"
            elif version.get("is_current"):
                status_label = "Current"
            else:
                status_label = "Previous"
            grid.attach(Gtk.Label(label=status_label, xalign=0), 4, row_index, 1, 1)

            show_restore = (not version.get("is_current")) or entry_deleted
            if show_restore:
                restore_btn = Gtk.Button(
                    label="Restore as current",
                    css_classes=["pill", "suggested-action"],
                )
                restore_btn.set_tooltip_text(
                    "Promote this version to the current one. Tombstone is lifted."
                    if entry_deleted else
                    "Promote this version to the current one. The previous "
                    "current becomes restorable history."
                )
                restore_btn.connect(
                    "clicked",
                    lambda _b, v=dict(version), f=dict(file_row):
                        self._confirm_restore_version(f, v),
                )
                grid.attach(restore_btn, 5, row_index, 1, 1)

    # ------------------------------------------------------------------ navigation
    def _navigate_to(self, path: str, *, record: bool = True) -> None:
        new_path = str(path)
        if new_path == self.state.path:
            self._render_all()
            return
        if record:
            self.state.back.append(self.state.path)
            self.state.forward = []
        self.state.path = new_path
        self.state.selected_file = None
        self._render_all()

    def _on_back_clicked(self, _btn: Gtk.Button) -> None:
        if not self.state.back:
            return
        self.state.forward.append(self.state.path)
        previous = self.state.back.pop()
        self._navigate_to(str(previous), record=False)

    def _on_forward_clicked(self, _btn: Gtk.Button) -> None:
        if not self.state.forward:
            return
        self.state.back.append(self.state.path)
        next_path = self.state.forward.pop()
        self._navigate_to(str(next_path), record=False)

    # ------------------------------------------------------------------ async manifest refresh
    def _refresh_manifest_async(self) -> None:
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self.state.manifest = None
            self.state.path = ""
            self.state.back = []
            self.state.forward = []
            self.state.selected_file = None
            self._render_all("No local vault is connected.", "error")
            return

        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        self._set_status("Refreshing vault manifest...")

        def worker() -> None:
            try:
                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    manifest = vault.fetch_manifest(
                        relay, local_index=self.local_index,
                    )
                finally:
                    vault.close()
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._render_all(
                        f"Could not refresh vault browser: {error_message}",
                        "error",
                    )
                    return False

                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                self.state.manifest = manifest
                # Validate the current path still exists in the new
                # manifest; reset to root if not.
                try:
                    list_folder(manifest, str(self.state.path))
                except Exception:
                    self.state.path = ""
                    self.state.back = []
                    self.state.forward = []
                self.state.selected_file = None
                self._render_all("Vault browser refreshed.", "success")
                self._refresh_resume_banner(vault_id)
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ upload paths
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

    # ------------------------------------------------------------------ delete + restore
    def _show_progress_no_cancel(self) -> None:
        """Short-running flow that shouldn't expose a Cancel button.

        Used for the delete worker (single manifest mutation) where
        cancel mid-publish would be a partial-state hazard.
        """
        self._active_cancel = None
        if self.cancel_btn is not None:
            self.cancel_btn.set_visible(False)
        if self.progress_box is not None:
            self.progress_box.set_visible(True)

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
        from ..vault_delete import delete_file

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
        from ..vault_delete import delete_folder_contents

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
        from ..vault_delete import restore_version_to_current

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

    # ------------------------------------------------------------------ quota + eviction
    def _handle_quota_exceeded(
        self, exc: VaultQuotaExceededError, *, action: str,
    ) -> None:
        """T6.6 + T7.5: route a 507 into either the eviction prompt or
        the vault-full banner depending on ``eviction_available``."""
        info = describe_quota_exceeded(exc)
        if info["eviction_available"]:
            if self.quota_banner is not None:
                self.quota_banner.set_revealed(False)
            dlg = Adw.AlertDialog(
                heading=info["heading"],
                body=info["body"],
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("evict", info["primary_action_label"])
            # F-U04: eviction is irreversible (history is dropped),
            # so Enter must default to Cancel. The action button is
            # rendered destructive to match brand guidance.
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance(
                "evict", Adw.ResponseAppearance.DESTRUCTIVE,
            )

            def on_response(_dialog, response: str) -> None:
                if response == "evict":
                    delta = max(1, exc.used_bytes - exc.quota_bytes + 1)
                    self._run_eviction_pass(action=action, target_bytes=delta)
                else:
                    self._set_status(
                        f"{action} paused — vault is full ({info['percent']}%).",
                        "error",
                    )
            dlg.connect("response", on_response)
            dlg.present(self.win)
            return

        # No history left → terminal sync-stop banner per §D2 step 4.
        if self.quota_banner is not None:
            self.quota_banner.set_title(info["body"])
            self.quota_banner.set_button_label(info["primary_action_label"])
            self.quota_banner.set_revealed(True)
        self._set_status(
            f"{action} stopped: vault full and no backup history remains.",
            "error",
        )

    def _run_eviction_pass(self, *, action: str, target_bytes: int) -> None:
        """T7.5: run the §D2 eviction pipeline in a worker thread."""
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Reclaiming space...")
        self._set_status(
            f"{action}: running eviction to free {target_bytes} bytes...",
        )

        def worker() -> None:
            try:
                from ..vault_eviction import eviction_pass

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
                    result = eviction_pass(
                        vault=vault, relay=relay,
                        manifest=current_manifest,
                        author_device_id=device_id,
                        target_bytes_to_free=target_bytes,
                        local_index=self.local_index,
                        should_continue=lambda: not cancel_event.is_set(),
                    )
                finally:
                    vault.close()
            except SyncCancelledError:
                def cancelled() -> bool:
                    self._disarm_cancel()
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status("Eviction cancelled.")
                    return False
                GLib.idle_add(cancelled)
                return
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status(f"Eviction failed: {error_message}", "error")
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self.state.manifest = result.manifest
                self.state.selected_file = None
                self._disarm_cancel()
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                if result.no_more_candidates:
                    if self.quota_banner is not None:
                        self.quota_banner.set_title(
                            "Vault is full and no backup history remains. "
                            "Sync is stopped. Free space by deleting files, "
                            "or export and migrate to a relay with more capacity."
                        )
                        self.quota_banner.set_button_label("Open vault settings")
                        self.quota_banner.set_revealed(True)
                    self._render_all(
                        f"Eviction stopped — no more candidates. "
                        f"Freed {result.bytes_freed} bytes.",
                        "error",
                    )
                else:
                    self._render_all(
                        f"Eviction freed {result.bytes_freed} bytes "
                        f"({result.chunks_freed} chunks). "
                        f"Try {action.lower()} again.",
                        "success",
                    )
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ resume banner
    def _refresh_resume_banner(self, vault_id: str) -> None:
        try:
            sessions = list_resumable_sessions(
                vault_id, default_upload_resume_dir(),
            )
        except Exception:
            sessions = []
        self.state.resume_sessions = sessions
        if self.resume_banner is None:
            return
        if not sessions:
            self.resume_banner.set_revealed(False)
            return
        count = len(sessions)
        label = (
            "1 upload was interrupted — click Resume to finish it."
            if count == 1
            else f"{count} uploads were interrupted — click Resume to finish them."
        )
        self.resume_banner.set_title(label)
        self.resume_banner.set_revealed(True)

    def _start_resume_pending(self, _btn=None) -> None:
        sessions = list(self.state.resume_sessions or [])
        if not sessions:
            return
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        if self.upload_btn is not None:
            self.upload_btn.set_sensitive(False)
        if self.upload_folder_btn is not None:
            self.upload_folder_btn.set_sensitive(False)
        if self.resume_banner is not None:
            self.resume_banner.set_revealed(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Resuming uploads...")
        self._set_status(
            f"Resuming {len(sessions)} interrupted upload(s)...",
        )

        def worker() -> None:
            from ..vault_upload import resume_upload

            completed = 0
            failed = 0
            cancelled_count = 0
            last_manifest = self.state.manifest
            try:
                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    for session in sessions:
                        if cancel_event.is_set():
                            break
                        try:
                            current_manifest = vault.fetch_manifest(
                                relay, local_index=self.local_index,
                            )
                            result = resume_upload(
                                vault=vault,
                                relay=relay,
                                manifest=current_manifest,
                                session=session,
                                local_index=self.local_index,
                                should_continue=lambda: not cancel_event.is_set(),
                            )
                            last_manifest = result.manifest
                            completed += 1
                        except SyncCancelledError:
                            cancelled_count += 1
                            break
                        except Exception:
                            failed += 1
                finally:
                    vault.close()
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    upload_dest = self._resolve_upload_destination()
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(upload_dest is not None)
                    if self.upload_folder_btn is not None:
                        self.upload_folder_btn.set_sensitive(upload_dest is not None)
                    self._set_status(f"Resume failed: {error_message}", "error")
                    self._refresh_resume_banner(vault_id)
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self._disarm_cancel()
                upload_dest = self._resolve_upload_destination()
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                if self.upload_btn is not None:
                    self.upload_btn.set_sensitive(upload_dest is not None)
                if self.upload_folder_btn is not None:
                    self.upload_folder_btn.set_sensitive(upload_dest is not None)
                if last_manifest is not None:
                    self.state.manifest = last_manifest
                self.state.selected_file = None
                self._render_all()
                if cancelled_count > 0:
                    self._set_status(
                        f"Resume cancelled. {completed} upload(s) finished, "
                        f"{len(sessions) - completed - failed} pending.",
                    )
                elif failed == 0:
                    self._set_status(
                        f"Resumed {completed} upload(s).", "success",
                    )
                else:
                    self._set_status(
                        f"Resumed {completed} upload(s); {failed} failed "
                        "(will retry next time).",
                        "error",
                    )
                self._refresh_resume_banner(vault_id)
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()
