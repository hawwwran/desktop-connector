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
from ..vault.state.local_index import VaultLocalIndex  # noqa: E402
from ..vault.binding.runtime import (  # noqa: E402
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault.error_messages import humanize  # noqa: E402
from ..vault.ui.browser_model import list_folder  # noqa: E402
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
        # Legacy slot — used to be the body button strip. Retained at
        # ``None`` so any straggler reference doesn't NameError; the
        # action bar now lives in ``Adw.HeaderBar`` (chrome) plus
        # ``selection_actions_revealer`` (contextual). Drop in Wave 1.5.
        self.action_bar: Gtk.Box | None = None
        self._toolbar_view: Adw.ToolbarView | None = None
        self._header_bar: Adw.HeaderBar | None = None
        self._show_deleted_action = None
        self._upload_folder_action = None
        self.back_btn: Gtk.Button | None = None
        self.forward_btn: Gtk.Button | None = None
        self.refresh_btn: Gtk.Button | None = None
        self.upload_btn: Adw.SplitButton | None = None
        self.upload_folder_btn: Gtk.Button | None = None
        self.delete_btn: Gtk.Button | None = None
        self.versions_btn: Gtk.Button | None = None
        self.download_btn: Gtk.Button | None = None
        self.show_deleted_toggle: Gtk.CheckButton | None = None
        self.selection_actions_revealer: Gtk.Revealer | None = None
        # Wave 1.5: window title (header subtitle is the current path).
        self._window_title: Adw.WindowTitle | None = None
        # Wave 3.1: fixed-position status icon in the header bar.
        self._status_icon: Gtk.Image | None = None
        self._status_timeout_id: int | None = None
        # Wave 2.5: Adw.OverlaySplitView wrapping the sidebar + content,
        # plus the header-bar toggle that reveals/hides the sidebar
        # while collapsed.
        self.split_view: Adw.OverlaySplitView | None = None
        self._sidebar_toggle_btn: Gtk.ToggleButton | None = None
        # Wave 3.5: detail-pane scroller is needed by _scroll_to_versions.
        self.detail_scroller: Gtk.ScrolledWindow | None = None
        self._versions_heading_label: Gtk.Label | None = None
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
        # Wave 2: the folder sidebar is now a Gtk.ListBox styled
        # ``navigation-sidebar``. The legacy ``tree_box`` slot is
        # retired; ``tree_listbox`` is the new home.
        self.tree_box = None  # legacy slot; kept None for back-compat
        self.tree_listbox: Gtk.ListBox | None = None
        # Wave 3.2: list_grid slot retired in favour of list_listbox.
        self.list_grid: Gtk.Grid | None = None  # legacy slot; kept None for back-compat
        self.list_listbox: Gtk.ListBox | None = None
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
        # Header bar is added by ``_build_action_bar`` (it owns the
        # populated header now). Stash the toolbar view so the builder
        # can reach it.
        self._toolbar_view = toolbar

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
        self._wire_responsive_sidebar()

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

    def _wire_responsive_sidebar(self) -> None:
        """Wave 2.5: bind split-view ⟷ toggle button + window breakpoint.

        On a wide window the OverlaySplitView keeps the sidebar
        permanently visible — the toggle button is hidden. Below the
        breakpoint width (600sp), the split view collapses; the toggle
        button appears and reveals / hides the sidebar overlay. The
        ``Adw.Breakpoint`` watches the window's width condition and
        flips ``collapsed`` on the split view automatically.
        """
        if (
            self.split_view is None
            or self._sidebar_toggle_btn is None
            or self.win is None
        ):
            return

        from gi.repository import GObject

        # Toggle button visibility tracks split-view ``collapsed`` — the
        # button only matters in narrow mode. ``SYNC_CREATE`` snaps the
        # initial value across at bind time.
        self.split_view.bind_property(
            "collapsed",
            self._sidebar_toggle_btn,
            "visible",
            GObject.BindingFlags.SYNC_CREATE,
        )
        # Toggle state ↔ sidebar visibility, bidirectional.
        self.split_view.bind_property(
            "show-sidebar",
            self._sidebar_toggle_btn,
            "active",
            (
                GObject.BindingFlags.BIDIRECTIONAL
                | GObject.BindingFlags.SYNC_CREATE
            ),
        )

        # Narrow-window breakpoint: when the window is ≤ 600 scaled
        # pixels wide, collapse the split view. On wider windows the
        # sidebar is permanently visible.
        breakpoint_cond = Adw.BreakpointCondition.parse(
            "max-width: 600sp",
        )
        bp = Adw.Breakpoint.new(breakpoint_cond)
        bp.add_setter(self.split_view, "collapsed", True)
        bp.add_setter(self.split_view, "show-sidebar", False)
        self.win.add_breakpoint(bp)

    def _scroll_to_versions(self) -> bool:
        """Wave 3.5: jump the Details pane to the Versions heading.

        Scheduled via ``GLib.idle_add`` so the layout pass has
        completed and the heading label has a valid allocation.
        ``compute_point`` converts the label's origin (0, 0) into
        ``detail_box`` coordinates, which is what the scroller's
        vertical adjustment expects. Returns ``False`` so ``idle_add``
        treats it as one-shot.
        """
        if (
            self.detail_scroller is None
            or self.detail_box is None
            or self._versions_heading_label is None
        ):
            return False
        try:
            from gi.repository import Graphene
            success, point = self._versions_heading_label.compute_point(
                self.detail_box, Graphene.Point.zero(),
            )
        except Exception:
            log.debug("scroll-to-versions compute_point failed", exc_info=True)
            return False
        if not success:
            return False
        vadj = self.detail_scroller.get_vadjustment()
        if vadj is None:
            return False
        # A small upward bias gives the heading a bit of breathing room
        # from the scroll-area's top edge.
        target = max(0.0, point.y - 8.0)
        vadj.set_value(min(target, vadj.get_upper() - vadj.get_page_size()))
        return False

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

    def _set_status(self, message: str, css_class: str = "dim-label") -> None:
        """Set the user-facing status state.

        ``css_class`` is the legacy semantic tag: ``"success"`` /
        ``"error"`` / ``"dim-label"`` (info/idle). Wave 3.1 drives a
        fixed-position icon on the header bar; the message becomes
        the icon's tooltip.
        """
        if css_class == "success":
            self._update_status_icon("success", message, auto_clear_seconds=4)
        elif css_class == "error":
            self._update_status_icon("error", message, auto_clear_seconds=None)
        else:
            # ``dim-label`` covers transient info-style messages
            # ("Downloading foo…") plus the empty/idle clear path.
            if message:
                self._update_status_icon("info", message, auto_clear_seconds=4)
            else:
                self._update_status_icon("idle", "", auto_clear_seconds=None)

    def _update_status_icon(
        self, state: str, tooltip: str, *, auto_clear_seconds: int | None,
    ) -> None:
        """Drive the header-bar status icon.

        States: ``idle`` (hidden), ``success`` (brand blue check),
        ``error`` (brand orange triangle), ``info`` (dim info-i).
        Optional ``auto_clear_seconds`` schedules a fade back to idle.
        """
        if self._status_icon is None:
            return
        # Cancel any pending auto-clear so a new state doesn't get
        # wiped by the previous state's timer.
        if self._status_timeout_id is not None:
            try:
                GLib.source_remove(self._status_timeout_id)
            except Exception:
                pass
            self._status_timeout_id = None

        icon_for_state = {
            "success": "emblem-ok-symbolic",
            "error": "dialog-error-symbolic",
            "info": "dialog-information-symbolic",
        }
        css_for_state = {
            "success": "success",
            "error": "error",
            "info": "dim-label",
        }
        if state == "idle":
            self._status_icon.set_visible(False)
            self._status_icon.set_tooltip_text(None)
            for klass in ("success", "error", "dim-label"):
                self._status_icon.remove_css_class(klass)
            return

        self._status_icon.set_from_icon_name(icon_for_state[state])
        for klass in ("success", "error", "dim-label"):
            self._status_icon.remove_css_class(klass)
        self._status_icon.add_css_class(css_for_state[state])
        self._status_icon.set_tooltip_text(tooltip or None)
        self._status_icon.set_visible(True)

        if auto_clear_seconds is not None and auto_clear_seconds > 0:
            def _clear() -> bool:
                self._status_timeout_id = None
                self._update_status_icon("idle", "", auto_clear_seconds=None)
                return False  # one-shot

            self._status_timeout_id = GLib.timeout_add_seconds(
                auto_clear_seconds, _clear,
            )

    def _current_path_label(self) -> str:
        path = str(self.state.path)
        return "Vault" if not path else "Vault / " + path.replace("/", " / ")

    def _current_subtitle(self) -> str:
        """Path component for the header bar subtitle.

        Empty at the vault root (title alone is enough); otherwise
        ``foo / bar`` formatting that matches the legacy breadcrumb
        style minus the leading ``Vault / `` (now redundant with
        the header title).
        """
        path = str(self.state.path or "").strip("/")
        return path.replace("/", " / ") if path else ""

    def _update_nav_buttons(self) -> None:
        if self.back_btn is not None:
            self.back_btn.set_sensitive(bool(self.state.back))
        if self.forward_btn is not None:
            self.forward_btn.set_sensitive(bool(self.state.forward))


    # ------------------------------------------------------------------ render
    def _render_all(self, message: str | None = None, css_class: str = "dim-label") -> None:
        if self._window_title is not None:
            self._window_title.set_subtitle(self._current_subtitle())
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
        return self._resolve_upload_destination_for(str(self.state.path or ""))

    def _resolve_upload_destination_for(
        self, target_path: str,
    ) -> tuple[str, str] | None:
        """Wave 3.2: explicit-path variant for per-row menus.

        Same shape as ``_resolve_upload_destination`` but resolves
        against a caller-supplied path instead of the current
        navigation state — lets a sidebar folder's hamburger menu
        operate on a folder you're not currently inside.
        """
        manifest = self.state.manifest
        path = str(target_path or "")
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

    # ------------------------------------------------------------------ Wave 3.2 / 3.3 per-row menu actions
    def _menu_action_download_file(self, file_row: dict) -> None:
        """Per-row file menu: select the row then trigger the existing dispatch.

        Selecting first lights up the row + populates the right-hand
        details pane, so the user gets visual feedback for what they
        clicked on. The existing ``_choose_download_destination``
        handler reads ``self.state.selected_file`` and opens the
        save-file dialog for that file.
        """
        self._select_file(dict(file_row))
        self._render_all()
        self._choose_download_destination(None)

    def _menu_action_versions(self, file_row: dict) -> None:
        """Per-row file menu: surface the file's version history.

        Selects the row so the right-hand Details pane renders its
        version list — that pane already builds the per-version
        Download icons via ``_render_versions_section``. Wave 3.5
        polish: schedule a scroll-to-Versions-heading on the next
        idle so the user lands on the section they asked for instead
        of the top of the Details pane.
        """
        self._select_file(dict(file_row))
        self._render_all()
        GLib.idle_add(self._scroll_to_versions)

    def _menu_action_delete_file(self, file_row: dict) -> None:
        """Per-row file menu: confirm + delete the file.

        Routes straight to the existing ``_confirm_delete_file``
        dispatcher — no state mutation required, since that helper
        already takes the file_row dict explicitly.
        """
        if bool(file_row.get("deleted")):
            self._set_status(
                "File is already deleted; cannot soft-delete again.",
                "error",
            )
            return
        self._confirm_delete_file(dict(file_row))

    def _menu_action_download_folder(self, folder_path: str) -> None:
        """Per-row sidebar menu: download the folder at ``folder_path``.

        Navigates into the folder first so the user sees the contents
        they're about to download, then opens the folder-save dialog
        via the existing dispatch. Side effect: the right pane and
        breadcrumb update — appropriate UX feedback for the action.
        """
        self._navigate_to(folder_path)
        self._choose_download_destination(None)

    def _menu_action_delete_folder(self, folder_path: str) -> None:
        """Per-row sidebar menu: confirm + delete the folder.

        Resolves ``folder_path`` to the underlying (remote_folder_id,
        sub_path) tuple via the explicit-path resolver, then dispatches
        to the existing ``_confirm_delete_folder`` helper. Avoids
        mutating navigation state — the delete is per-row, not "for
        the folder you're currently in".
        """
        destination = self._resolve_upload_destination_for(folder_path)
        if destination is None:
            self._set_status(
                f"Cannot delete '{folder_path}' — not inside an active "
                "remote folder.",
                "error",
            )
            return
        remote_folder_id, sub_path = destination
        self._confirm_delete_folder(remote_folder_id, sub_path)

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

    def _on_list_row_selected(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None,
    ) -> None:
        """Selection in the center file list drives the Details pane.

        File rows update ``selected_file`` and render details; folder
        rows clear the selection (their action is navigation via
        row-activated, not selection-driven details).
        """
        if row is None:
            return
        kind = getattr(row, "_vault_kind", None)
        if kind == "file":
            file_row = getattr(row, "_vault_file_row", None)
            if file_row is not None:
                self._select_file(dict(file_row))
                self._render_detail(self.state.selected_file)
                return
        # Folder row or empty row: clear file selection so the
        # Details pane doesn't show stale info from a previous click.
        if self.state.selected_file is not None:
            self.state.selected_file = None
            self._render_detail(None)

    def _on_list_row_activated(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow,
    ) -> None:
        """Double-click (or Enter) on a folder row navigates into it.

        File rows already drive selection via ``row-selected``;
        ``row-activated`` is only meaningful for folder rows where
        the affordance is "open this folder".
        """
        if row is None:
            return
        if getattr(row, "_vault_kind", None) != "folder":
            return
        folder_path = getattr(row, "_vault_folder_path", None)
        if folder_path:
            self._navigate_to(str(folder_path))

    def _on_tree_row_activated(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow,
    ) -> None:
        """Sidebar row clicked → navigate to that folder.

        Each row stashes its target path on the ``_vault_path`` Python
        attribute when constructed in ``_render_tree``. An empty path
        means the Vault root.
        """
        if row is None:
            return
        target = getattr(row, "_vault_path", None)
        if target is None:
            return
        self._navigate_to(str(target))

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
