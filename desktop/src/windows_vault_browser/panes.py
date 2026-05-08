"""PanesMixin — tree, file list, detail + versions render passes.

Owns every ``self._render_*`` and the small ``_attach_*`` /
``_select_file`` helpers that paint widget content based on the
current ``self.state`` (manifest + path + selection). All called
from the orchestrator's ``_render_all`` coordinator.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Pango  # noqa: E402

from ..vault_browser_model import list_folder, list_versions
from ..vault_time_format import format_local
from ..windows_common import _format_bytes


class PanesMixin:
    """Tree pane + center file list + right-hand detail pane."""

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
