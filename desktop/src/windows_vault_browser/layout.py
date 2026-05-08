"""LayoutMixin — header/action-bar + breadcrumb/status + paned panes.

Extracted from the original ``windows_vault_browser/app.py`` mixin
extraction (2026-05-08). These three builders run once during
``_on_activate`` and own widget construction only — no business
logic or threading. Cross-mixin couplings (e.g. ``_on_back_clicked``
lives in the orchestrator, ``_choose_upload_source`` in
``UploadsMixin``) resolve through MRO at runtime.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402


class LayoutMixin:
    """Builds the static widget skeleton.

    Mixin — relies on the orchestrator's ``__init__`` to have set up
    every widget slot (``self.outer``, ``self.action_bar`` …) as
    ``None`` so the asserts inside the builders catch out-of-order
    activation.
    """

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
