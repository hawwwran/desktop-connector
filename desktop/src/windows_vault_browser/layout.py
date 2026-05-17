"""LayoutMixin — header bar chrome + selection-driven action bar + panes.

Wave 1 of the Vault Browser chrome redesign
(`temp/finished-plans/vault-browser-chrome-redesign.md`, 2026-05-13).

The toolbar is no longer a body `Gtk.Box` strip of 8 pill buttons. It
is now:
- An `Adw.HeaderBar` with Back on the start side, an `Adw.SplitButton`
  "Upload" (primary = file, dropdown = folder) and a hamburger
  `Gtk.MenuButton` (Refresh + Show deleted) on the end side.
- A `Gtk.Revealer` below the banners holding contextual Download /
  Versions / Delete buttons, hidden when nothing is selected and no
  remote-folder context is active.

Slot names (``self.upload_btn``, ``self.delete_btn`` …) are preserved
so the other mixins (`uploads.py`, `downloads.py`, `delete_restore.py`,
`quota.py`, `resume_banner.py`, `panes.py`) keep working unchanged.
For widgets that no longer have a presence in the widget tree
(``upload_folder_btn`` is folded into the SplitButton's menu;
``refresh_btn`` is folded into the hamburger menu;
``show_deleted_toggle`` is folded into the hamburger toggle), the slot
holds an off-tree `Gtk.Button` / `Gtk.CheckButton` and a
``notify::sensitive`` bridge mirrors its state onto the matching
`Gio.SimpleAction` that backs the menu item.

The forward-button slot is set to ``None`` per plan — file-manager
forward is a web-browser metaphor that no modern GNOME file app ships.
``_update_nav_buttons`` already no-ops when ``forward_btn is None``.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402


class LayoutMixin:
    """Builds the static widget skeleton.

    Mixin — relies on the orchestrator's ``__init__`` to have set up
    every widget slot (``self.outer``, ``self.upload_btn`` …) as
    ``None`` so the asserts inside the builders catch out-of-order
    activation. Also reads ``self._toolbar_view`` (stashed in
    ``_on_activate``) to attach the header bar.
    """

    def _build_action_bar(self) -> None:
        assert self.outer is not None
        assert self.win is not None
        assert self._toolbar_view is not None

        header_bar = Adw.HeaderBar()
        # Window title widget — Wave 1.5 absorbs the former body
        # breadcrumb into the header chrome. Title stays "Vault";
        # subtitle is the current path (empty when at root).
        self._window_title = Adw.WindowTitle(title="Vault", subtitle="")
        header_bar.set_title_widget(self._window_title)

        # --- Start edge: sidebar toggle (Wave 2.5) + Back -----------------
        # Wave 2.5: when the OverlaySplitView is collapsed (narrow
        # window), the sidebar slides over the content. This toggle
        # button reveals / hides it. The button is bound to the
        # split-view's ``collapsed`` (visibility) and ``show-sidebar``
        # (active state) properties so it auto-hides on wide windows
        # and tracks sidebar state when shown.
        self._sidebar_toggle_btn = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic",
        )
        self._sidebar_toggle_btn.add_css_class("flat")
        self._sidebar_toggle_btn.set_tooltip_text("Show folder sidebar")
        # F-U10: icon-only ToggleButton — tooltip alone isn't an
        # accessible name. Bind an explicit Gtk.AccessibleProperty.LABEL
        # so AT-SPI / dogtail see "Toggle folder sidebar" instead of an
        # empty-named togglebutton.
        self._sidebar_toggle_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Toggle folder sidebar"],
        )
        self._sidebar_toggle_btn.set_visible(False)
        header_bar.pack_start(self._sidebar_toggle_btn)

        self.back_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_btn.add_css_class("flat")
        self.back_btn.set_tooltip_text("Back to previous folder")
        self.back_btn.connect("clicked", self._on_back_clicked)
        header_bar.pack_start(self.back_btn)

        # Forward is dropped per plan; slot stays None so
        # ``_update_nav_buttons`` no-ops the Forward branch.
        self.forward_btn = None

        # --- End edge: hamburger then SplitButton (right-to-left pack) ---
        # ``pack_end`` packs from right toward center, so the first
        # packed widget ends up at the far right. Pack the hamburger
        # first so the SplitButton lands to the left of it (primary
        # action visually leftmost of the end cluster).
        overflow_menu = Gio.Menu()
        overflow_menu.append("Add remote folder…", "win.add-folder")
        overflow_menu.append("Refresh", "win.refresh")
        overflow_menu.append("Show deleted", "win.show-deleted")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_menu_model(overflow_menu)
        menu_button.add_css_class("flat")
        menu_button.set_tooltip_text("More")
        header_bar.pack_end(menu_button)

        # Wave 3.1: fixed-position status icon. Sits between the
        # hamburger and the upload group so layout never shifts on
        # success / error / info state changes — only the icon glyph
        # + tooltip update. Hidden when idle (no message) so the
        # chrome is uncluttered.
        self._status_icon = Gtk.Image()
        self._status_icon.set_pixel_size(16)
        # F-U10: bare Gtk.Image isn't named for AT-SPI. Bind a label
        # so screen readers announce "Status indicator" plus whatever
        # description ``_update_status_icon`` writes onto the tooltip
        # (which AT-SPI surfaces as ``accessible-description``).
        self._status_icon.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Status indicator"],
        )
        self._status_icon.set_visible(False)
        header_bar.pack_end(self._status_icon)

        # Refresh action backs the menu item; the off-tree
        # ``self.refresh_btn`` mirrors its sensitivity for legacy callers.
        refresh_action = Gio.SimpleAction.new("refresh", None)
        refresh_action.connect(
            "activate", lambda *_a: self._refresh_manifest_async(),
        )
        self.win.add_action(refresh_action)

        # "Add remote folder…" opens the same dialog the Folders tab in
        # Vault Settings uses, so a brand-new vault can grow a first
        # folder without round-tripping through Settings.
        add_folder_action = Gio.SimpleAction.new("add-folder", None)
        add_folder_action.connect(
            "activate", lambda *_a: self._open_add_folder_dialog(),
        )
        self.win.add_action(add_folder_action)
        self._add_folder_action = add_folder_action

        # Show-deleted is a stateful boolean action; the menu binds it
        # automatically as a check-style row. The off-tree
        # ``self.show_deleted_toggle`` reflects state for legacy callers.
        show_deleted_action = Gio.SimpleAction.new_stateful(
            "show-deleted", None, GLib.Variant.new_boolean(False),
        )
        show_deleted_action.connect(
            "change-state", self._on_show_deleted_action_change,
        )
        self.win.add_action(show_deleted_action)
        self._show_deleted_action = show_deleted_action

        # SplitButton: primary click = Upload (file), arrow = popover with
        # "Upload folder…". Set the SplitButton itself as ``upload_btn``
        # so existing ``set_sensitive`` calls disable the whole control.
        upload_menu = Gio.Menu()
        upload_menu.append("Upload folder…", "win.upload-folder")

        self.upload_btn = Adw.SplitButton(label="Upload")
        self.upload_btn.add_css_class("suggested-action")
        self.upload_btn.set_menu_model(upload_menu)
        self.upload_btn.set_tooltip_text(
            "Upload a file into the current folder. Use the arrow for "
            "Upload folder.",
        )
        self.upload_btn.connect(
            "clicked", lambda _b: self._choose_upload_source(None),
        )
        self.upload_btn.set_sensitive(False)
        header_bar.pack_end(self.upload_btn)

        upload_folder_action = Gio.SimpleAction.new("upload-folder", None)
        upload_folder_action.connect(
            "activate", lambda *_a: self._choose_upload_folder_source(None),
        )
        self.win.add_action(upload_folder_action)
        self._upload_folder_action = upload_folder_action

        # --- Off-tree compatibility slots ---------------------------------
        # The other mixins call ``set_sensitive`` on these — give them
        # a real widget to call into, and bridge sensitivity changes
        # onto the matching Gio actions so the menu items follow.
        self.upload_folder_btn = Gtk.Button()
        self.upload_folder_btn.connect(
            "notify::sensitive",
            lambda btn, _p: upload_folder_action.set_enabled(
                btn.get_sensitive(),
            ),
        )
        self.upload_folder_btn.set_sensitive(False)

        self.refresh_btn = Gtk.Button()
        self.refresh_btn.connect(
            "notify::sensitive",
            lambda btn, _p: refresh_action.set_enabled(btn.get_sensitive()),
        )
        self.refresh_btn.set_sensitive(True)

        self.show_deleted_toggle = Gtk.CheckButton()
        self.show_deleted_toggle.connect(
            "toggled", self._on_show_deleted_toggle_bridge,
        )

        self._toolbar_view.add_top_bar(header_bar)
        self._header_bar = header_bar

        # --- Off-tree compatibility slots for download / versions / delete --
        # Wave 3.4 (2026-05-13): the global selection-driven action bar
        # is retired — per-row hamburger menus on file cards + sidebar
        # folder rows own these actions now. The slots stay as off-tree
        # Gtk.Buttons so the dozens of legacy ``set_sensitive`` calls in
        # ``downloads.py`` / ``delete_restore.py`` / ``quota.py`` /
        # ``resume_banner.py`` / ``panes.py`` keep working without a
        # sweeping rewrite — they just affect a widget no longer in the
        # tree, which is harmless.
        self.download_btn = Gtk.Button()
        self.versions_btn = Gtk.Button()
        self.delete_btn = Gtk.Button()
        self.selection_actions_revealer = None

        # Legacy slot: ``self.action_bar`` used to be the body container
        # for the 8-button strip. Nothing reads it after the rewrite, so
        # leave the attribute at ``None`` (initialised in __init__).
        self._update_nav_buttons()

    def _on_show_deleted_action_change(
        self, action: Gio.SimpleAction, value: GLib.Variant,
    ) -> None:
        """Hamburger toggle path: action state change → state update."""
        action.set_state(value)
        new_active = bool(value.get_boolean())
        # Mirror onto the off-tree CheckButton so legacy code reading
        # ``self.show_deleted_toggle.get_active()`` sees the new value.
        # Block the bridge handler to avoid an action↔toggle pingpong.
        if self.show_deleted_toggle is not None:
            self.show_deleted_toggle.handler_block_by_func(
                self._on_show_deleted_toggle_bridge,
            )
            self.show_deleted_toggle.set_active(new_active)
            self.show_deleted_toggle.handler_unblock_by_func(
                self._on_show_deleted_toggle_bridge,
            )
        self.state.show_deleted = new_active
        self.state.selected_file = None
        self._render_all()

    def _on_show_deleted_toggle_bridge(self, button: Gtk.CheckButton) -> None:
        """Bridge: off-tree CheckButton.toggled → Gio action state."""
        new_active = bool(button.get_active())
        if self._show_deleted_action is not None:
            self._show_deleted_action.set_state(
                GLib.Variant.new_boolean(new_active),
            )
        # The action's ``change-state`` handler does the state-update
        # + ``_render_all``; call directly here since ``set_state``
        # does not fire ``change-state``.
        self.state.show_deleted = new_active
        self.state.selected_file = None
        self._render_all()

    def _build_breadcrumb_and_status(self) -> None:
        assert self.outer is not None

        # Ambient "Vault sync K/N" banner above the resume banner.
        # Driven by SyncStatusBannerMixin's GLib poll; hidden when the
        # pending-ops queue is empty. Spec:
        # docs/plans/vault-large-folder-perf.md Phase 1.5.
        self.sync_status_banner_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.sync_status_banner_box.add_css_class("card")
        self.sync_status_banner_box.set_visible(False)
        self.sync_status_banner_label = Gtk.Label(
            xalign=0, hexpand=True, wrap=False,
            margin_top=10,
            margin_bottom=10,
            margin_start=16,
            margin_end=16,
        )
        self.sync_status_banner_box.append(self.sync_status_banner_label)
        self.outer.append(self.sync_status_banner_box)

        # Resume "banner" — a custom horizontal box that pairs the
        # message with both Resume and Cancel actions. Adw.Banner is
        # single-action by design, so we roll our own to surface the
        # Cancel branch alongside Resume. The box uses the "card" CSS
        # class for a subtle visual treatment that distinguishes it
        # from the surrounding content without painting it as a
        # warning. Hidden until ``_refresh_resume_banner`` reveals it.
        # The ``card`` CSS class paints the rounded white background but
        # adds no internal padding — children sit flush with the
        # border by default. Set margins on each child instead; GTK4
        # grows the parent's allocation to include child margins, which
        # gives the visual "interior padding" effect (the card paints
        # around the gap).
        self.resume_banner_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.resume_banner_box.add_css_class("card")
        self.resume_banner_box.set_visible(False)

        self.resume_banner_label = Gtk.Label(
            xalign=0, hexpand=True, wrap=True,
            margin_top=12,
            margin_bottom=12,
            margin_start=16,
            margin_end=4,
        )
        self.resume_banner_box.append(self.resume_banner_label)

        self.resume_cancel_btn = Gtk.Button(
            label="Cancel",
            css_classes=["pill", "destructive-action"],
            margin_top=8,
            margin_bottom=8,
        )
        self.resume_cancel_btn.set_tooltip_text(
            "Discard the saved session(s). The local file is unchanged "
            "— start a fresh upload anytime.",
        )
        self.resume_cancel_btn.connect(
            "clicked", self._on_resume_cancel_clicked,
        )
        self.resume_banner_box.append(self.resume_cancel_btn)

        self.resume_resume_btn = Gtk.Button(
            label="Resume",
            css_classes=["pill", "suggested-action"],
            margin_top=8,
            margin_bottom=8,
            margin_end=12,
        )
        self.resume_resume_btn.connect(
            "clicked", lambda _b: self._start_resume_pending(),
        )
        self.resume_banner_box.append(self.resume_resume_btn)

        self.outer.append(self.resume_banner_box)

        self.quota_banner = Adw.Banner.new("")
        self.quota_banner.set_revealed(False)
        self.outer.append(self.quota_banner)

        # Wave 1.5: body breadcrumb removed — current path now lives
        # as the subtitle of self._window_title in the header bar.
        # self.breadcrumb slot stays at None for backward compatibility
        # with any straggler references.

        # Wave 3.4: body status label retired — status surfaces as the
        # fixed-position icon on the header bar (Wave 3.1). Legacy
        # ``self.status_label`` slot stays None; ``_set_status`` no-ops
        # the body branch on None and drives the icon directly.
        self.status_label = None

        # Wave 3.4: selection-action revealer retired — Download /
        # Versions / Delete live on per-row hamburger menus now.

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
        # Wave 2.5 (2026-05-13): the outer two-pane split is now an
        # Adw.OverlaySplitView so the sidebar slides over the content
        # when the window narrows past the breakpoint defined in
        # ``_on_activate``. On a wide window it behaves like the prior
        # Gtk.Paned — fixed sidebar, content fills the rest. On narrow
        # windows, the sidebar collapses and a toggle button in the
        # header bar (also added in Wave 2.5) reveals / hides it.
        self.split_view = Adw.OverlaySplitView()
        # Marker class so brand.py's `.vault-folder-split > .sidebar`
        # rule can transparentize the pane Adwaita would otherwise
        # paint with @sidebar_bg_color.
        self.split_view.add_css_class("vault-folder-split")
        self.split_view.set_vexpand(True)
        self.split_view.set_sidebar_width_fraction(0.25)
        self.split_view.set_min_sidebar_width(180.0)
        self.split_view.set_max_sidebar_width(360.0)
        self.outer.append(self.split_view)

        # Left: folder sidebar — Wave 2 chrome redesign. A Gtk.ListBox
        # styled with the Adwaita ``navigation-sidebar`` CSS class so
        # rows pick up the same look as Files / Calendar / Settings
        # sidebars (icon + label + selection highlight + hover state).
        # Row activation drives ``_navigate_to`` via the
        # ``row-activated`` signal, with each row carrying its target
        # path as a Python attribute set in ``_make_tree_row``.
        tree_scroller = Gtk.ScrolledWindow(min_content_width=160)
        tree_scroller.add_css_class("vault-folder-sidebar-wrap")
        self.tree_listbox = Gtk.ListBox()
        self.tree_listbox.add_css_class("navigation-sidebar")
        # Marker class so brand.py can paint the rounded white card
        # treatment that matches the right-hand boxed-list file pane.
        # Pair with margins matching ``self.list_listbox`` below so the
        # left and right cards line up vertically inside the
        # OverlaySplitView (whose own sidebar-pane background is
        # transparentized in the same CSS rule).
        self.tree_listbox.add_css_class("vault-folder-sidebar")
        self.tree_listbox.set_margin_top(8)
        self.tree_listbox.set_margin_bottom(8)
        self.tree_listbox.set_margin_start(8)
        self.tree_listbox.set_margin_end(8)
        self.tree_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.tree_listbox.connect(
            "row-activated", self._on_tree_row_activated,
        )
        tree_scroller.set_child(self.tree_listbox)
        self.split_view.set_sidebar(tree_scroller)

        # Right: split between file list (center) and detail (right).
        right = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.split_view.set_content(right)

        # Wave 3.2: center file list is now a Gtk.ListBox of cards
        # (replaces the former 5-column Gtk.Grid). Each row carries
        # its own title/subtitle/hamburger; the listbox's
        # ``row-selected`` signal drives the right-hand Details pane.
        list_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.list_listbox = Gtk.ListBox(
            hexpand=True, vexpand=True,
        )
        self.list_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_listbox.add_css_class("boxed-list")
        self.list_listbox.set_margin_top(8)
        self.list_listbox.set_margin_bottom(8)
        self.list_listbox.set_margin_start(8)
        self.list_listbox.set_margin_end(8)
        self.list_listbox.connect("row-selected", self._on_list_row_selected)
        self.list_listbox.connect("row-activated", self._on_list_row_activated)
        list_scroller.set_child(self.list_listbox)

        # Wave 3.6: loading affordance. The manifest fetch is the
        # gating remote call between window open and first paint. On
        # a slow relay we used to leave the centre pane visually
        # empty for seconds, so users read it as "broken" rather
        # than "loading". A Gtk.Stack swaps between:
        #   - "loading" — Gtk.Spinner + a column of pulsing skeleton
        #     rows shaped like the real folder/file cards.
        #   - "content" — the real ``list_listbox`` scroller.
        # `_refresh_manifest_async` flips to "loading" only when
        # ``state.manifest is None`` so subsequent refreshes don't
        # blank the existing view; the header status icon already
        # surfaces in-flight work in that case.
        loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=24,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
            valign=Gtk.Align.START,
        )
        spinner_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            margin_bottom=4,
        )
        self._loading_spinner = Gtk.Spinner()
        self._loading_spinner.set_size_request(20, 20)
        spinner_row.append(self._loading_spinner)
        loading_label = Gtk.Label(label="Loading vault…")
        loading_label.add_css_class("dim-label")
        spinner_row.append(loading_label)
        loading_box.append(spinner_row)
        # Skeleton cards: same outer shape as a real folder/file row
        # (boxed-list pill, ~56px tall) so the layout doesn't jump
        # when the real rows replace them.
        for _ in range(4):
            sk = Gtk.Box()
            sk.add_css_class("vault-skeleton-row")
            sk.set_size_request(-1, 56)
            sk.set_hexpand(True)
            loading_box.append(sk)

        self.list_stack = Gtk.Stack()
        self.list_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.list_stack.set_transition_duration(180)
        self.list_stack.set_hhomogeneous(True)
        self.list_stack.set_vhomogeneous(False)
        self.list_stack.add_named(loading_box, "loading")
        self.list_stack.add_named(list_scroller, "content")
        self.list_stack.set_visible_child_name("content")

        right.set_start_child(self.list_stack)
        right.set_resize_start_child(True)
        right.set_shrink_start_child(True)

        # Wave 3.5: stash the detail scroller on self so
        # ``_scroll_to_versions`` can drive its vertical adjustment
        # when the Versions menu item is activated.
        self.detail_scroller = Gtk.ScrolledWindow(min_content_width=200)
        detail_scroller = self.detail_scroller
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
        # Wave 3.7 (2026-05-14): divider lowered 540 → 500 to give
        # the detail pane more horizontal room out of the gate. With
        # the window default also widened to 1280, the detail pane
        # now lands at ~460px (was ~240px) — enough for the
        # Versions card list's subtitle row to fit on one line.
        right.set_position(500)
