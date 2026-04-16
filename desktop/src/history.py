"""
Transfer history tracking and GTK4/libadwaita display.
"""

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_HISTORY = 50


class TransferHistory:
    """Persistent transfer history (JSON file, max 50 items)."""

    def __init__(self, config_dir: Path):
        self.history_file = config_dir / "history.json"
        self._lock = threading.Lock()
        self._items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self.history_file.exists():
            try:
                return json.loads(self.history_file.read_text())
            except Exception:
                log.warning("Failed to load history, starting fresh")
        return []

    def _save(self) -> None:
        self.history_file.write_text(json.dumps(self._items, indent=2))

    @property
    def items(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def add(self, filename: str, display_label: str, direction: str,
            size: int, content_path: str = "", sender_id: str = "",
            transfer_id: str = "", status: str = "complete",
            chunks_downloaded: int = 0, chunks_total: int = 0) -> None:
        with self._lock:
            self._items.insert(0, {
                "filename": filename,
                "display_label": display_label,
                "direction": direction,
                "size": size,
                "content_path": content_path,
                "sender_id": sender_id,
                "transfer_id": transfer_id,
                "status": status,
                "chunks_downloaded": chunks_downloaded,
                "chunks_total": chunks_total,
                "delivered": direction == "received" and status == "complete",
                "timestamp": int(time.time()),
            })
            self._items = self._items[:MAX_HISTORY]
            self._save()

    def update(self, transfer_id: str, **fields) -> bool:
        """Update an existing history entry by transfer_id. Returns True if found."""
        with self._lock:
            for item in self._items:
                if item.get("transfer_id") == transfer_id:
                    item.update(fields)
                    self._save()
                    return True
        return False

    def mark_delivered(self, transfer_id: str) -> bool:
        """Mark a sent transfer as delivered. Returns True if found and updated."""
        with self._lock:
            for item in self._items:
                if item.get("transfer_id") == transfer_id and not item.get("delivered"):
                    item["delivered"] = True
                    self._save()
                    return True
        return False

    def get_undelivered_transfer_ids(self) -> list[str]:
        """Get transfer_ids of sent items not yet marked delivered."""
        # Reload from disk to pick up transfers added by other processes (--send, subprocesses)
        self._items = self._load()
        with self._lock:
            return [
                item["transfer_id"] for item in self._items
                if item.get("direction") == "sent"
                and item.get("transfer_id")
                and not item.get("delivered")
            ]

    def remove(self, item: dict) -> None:
        """Remove a specific item from history."""
        with self._lock:
            self._items = [i for i in self._items if i is not item and i.get("timestamp") != item.get("timestamp")]
            self._save()

    def get_label(self, item: dict) -> str:
        return item.get("display_label") or item.get("filename", "Unknown")


def show_history_window(history: TransferHistory, on_resend_clipboard: callable = None) -> None:
    """Show transfer history in a libadwaita window."""
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, Pango

    app = Gtk.Application(application_id="com.desktopconnector.history")

    def on_activate(app):
        win = Adw.ApplicationWindow(application=app, title="Transfer History",
                                     default_width=500, default_height=480)

        toolbar_view = Adw.ToolbarView()
        win.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar_view.set_content(main_box)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scroll)

        clamp = Adw.Clamp(maximum_size=500, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        scroll.set_child(clamp)

        group = Adw.PreferencesGroup()
        clamp.set_child(group)

        items = history.items
        item_rows = {}
        selected_item = [None]

        if not items:
            row = Adw.ActionRow(title="No transfers yet")
            row.add_css_class("dim-label")
            group.add(row)
        else:
            for item in items:
                direction_icon = "go-down-symbolic" if item["direction"] == "received" else "go-up-symbolic"
                direction_prefix = "\u2193" if item["direction"] == "received" else "\u2191"
                label = history.get_label(item)
                size = _format_size(item.get("size", 0))
                ts = time.strftime("%b %d, %H:%M", time.localtime(item.get("timestamp", 0)))

                row = Adw.ActionRow(
                    title=f"{direction_prefix}  {label}",
                    subtitle=f"{size}  \u00b7  {ts}",
                )
                row.set_title_lines(1)

                is_clipboard = item.get("filename", "").startswith(".fn.clipboard")
                has_path = item.get("content_path") and Path(item["content_path"]).exists()

                if is_clipboard and has_path and on_resend_clipboard:
                    btn = Gtk.Button(label="Resend", valign=Gtk.Align.CENTER)
                    btn.add_css_class("flat")
                    cp = item["content_path"]
                    btn.connect("clicked", lambda b, p=cp: on_resend_clipboard(Path(p)))
                    row.add_suffix(btn)

                group.add(row)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def _format_size(bytes: int) -> str:
    if bytes < 1024:
        return f"{bytes} B"
    if bytes < 1024 * 1024:
        return f"{bytes // 1024} KB"
    return f"{bytes / (1024 * 1024):.1f} MB"
