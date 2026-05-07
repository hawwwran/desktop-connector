"""v2 Vault browser — structural refactor of windows_vault_browser.py.

Closures from v1's ``on_activate`` are lifted onto a ``VaultBrowser``
class so each piece of state is reachable via ``self.*`` instead of
captured in a nested function. Pass 1 ports the window shell, the
async manifest refresh, the breadcrumb, the back/forward stack, and
the left tree pane. The center file list and right detail pane are
placeholders until pass 2.

The v1 module stays in place; this v2 entry point is wired through
the tray's "Open Vault NEW" menu item so both can be exercised side
by side until parity is verified.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..brand import (  # noqa: E402
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from ..vault_browser_model import list_folder  # noqa: E402
from ..vault_error_messages import humanize  # noqa: E402
from ..vault_local_index import VaultLocalIndex  # noqa: E402
from ..vault_runtime import (  # noqa: E402
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault_time_format import format_local  # noqa: E402
from ..windows_common import _format_bytes, _make_app  # noqa: E402
from .state import BrowserState

log = logging.getLogger(__name__)


def show_vault_browser_v2(
    config_dir: Path,
    vault_id_override: str | None = None,
) -> None:
    """Run the v2 vault browser as a subprocess window.

    Mirrors :func:`show_vault_browser` (v1) so the dispatch in
    ``windows.py`` and the tray menu can swap between them with no
    other plumbing changes.
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
        self.breadcrumb: Gtk.Label | None = None
        self.status_label: Gtk.Label | None = None
        self.tree_box: Gtk.Box | None = None
        self.list_grid: Gtk.Grid | None = None
        self.detail_box: Gtk.Box | None = None

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
            title="Vault (NEW)",
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

        self.win.present()
        # Kick the initial manifest fetch on entry so the user sees the
        # tree populate without a manual click.
        self._refresh_manifest_async()

    # ------------------------------------------------------------------ layout
    def _build_action_bar(self) -> None:
        assert self.outer is not None
        self.action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.outer.append(self.action_bar)

        self.back_btn = Gtk.Button(label="Back", css_classes=["pill"])
        self.forward_btn = Gtk.Button(label="Forward", css_classes=["pill"])
        self.refresh_btn = Gtk.Button(label="Refresh", css_classes=["pill"])

        self.back_btn.connect("clicked", self._on_back_clicked)
        self.forward_btn.connect("clicked", self._on_forward_clicked)
        self.refresh_btn.connect("clicked", lambda _btn: self._refresh_manifest_async())

        for button in (self.back_btn, self.forward_btn, self.refresh_btn):
            self.action_bar.append(button)
        self._update_nav_buttons()

    def _build_breadcrumb_and_status(self) -> None:
        assert self.outer is not None
        self.breadcrumb = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.breadcrumb.add_css_class("title-4")
        self.outer.append(self.breadcrumb)

        self.status_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self.outer.append(self.status_label)

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
        self._render_detail_placeholder()
        if message is not None:
            self._set_status(message, css_class)

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
        # Detail pane is still placeholder in pass 2; pass 3 will
        # replace this with the real render_detail. Re-render the
        # placeholder so the selected-row state is at least visible
        # in self.state for the next pass.
        self._render_detail_placeholder()

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

    def _render_detail_placeholder(self) -> None:
        if self.detail_box is None:
            return
        self._clear_box(self.detail_box)
        self.detail_box.append(Gtk.Label(
            label="Details", xalign=0, css_classes=["title-3"],
        ))
        self.detail_box.append(Gtk.Label(
            label="Right pane lands in v2 pass 3 — metadata + version history.",
            xalign=0,
            wrap=True,
            css_classes=["dim-label"],
        ))

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
                return False

            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()
