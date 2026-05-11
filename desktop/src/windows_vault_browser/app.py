"""Vault browser — structural refactor of the original windows_vault_browser.py.

Closures from the original ``on_activate`` body are lifted onto a
``VaultBrowser`` class so each piece of state is reachable via
``self.*`` instead of captured in a nested function. Built up over
five passes (introduction → file list → detail pane → downloads →
uploads/delete/quota/resume) verified side-by-side against the
original module before that module was removed.

Mixin extraction (2026-05-08): the body of every ``self._*`` method
was moved to a topical sibling module and composed onto
``VaultBrowser`` via multiple inheritance. The orchestrator keeps
``__init__`` + ``run`` + ``_on_activate`` + the cross-cutting
``_render_all`` coordinator + small shared helpers
(``_set_status``, ``_arm_cancel`` …) used by every mixin.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..brand import (  # noqa: E402
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..vault_local_index import VaultLocalIndex  # noqa: E402
from ..vault.binding.runtime import (  # noqa: E402
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault.error_messages import humanize  # noqa: E402
from ..vault_browser_model import list_folder  # noqa: E402
from ..windows_common import _make_app  # noqa: E402

from .delete_restore import DeleteRestoreMixin
from .downloads import DownloadsMixin
from .layout import LayoutMixin
from .panes import PanesMixin
from .quota import QuotaMixin
from .resume_banner import ResumeBannerMixin
from .state import BrowserState
from .uploads import UploadsMixin

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


class VaultBrowser(
    LayoutMixin,
    PanesMixin,
    DownloadsMixin,
    UploadsMixin,
    DeleteRestoreMixin,
    QuotaMixin,
    ResumeBannerMixin,
):
    """Owns the entire browser window: state, widgets, and async hooks.

    Built incrementally — pass 1 covers the window shell + tree pane +
    manifest refresh. Subsequent passes added file list, detail pane,
    downloads, uploads, quota, resume, and the cancel/progress
    cluster. The same shape that v1 used (one ``on_activate`` closure
    body + helper closures) is preserved here as ``_on_activate`` +
    a flat list of ``self._render_*`` / ``self._on_*`` methods, now
    distributed across topical mixins (see imports above) but
    behaviourally identical.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        config,
        vault_id_override: str | None,
    ) -> None:
        from ..vault.ui.window_args import resolve_active_vault_id

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
        # Resume "banner" is a custom horizontal Gtk.Box (not Adw.Banner)
        # because Adw.Banner only supports a single action button. Users
        # need both Resume (continue the interrupted upload) and Cancel
        # (discard the saved session, banner goes away).
        self.resume_banner_box: Gtk.Box | None = None
        self.resume_banner_label: Gtk.Label | None = None
        self.resume_resume_btn: Gtk.Button | None = None
        self.resume_cancel_btn: Gtk.Button | None = None
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

    def _on_show_deleted_toggled(self, button: Gtk.CheckButton) -> None:
        self.state.show_deleted = bool(button.get_active())
        # v1 line 1820: also clear the selection so a tombstoned row
        # doesn't keep its detail pane after the toggle hides it.
        self.state.selected_file = None
        self._render_all()

    # ------------------------------------------------------------------ download folder name (shared helper)
    @staticmethod
    def _download_folder_name(path: str) -> str:
        parts = [
            part for part in str(path).replace("\\", "/").split("/")
            if part and part != "."
        ]
        return parts[-1] if parts else "Vault"

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
