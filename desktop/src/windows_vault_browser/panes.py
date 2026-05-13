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

from ..vault.ui.browser_model import list_folder, list_versions
from ..vault.ui.time_format import format_local
from ..windows_common import _format_bytes


class PanesMixin:
    """Tree pane + center file list + right-hand detail pane."""

    def _render_tree(self) -> None:
        """Render the folder sidebar (Gtk.ListBox with navigation-sidebar style).

        Each row carries its target vault path on the ``_vault_path``
        Python attribute; the ``row-activated`` signal on the listbox
        looks it up and calls ``_navigate_to``. The row matching the
        current ``self.state.path`` is selected (highlighted) at the
        end so the sidebar always reflects "where you are".
        """
        if self.tree_listbox is None:
            return
        # Block the row-activated signal while we rebuild + re-select
        # so programmatic ``select_row`` doesn't bounce back through
        # ``_navigate_to``.
        self.tree_listbox.handler_block_by_func(self._on_tree_row_activated)
        try:
            self.tree_listbox.remove_all()
            current_path = str(self.state.path or "")
            active_row: Gtk.ListBoxRow | None = None

            root_row = self._make_tree_row("Vault", path="", depth=0)
            self.tree_listbox.append(root_row)
            if current_path == "":
                active_row = root_row

            manifest = self.state.manifest
            if manifest:
                def walk(path: str, depth: int) -> None:
                    nonlocal active_row
                    try:
                        children, _files = list_folder(manifest, path)
                    except Exception:
                        return
                    for child in children:
                        child_path = str(child["path"])
                        name = (
                            child_path.split("/")[-1]
                            if child_path else "Vault"
                        )
                        row = self._make_tree_row(
                            name, path=child_path, depth=depth,
                        )
                        self.tree_listbox.append(row)
                        if child_path == current_path:
                            active_row = row
                        walk(child_path, depth + 1)

                walk("", 1)

            if active_row is not None:
                self.tree_listbox.select_row(active_row)
        finally:
            self.tree_listbox.handler_unblock_by_func(
                self._on_tree_row_activated,
            )

    def _make_tree_row(
        self, name: str, *, path: str, depth: int,
    ) -> Gtk.ListBoxRow:
        """Build one sidebar row: folder icon + label, indented per depth.

        Indentation tracks the manifest's folder depth so users can read
        the tree shape visually. Depth 0 (the Vault root) gets no inset.

        Wave 3.3 (2026-05-13): non-root rows get a hamburger MenuButton
        suffix with Download folder / Delete folder. The Vault root row
        is unchanged — you can't download or delete "everything".
        """
        row = Gtk.ListBoxRow()
        row._vault_path = path  # type: ignore[attr-defined]
        body = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=4,
            margin_bottom=4,
            margin_start=8 + depth * 16,
            margin_end=8,
        )
        body.append(Gtk.Image.new_from_icon_name("folder-symbolic"))
        label = Gtk.Label(label=name, xalign=0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        body.append(label)

        if path:
            # Non-root: attach per-row hamburger with Download / Delete.
            body.append(
                self._make_row_menu_button(folder={"path": path, "name": name}),
            )
        row.set_child(body)
        return row

    def _select_file(self, file_row: dict) -> None:
        self.state.selected_file = file_row
        self._render_detail(file_row)

    def _render_file_list(self) -> None:
        """Render the center file list as a Gtk.ListBox of cards.

        Wave 3.2 chrome redesign (2026-05-13): the former Gtk.Grid
        with five fixed columns (Name, Size, Modified, Versions,
        Status) was poorly fitted to variable-width content — the
        date column in particular forced the rest of the row to
        compete for space. The replacement renders each entry as a
        card with a title (name) + subtitle (size · modified · status
        · versions) + per-row hamburger menu (Download / [Versions]
        / Delete). Subtitle fields are joined with `` · `` separators
        and gracefully wrap on narrow widths.
        """
        if self.list_listbox is None:
            return
        # Block selection callback so programmatic rebuilds don't
        # bounce events back through _select_file.
        self.list_listbox.handler_block_by_func(self._on_list_row_selected)
        try:
            self.list_listbox.remove_all()
            self._render_file_list_inner()
        finally:
            self.list_listbox.handler_unblock_by_func(
                self._on_list_row_selected,
            )

    def _render_file_list_inner(self) -> None:
        assert self.list_listbox is not None
        manifest = self.state.manifest
        if not manifest:
            self.list_listbox.append(
                self._make_empty_row(
                    "Open or refresh a vault to browse files.",
                ),
            )
            return

        include_deleted = self.state.show_deleted
        try:
            folders, files = list_folder(
                manifest, str(self.state.path), include_deleted=include_deleted,
            )
        except Exception as exc:
            self.list_listbox.append(
                self._make_empty_row(
                    f"Could not list this folder: {exc}", error=True,
                ),
            )
            return

        any_rows = False
        for folder in folders:
            any_rows = True
            self.list_listbox.append(self._make_folder_card_row(folder))

        for file_row in files:
            any_rows = True
            self.list_listbox.append(self._make_file_card_row(file_row))

        if not any_rows:
            if self.state.path:
                self.list_listbox.append(
                    self._make_empty_row(
                        "Folder is empty — click Upload to add a file.",
                    ),
                )
            else:
                self.list_listbox.append(
                    self._make_empty_row("No remote folders yet."),
                )

    def _make_empty_row(self, message: str, *, error: bool = False) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        label = Gtk.Label(
            label=message,
            xalign=0,
            wrap=True,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.add_css_class("error" if error else "dim-label")
        row.set_child(label)
        return row

    def _make_folder_card_row(self, folder: dict) -> Gtk.ListBoxRow:
        """Folder card: icon + name + 'Folder' subtitle + hamburger."""
        row = Gtk.ListBoxRow()
        row._vault_kind = "folder"  # type: ignore[attr-defined]
        row._vault_folder_path = str(folder["path"])  # type: ignore[attr-defined]
        row._vault_file_row = None  # type: ignore[attr-defined]

        body = self._make_card_body(
            icon_name="folder-symbolic",
            title=str(folder["name"]),
            subtitle_parts=["Folder"],
            dim=False,
        )
        # Hamburger menu — Download folder / Delete folder.
        body.append(self._make_row_menu_button(folder=folder))
        row.set_child(body)
        return row

    def _make_file_card_row(self, file_row: dict) -> Gtk.ListBoxRow:
        """File card: icon + name + 'size · modified · versions' subtitle + hamburger."""
        row = Gtk.ListBoxRow()
        row._vault_kind = "file"  # type: ignore[attr-defined]
        row._vault_file_row = dict(file_row)  # type: ignore[attr-defined]
        row._vault_folder_path = None  # type: ignore[attr-defined]

        deleted = str(file_row.get("status", "")) == "Deleted"
        size_label = _format_bytes(int(file_row.get("size", 0)))
        modified_label = format_local(file_row.get("modified")) or ""
        versions_label = ""
        version_count = int(file_row.get("versions", 0) or 0)
        if version_count > 1:
            versions_label = f"{version_count} versions"
        status_label = ""
        if deleted:
            recoverable = format_local(file_row.get("recoverable_until"))
            status_label = (
                f"Deleted — recoverable until {recoverable}"
                if recoverable else "Deleted"
            )

        parts = [p for p in (size_label, modified_label, versions_label, status_label) if p]
        body = self._make_card_body(
            icon_name="text-x-generic-symbolic",
            title=str(file_row.get("name", "")) or "(unnamed)",
            subtitle_parts=parts,
            dim=deleted,
        )
        body.append(self._make_row_menu_button(file_row=file_row))
        row.set_child(body)
        return row

    def _make_card_body(
        self,
        *,
        icon_name: str,
        title: str,
        subtitle_parts: list[str],
        dim: bool,
    ) -> Gtk.Box:
        """Build the inner row body: [icon] [title / subtitle] (hamburger appended by caller)."""
        body = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=8,
        )
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(24)
        if dim:
            icon.add_css_class("dim-label")
        body.append(icon)

        text_col = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
        )
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("title-4")
        if dim:
            title_label.add_css_class("dim-label")
        title_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        text_col.append(title_label)

        if subtitle_parts:
            subtitle_label = Gtk.Label(
                label=" · ".join(subtitle_parts), xalign=0, wrap=True,
            )
            subtitle_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            subtitle_label.add_css_class("dim-label")
            subtitle_label.add_css_class("caption")
            text_col.append(subtitle_label)

        body.append(text_col)
        return body

    def _make_row_menu_button(
        self,
        *,
        folder: dict | None = None,
        file_row: dict | None = None,
    ) -> Gtk.MenuButton:
        """Build the per-row hamburger MenuButton.

        Folders get Download / Delete; files get Download / Versions
        / Delete. Each menu item is a plain Gtk.Button inside a
        Gtk.Popover — we skip Gio actions because the row already
        carries the full target context, and a plain button keeps
        the wiring local + lambda-capturable.
        """
        menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            valign=Gtk.Align.CENTER,
        )
        menu_btn.add_css_class("flat")
        menu_btn.set_tooltip_text("Actions for this item")

        popover = Gtk.Popover()
        menu_btn.set_popover(popover)
        menu_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=4, margin_bottom=4, margin_start=4, margin_end=4,
        )
        popover.set_child(menu_box)

        def add_item(label: str, callback, css: str | None = None) -> None:
            btn = Gtk.Button(label=label, halign=Gtk.Align.FILL)
            btn.add_css_class("flat")
            if css is not None:
                btn.add_css_class(css)
            btn.set_has_frame(False)
            child = btn.get_first_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0)

            def on_click(_b) -> None:
                popover.popdown()
                callback()

            btn.connect("clicked", on_click)
            menu_box.append(btn)

        if folder is not None:
            folder_path = str(folder["path"])
            add_item(
                "Download folder",
                lambda: self._menu_action_download_folder(folder_path),
            )
            add_item(
                "Delete folder",
                lambda: self._menu_action_delete_folder(folder_path),
                css="destructive-action",
            )
        elif file_row is not None:
            captured = dict(file_row)
            deleted = str(captured.get("status", "")) == "Deleted"
            add_item(
                "Download",
                lambda: self._menu_action_download_file(captured),
            )
            if int(captured.get("versions", 0) or 0) > 0:
                add_item(
                    "Versions",
                    lambda: self._menu_action_versions(captured),
                )
            if not deleted:
                add_item(
                    "Delete",
                    lambda: self._menu_action_delete_file(captured),
                    css="destructive-action",
                )
        return menu_btn

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
