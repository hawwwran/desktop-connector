"""LayoutMixin — header bar chrome + selection-driven action bar + panes.

Wave 1 of the Vault Browser chrome redesign
(`docs/plans/vault-browser-chrome-redesign.md`, 2026-05-13).

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
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402


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

        # --- Start edge: Back ---------------------------------------------
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
        overflow_menu.append("Refresh", "win.refresh")
        overflow_menu.append("Show deleted", "win.show-deleted")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_menu_model(overflow_menu)
        menu_button.add_css_class("flat")
        menu_button.set_tooltip_text("More")
        header_bar.pack_end(menu_button)

        # Refresh action backs the menu item; the off-tree
        # ``self.refresh_btn`` mirrors its sensitivity for legacy callers.
        refresh_action = Gio.SimpleAction.new("refresh", None)
        refresh_action.connect(
            "activate", lambda *_a: self._refresh_manifest_async(),
        )
        self.win.add_action(refresh_action)

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

        # --- Selection-driven action bar (Gtk.Revealer below banners) ----
        # Built here but appended to ``self.outer`` by
        # ``_build_breadcrumb_and_status`` so it sits below the banners
        # and just above the file list. Created up-front so the
        # ``download_btn`` / ``versions_btn`` / ``delete_btn`` slots are
        # populated before any other mixin's first sensitivity poke.
        self.selection_actions_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            reveal_child=False,
        )
        selection_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=4,
            margin_bottom=4,
            halign=Gtk.Align.END,
        )

        self.download_btn = Gtk.Button(
            label="Download", css_classes=["pill", "suggested-action"],
        )
        self.download_btn.set_tooltip_text(
            "Download selected file or current folder",
        )
        self.download_btn.connect("clicked", self._choose_download_destination)
        self.download_btn.set_sensitive(False)
        selection_bar.append(self.download_btn)

        self.versions_btn = Gtk.Button(label="Versions", css_classes=["pill"])
        self.versions_btn.set_tooltip_text("Choose a version below to download")
        self.versions_btn.set_sensitive(False)
        selection_bar.append(self.versions_btn)

        self.delete_btn = Gtk.Button(
            label="Delete", css_classes=["pill", "destructive-action"],
        )
        self.delete_btn.set_tooltip_text(
            "Soft-delete the selected file or current folder",
        )
        self.delete_btn.connect("clicked", self._confirm_and_delete)
        self.delete_btn.set_sensitive(False)
        selection_bar.append(self.delete_btn)

        self.selection_actions_revealer.set_child(selection_bar)

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

        self.breadcrumb = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.breadcrumb.add_css_class("title-4")
        self.outer.append(self.breadcrumb)

        # Selection-driven action bar sits between the breadcrumb and
        # the status / progress rows so contextual actions live close
        # to the content they act on.
        if self.selection_actions_revealer is not None:
            self.outer.append(self.selection_actions_revealer)

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
