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


def _open_local_path(local_path: str) -> None:
    """Open ``local_path`` in the system file manager via gio."""
    from pathlib import Path
    from gi.repository import Gio
    try:
        Gio.AppInfo.launch_default_for_uri(Path(local_path).as_uri(), None)
    except Exception:  # noqa: BLE001
        pass


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
        suffix with Download folder / Delete folder.

        The Vault root row gets its own hamburger with "Create remote
        folder…" — without it, a brand-new empty vault offers no
        affordance to grow its first folder from the browser.
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
        else:
            # Root row: hamburger with "Create remote folder…".
            body.append(
                self._make_card_menu_button(
                    a11y_label="Actions for the vault root",
                    items=[(
                        "Create remote folder…",
                        lambda: self._open_add_folder_dialog(),
                        None,
                    )],
                ),
            )
        row.set_child(body)
        return row

    def _select_file(self, file_row: dict) -> None:
        """State-only setter.

        Callers that immediately follow with ``_render_all`` get the
        Details pane painted for free; callers that don't (the
        ``row-selected`` handler) must call ``_render_detail``
        explicitly. Keeping this state-only avoids the double-render
        per menu click that the earlier "render inside the setter"
        shape produced.
        """
        self.state.selected_file = file_row

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
        """Folder card: icon + name + 'Folder' subtitle + hamburger.

        Dim + "Deleted" subtitle match the file-card treatment for
        tombstoned items so users browsing in show-deleted mode see a
        consistent grayed-out shape across both kinds of row.
        """
        row = Gtk.ListBoxRow()
        row._vault_kind = "folder"  # type: ignore[attr-defined]
        row._vault_folder_path = str(folder["path"])  # type: ignore[attr-defined]
        row._vault_file_row = None  # type: ignore[attr-defined]

        deleted = bool(folder.get("deleted"))
        subtitle = "Deleted folder" if deleted else "Folder"
        body = self._make_card_body(
            icon_name="folder-symbolic",
            title=str(folder["name"]),
            subtitle_parts=[subtitle],
            dim=deleted,
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
        """Build the per-row hamburger MenuButton for a folder or file.

        Folders get Download / Delete; files get Download / Versions /
        Delete. The actual MenuButton + popover plumbing lives in
        ``_make_card_menu_button`` so the version-history rows (Wave
        3.7) can reuse the same shape with their own action set.
        """
        if folder is not None:
            target_name = str(folder.get("name") or folder.get("path") or "folder")
            a11y_label = f"Actions for folder {target_name}"
            folder_path = str(folder["path"])
            folder_deleted = bool(folder.get("deleted"))
            items: list[tuple[str, object, str | None]] = [
                (
                    "Download folder",
                    lambda: self._menu_action_download_folder(folder_path),
                    None,
                ),
            ]
            if folder_deleted:
                items.append((
                    "Restore folder",
                    lambda: self._menu_action_restore_folder(folder_path),
                    None,
                ))
            else:
                items.append((
                    "Delete folder",
                    lambda: self._menu_action_delete_folder(folder_path),
                    "destructive-action",
                ))
        elif file_row is not None:
            target_name = str(file_row.get("name", "")) or "file"
            a11y_label = f"Actions for file {target_name}"
            captured = dict(file_row)
            deleted = str(captured.get("status", "")) == "Deleted"
            items = [
                (
                    "Download",
                    lambda: self._menu_action_download_file(captured),
                    None,
                ),
            ]
            if int(captured.get("versions", 0) or 0) > 0:
                items.append((
                    "Versions",
                    lambda: self._menu_action_versions(captured),
                    None,
                ))
            if deleted:
                items.append((
                    "Restore",
                    lambda: self._menu_action_restore_file(captured),
                    None,
                ))
            else:
                items.append((
                    "Delete",
                    lambda: self._menu_action_delete_file(captured),
                    "destructive-action",
                ))
        else:
            a11y_label = "Actions for this item"
            items = []

        return self._make_card_menu_button(
            a11y_label=a11y_label, items=items,
        )

    def _make_card_menu_button(
        self,
        *,
        a11y_label: str,
        items: list[tuple[str, object, str | None]],
    ) -> Gtk.MenuButton:
        """Build a per-card hamburger MenuButton from an items list.

        ``items`` is a list of ``(label, callback, css_class)`` triples;
        ``css_class`` may be ``None`` for a plain menu item or a class
        like ``"destructive-action"``. The popover dismisses itself
        before invoking the callback so the click handler observes a
        clean window-focus state. No tooltip on the MenuButton: the
        view-more glyph is a universally recognised affordance, and a
        tooltip appearing right where the popover opens obscures the
        click target.

        F-U10: icon-only MenuButton needs an explicit accessible
        label — ``a11y_label`` (e.g. "Actions for file foo.txt",
        "Actions for version 2026-05-14 12:30") is what screen readers
        announce for the button.
        """
        menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            valign=Gtk.Align.CENTER,
        )
        menu_btn.add_css_class("flat")
        menu_btn.update_property([Gtk.AccessibleProperty.LABEL], [a11y_label])

        popover = Gtk.Popover()
        menu_btn.set_popover(popover)
        menu_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=4, margin_bottom=4, margin_start=4, margin_end=4,
        )
        popover.set_child(menu_box)

        for label, callback, css in items:
            btn = Gtk.Button(label=label, halign=Gtk.Align.FILL)
            btn.add_css_class("flat")
            if css is not None:
                btn.add_css_class(css)
            btn.set_has_frame(False)
            child = btn.get_first_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0)

            # Default-arg capture pins ``callback`` per iteration —
            # otherwise every button would invoke the last item's
            # lambda due to late-binding closure semantics.
            def on_click(_b, cb=callback) -> None:
                popover.popdown()
                cb()

            btn.connect("clicked", on_click)
            menu_box.append(btn)

        return menu_btn

    def _append_remote_folder_details(self, current_path: str) -> bool:
        """Render the Folders-tab folder detail widget (sizes + Local
        binding) into the right pane when ``current_path`` resolves to
        a top-level remote folder.

        Returns True when the widget was appended (caller skips the
        legacy "Current folder" hint), False otherwise.
        """
        assert self.detail_box is not None
        manifest = self.state.manifest
        if not manifest:
            return False
        top_segment = current_path.split("/", 1)[0]
        if not top_segment:
            return False

        from ..vault.folder.ui_state import folder_rows_from_cache
        from ..vault.state.usage import calculate_vault_usage
        from ..vault_folders.details import append_folder_details

        target_folder: dict | None = None
        for folder in manifest.get("remote_folders", []) or []:
            if not isinstance(folder, dict):
                continue
            if str(folder.get("state", "active")) != "active":
                continue
            if str(folder.get("display_name_enc") or "") == top_segment:
                target_folder = folder
                break
        if target_folder is None:
            return False
        remote_folder_id = str(target_folder.get("remote_folder_id") or "")
        if not remote_folder_id:
            return False

        # Stats come from the in-memory manifest the browser already
        # holds, so we don't re-fetch.
        usage = calculate_vault_usage(manifest).by_folder
        folder_rows = folder_rows_from_cache(
            [target_folder], usage_by_folder=usage,
        )
        if not folder_rows:
            return False
        folder_row = folder_rows[0]

        self.detail_box.append(Gtk.Label(
            label=folder_row["name"], xalign=0, css_classes=["title-3"],
        ))

        ctx = self._build_folders_ctx(remote_folder_id, manifest)
        append_folder_details(
            self.detail_box, ctx,
            remote_folder_id=remote_folder_id, folder_row=folder_row,
        )
        return True

    def _build_folders_ctx(self, remote_folder_id: str, manifest: dict):
        """Construct a ``FoldersContext`` shim for the shared detail +
        action helpers.

        Reuses the same dataclass the Folders tab populates — the
        binding-row actions (sync now / pause / resume / disconnect)
        and the connect-local dialog all read from this shape, so a
        shim lets the browser piggyback on the exact same code paths
        without copy-pasting them.
        """
        from ..vault.binding.lifecycle import BindingCancellationRegistry
        from ..vault.folder.runtime import VaultRuntime
        from ..vault_folders.context import FoldersContext

        vault_id = self._resolve_vault_id() or ""

        # Cached lazily — folder selections within a single browser
        # session can share the same VaultRuntime + cancellation
        # registry, so an in-flight sync stays trackable as the user
        # clicks around.
        cached = getattr(self, "_folders_ctx_cache", None)
        if cached is None or cached.vault_id != vault_id:
            runtime = VaultRuntime(
                config_dir=self.config_dir,
                config=self.config,
                vault_id=vault_id,
                local_index=self.local_index,
            )
            cached = FoldersContext(
                app=self._app,
                parent_window=self.win,
                config_dir=self.config_dir,
                config=self.config,
                vault_id=vault_id,
                local_index=self.local_index,
                runtime=runtime,
                cancellation_registry=BindingCancellationRegistry(),
            )
            cached.set_sidebar_status = lambda msg, kind="dim-label": self._set_status(msg, kind)
            cached.set_content_status = lambda msg, kind="dim-label": self._set_status(msg, kind)
            cached.refresh_all = lambda *_a, **_kw: self._refresh_manifest_async()
            cached.open_browse_local = _open_local_path
            self._folders_ctx_cache = cached  # type: ignore[attr-defined]

        cached.selection_state["folder_id"] = remote_folder_id
        cached.folder_rows_by_id = {remote_folder_id: None}
        return cached

    def _render_detail(self, file_row: dict | None) -> None:
        if self.detail_box is None:
            return
        self._clear_box(self.detail_box)
        # v1 line 299: clear Versions sensitivity at the top of every
        # render; the versions section flips it back on if/when it
        # finds history. Same shape preserved here.
        if self.versions_btn is not None:
            self.versions_btn.set_sensitive(False)
        # Wave 3.5: drop any stale Versions-heading reference — the
        # previous label is about to be removed from the box.
        self._versions_heading_label = None

        if not file_row:
            current_path = str(self.state.path)
            if self.download_btn is not None:
                self.download_btn.set_sensitive(bool(current_path))
            if current_path and self._append_remote_folder_details(current_path):
                return
            self.detail_box.append(Gtk.Label(
                label="Details", xalign=0, css_classes=["title-3"],
            ))
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

        # Wave 3.5: stash this heading on the orchestrator so the
        # per-row "Versions" menu item can scroll to it after the
        # render pass settles.
        versions_heading = Gtk.Label(
            label="Versions", xalign=0, css_classes=["title-3"],
        )
        self._versions_heading_label = versions_heading
        self.detail_box.append(versions_heading)
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

        # Wave 3.7 (2026-05-14): the version history is now a
        # `boxed-list` Gtk.ListBox of card rows matching the main
        # file pane's geometry. Each card carries the version date
        # as title, `Device · Size · Status` as subtitle, and a
        # per-row hamburger menu with Download / Restore actions
        # (built via the shared `_make_card_menu_button` helper).
        # Selection is disabled — the menu drives interaction;
        # row-activate would clobber a click on the MenuButton.
        versions_listbox = Gtk.ListBox(hexpand=True)
        versions_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        versions_listbox.add_css_class("boxed-list")
        versions_listbox.set_margin_top(8)
        versions_listbox.set_margin_bottom(8)
        self.detail_box.append(versions_listbox)

        entry_deleted = bool(file_row.get("deleted"))
        for version in versions:
            versions_listbox.append(
                self._make_version_row(file_row, version, entry_deleted),
            )

    def _make_version_row(
        self,
        file_row: dict,
        version: dict,
        entry_deleted: bool,
    ) -> Gtk.ListBoxRow:
        """Build a single version card for the detail-pane history list."""
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        modified = format_local(version.get("modified")) or "Unknown date"
        device = str(version.get("author_device_id") or "")
        device_part = f"Device {device[:12]}" if device else ""
        size_part = _format_bytes(int(version.get("size", 0) or 0))
        is_current = bool(version.get("is_current"))
        if entry_deleted and is_current:
            status_part = "Latest (deleted)"
        elif is_current:
            status_part = "Current"
        else:
            status_part = "Previous"

        parts = [p for p in (device_part, size_part, status_part) if p]
        body = self._make_card_body(
            icon_name="text-x-generic-symbolic",
            title=modified,
            subtitle_parts=parts,
            dim=False,
        )

        # Build the action set — current-non-deleted has no download
        # (you already have this file) and no restore (it IS the
        # current). The "deleted entry's latest version" case is the
        # exception: download + restore are both meaningful because
        # the visible file is a tombstone.
        items: list[tuple[str, object, str | None]] = []
        if not is_current or entry_deleted:
            items.append((
                "Download this version",
                lambda v=dict(version), f=dict(file_row):
                    self._choose_version_destination(f, v),
                None,
            ))
        show_restore = (not is_current) or entry_deleted
        if show_restore:
            items.append((
                "Restore as current",
                lambda v=dict(version), f=dict(file_row):
                    self._confirm_restore_version(f, v),
                None,
            ))

        if items:
            body.append(self._make_card_menu_button(
                a11y_label=f"Actions for version {modified}",
                items=items,
            ))

        row.set_child(body)
        return row
