"""Activity tab — merged audit + op-log timeline (F-501).

Extracted from ``windows_vault.py`` (lines ~582–724).
"""

import threading
from datetime import datetime, timezone

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Pango

from ..vault.error_messages import humanize
from ._main_context import MainContext


def build_activity_tab(ctx: MainContext, win) -> "Gtk.Box":
    config = ctx.config
    config_dir = ctx.config_dir
    vault_id_undashed = ctx.vault_id_undashed

    from ..vault.state.activity import (
        ACTIVITY_KIND_PREFIXES,
        ActivityRow,
        filter_timeline,
        humanise_event_type,
        merge_timeline,
    )

    activity_tab = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=12,
        margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
    )
    activity_tab.append(Gtk.Label(
        label="Activity timeline",
        xalign=0, css_classes=["title-3"],
    ))
    activity_tab.append(Gtk.Label(
        label=(
            "Major operations on this vault: uploads, deletes, restore, "
            "device grants, eviction, purge. Sourced from the encrypted "
            "op-log in the head manifest."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))

    activity_filter_row = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
    )
    activity_search = Gtk.SearchEntry()
    activity_search.set_placeholder_text("Filter by filename…")
    activity_search.set_hexpand(True)
    activity_filter_row.append(activity_search)
    activity_refresh_btn = Gtk.Button(label="Refresh", css_classes=["pill"])
    activity_filter_row.append(activity_refresh_btn)
    activity_tab.append(activity_filter_row)

    activity_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    activity_tab.append(activity_status)

    activity_scroller = Gtk.ScrolledWindow(vexpand=True)
    activity_list_box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=4,
    )
    activity_scroller.set_child(activity_list_box)
    activity_tab.append(activity_scroller)

    activity_state: dict[str, Any] = {"rows": []}

    def _render_activity_rows(rows: list[ActivityRow]) -> None:
        child = activity_list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            activity_list_box.remove(child)
            child = next_child
        if not rows:
            empty = Gtk.Label(
                label="No activity yet. Once you upload, delete, or grant "
                      "access, entries will appear here.",
                xalign=0, wrap=True, css_classes=["dim-label"],
            )
            activity_list_box.append(empty)
            return
        for row in rows:
            row_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            )
            ts = datetime.fromtimestamp(
                row.timestamp_epoch or 0, tz=timezone.utc,
            ).strftime("%Y-%m-%d %H:%M") if row.timestamp_epoch else "—"
            ts_lbl = Gtk.Label(label=ts, xalign=0, css_classes=["dim-label"])
            ts_lbl.set_size_request(140, -1)
            row_box.append(ts_lbl)
            kind_lbl = Gtk.Label(
                label=humanise_event_type(row.event_type), xalign=0,
            )
            kind_lbl.set_size_request(220, -1)
            row_box.append(kind_lbl)
            detail_text = row.display_path or row.summary or ""
            if row.device_name:
                detail_text = f"{detail_text}  ({row.device_name})".strip()
            detail_lbl = Gtk.Label(
                label=detail_text, xalign=0, hexpand=True,
                ellipsize=Pango.EllipsizeMode.MIDDLE,
            )
            row_box.append(detail_lbl)
            activity_list_box.append(row_box)

    def _apply_activity_filter() -> None:
        search = activity_search.get_text().strip() or None
        filtered = filter_timeline(
            activity_state.get("rows") or [],
            filename_search=search,
        )
        _render_activity_rows(filtered)

    def _refresh_activity(_btn=None) -> None:
        if not vault_id_undashed:
            activity_status.set_label("No vault is connected.")
            _render_activity_rows([])
            return
        activity_status.set_label("Loading…")

        def worker() -> None:
            try:
                from ..vault.binding.runtime import (
                    create_vault_relay, open_local_vault_from_grant,
                )
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    manifest = vault.fetch_manifest(relay)
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                msg = humanize(exc)

                def fail() -> bool:
                    activity_status.set_label(
                        f"Could not load activity: {msg}"
                    )
                    return False
                GLib.idle_add(fail)
                return

            op_entries = list(manifest.get("operation_log_tail") or [])
            rows = merge_timeline(op_log_entries=op_entries)

            def succeed() -> bool:
                activity_state["rows"] = rows
                activity_status.set_label(f"{len(rows)} event(s).")
                _apply_activity_filter()
                return False
            GLib.idle_add(succeed)
        threading.Thread(target=worker, daemon=True).start()

    activity_refresh_btn.connect("clicked", _refresh_activity)
    activity_search.connect("search-changed", lambda _e: _apply_activity_filter())
    _refresh_activity()
    return activity_tab
