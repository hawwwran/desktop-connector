"""
Transfer history tracking and GTK4/libadwaita display.
"""

import fcntl
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_HISTORY = 50

# H.1: history.json doesn't carry secrets but lives in the same dir
# as config.json, so it inherits the restrictive-perms expectation.
HISTORY_FILE_MODE = 0o600


class TransferStatus:
    """Canonical status strings written to history rows.

    Single source of truth so senders, receivers, and the history
    renderer don't drift on case / spelling. Consumers can import the
    class and reference e.g. ``TransferStatus.SENDING`` instead of a
    string literal; new callers SHOULD do this. Legacy rows persisted
    by older builds use the same string values so no migration is
    needed.

    Phase C introduces three new values used only by streaming
    transfers: ``SENDING`` (sender uploading while recipient drains),
    ``WAITING_STREAM`` (mid-stream 507 backpressure), and ``ABORTED``
    (either side cancelled after init). The classic-mode statuses are
    unchanged.
    """

    # --- Classic + existing (unchanged wire shape) -------------------
    UPLOADING = "uploading"
    WAITING = "waiting"          # init-time 507 (quota full at init)
    COMPLETE = "complete"        # sender: upload done. See `delivered`
                                 # for the recipient-ack state.
    DOWNLOADING = "downloading"  # recipient pulling chunks
    FAILED = "failed"

    # --- Streaming additions (C.2 pass-through; C.3/C.4/C.5 writers) -
    SENDING = "sending"                # X→Y co-progress while streaming
    WAITING_STREAM = "waiting_stream"  # mid-stream 507 backpressure
    ABORTED = "aborted"                # either-party abort after init


# Terminal = no more state transitions expected, no server-side work
# needs polling. Used by ``get_undelivered_transfer_ids`` to stop
# painting delivery spinners on rows that are already done.
_TERMINAL_SENT_STATUSES = frozenset({
    TransferStatus.FAILED,
    TransferStatus.ABORTED,
})


class TransferHistory:
    """Persistent transfer history (JSON file, max 50 items).
    All mutations use file locking so multiple processes (tray, send-files,
    history window, --send CLI) can safely read/write the same file."""

    def __init__(self, config_dir: Path):
        self.history_file = config_dir / "history.json"
        self._lock = threading.Lock()
        self._warn_if_weak_perms()
        self._items: list[dict] = self._load()

    def _warn_if_weak_perms(self) -> None:
        if not self.history_file.exists():
            return
        try:
            mode = self.history_file.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            log.warning(
                "history.permissions.weak path=%s mode=%o expected=%o "
                "(fixed on next save)",
                self.history_file, mode, HISTORY_FILE_MODE,
            )

    def _load(self) -> list[dict]:
        """Read the history file under a shared flock.

        Without the lock a reader can hit the brief window between
        truncate() and write() inside _locked_read_modify_write — the
        file is empty on disk for a few hundred µs, json.loads raises,
        and the caller sees `[]`. That flashed the whole history to
        "No transfers yet" in the UI whenever the send-files subprocess
        was actively writing progress updates.

        LOCK_SH is compatible with other shared readers but blocks on
        any exclusive writer, so the read observes a consistent file.
        """
        if not self.history_file.exists():
            return []
        try:
            with open(self.history_file, "r") as fd:
                fcntl.flock(fd, fcntl.LOCK_SH)
                try:
                    content = fd.read()
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            if not content.strip():
                return []
            return json.loads(content)
        except Exception:
            log.warning("Failed to load history, keeping previous snapshot")
            return []

    def _locked_read_modify_write(self, modify_fn) -> bool:
        """Atomically read history from disk, apply modify_fn, write back.
        modify_fn(items) should return True if it made changes, False otherwise.
        Uses flock to serialize access across processes.

        flush + fsync happen INSIDE the lock so a reader that acquires
        LOCK_SH right after we release doesn't observe the truncated
        file (Python buffers writes; without the flush, the bytes are
        still in userspace when we release flock)."""
        with self._lock:
            try:
                fd = open(self.history_file, "a+")
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    fd.seek(0)
                    content = fd.read()
                    items = json.loads(content) if content.strip() else []
                    changed = modify_fn(items)
                    if changed:
                        fd.seek(0)
                        fd.truncate()
                        fd.write(json.dumps(items, indent=2))
                        fd.flush()
                        try:
                            os.fsync(fd.fileno())
                        except OSError:
                            # fsync unavailable on some filesystems —
                            # flush() alone still gets us into the page
                            # cache, which is enough for a same-host
                            # reader to see the data.
                            pass
                        # H.1: tighten perms on every write so pre-H.1
                        # installs (and any newly-created files) land at
                        # 0o600. fchmod-on-open-fd avoids a TOCTOU race
                        # against another process opening the path.
                        try:
                            os.fchmod(fd.fileno(), HISTORY_FILE_MODE)
                        except OSError:
                            pass
                    self._items = items
                    return changed
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    fd.close()
            except Exception:
                log.exception("History file locked read-modify-write failed")
                return False

    @property
    def items(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def add(self, filename: str, display_label: str, direction: str,
            size: int, content_path: str = "", sender_id: str = "",
            transfer_id: str = "", status: str = "complete",
            chunks_downloaded: int = 0, chunks_total: int = 0,
            *,
            peer_device_id: str = "",
            mode: str = "classic",
            chunks_uploaded: int = 0,
            abort_reason: str | None = None) -> None:
        """Append a history row.

        Streaming-specific kwargs (``mode``, ``chunks_uploaded``,
        ``abort_reason``) are keyword-only and optional so classic
        callers keep working unchanged; rows persisted without them
        still read cleanly (missing keys default to classic behaviour).

        ``chunks_uploaded`` is the sender's upload counter in streaming
        mode — paired with the existing ``recipient_chunks_downloaded``
        to paint "Sending X→Y". Distinct from ``chunks_downloaded``
        which historically doubles as the sender's upload counter
        AND the recipient's download counter depending on direction;
        kept untouched for back-compat.
        """
        new_item = {
            "filename": filename,
            "display_label": display_label,
            "direction": direction,
            "size": size,
            "content_path": content_path,
            "sender_id": sender_id,
            "peer_device_id": peer_device_id,
            "transfer_id": transfer_id,
            "status": status,
            "chunks_downloaded": chunks_downloaded,
            "chunks_total": chunks_total,
            "delivered": direction == "received" and status == "complete",
            "timestamp": int(time.time()),
            "mode": mode,
            "chunks_uploaded": chunks_uploaded,
        }
        if abort_reason is not None:
            new_item["abort_reason"] = abort_reason
        def do_add(items):
            # Upsert: if a row with this transfer_id already exists (e.g. a prior
            # failed attempt of the same transfer being retried), replace it in
            # place instead of creating a duplicate.
            if transfer_id:
                for i, item in enumerate(items):
                    if item.get("transfer_id") == transfer_id:
                        items[i] = new_item
                        return True
            items.insert(0, new_item)
            del items[MAX_HISTORY:]
            return True
        self._locked_read_modify_write(do_add)

    def get_peer_device_id(self, item: dict, *,
                           fallback_device_id: str = "") -> str:
        """Return the other device for a history row.

        New rows carry ``peer_device_id`` explicitly. Legacy received
        rows pre-M.1 only carried ``sender_id``, so keep that as a
        read-side fallback. Legacy sent rows did not persist a target;
        callers that need to bucket them can provide a best-effort
        fallback such as the active or first paired device.
        """
        peer_device_id = item.get("peer_device_id")
        if isinstance(peer_device_id, str) and peer_device_id:
            return peer_device_id

        sender_id = item.get("sender_id")
        if item.get("direction") == "received" and isinstance(sender_id, str) and sender_id:
            return sender_id

        return fallback_device_id

    def items_for_peer(self, peer_device_id: str, *,
                       fallback_device_id: str = "") -> list[dict]:
        """Return rows attributed to one connected device."""
        return [
            item for item in self.items
            if self.get_peer_device_id(
                item,
                fallback_device_id=fallback_device_id,
            ) == peer_device_id
        ]

    def update(self, transfer_id: str, **fields) -> bool:
        """Update an existing history entry by transfer_id. Returns True if found."""
        def do_update(items):
            for item in items:
                if item.get("transfer_id") == transfer_id:
                    item.update(fields)
                    return True
            return False
        return self._locked_read_modify_write(do_update)

    def mark_delivered(self, transfer_id: str) -> bool:
        """Mark a sent transfer as delivered. Returns True if found and updated."""
        def do_mark(items):
            for item in items:
                if item.get("transfer_id") == transfer_id and not item.get("delivered"):
                    item["delivered"] = True
                    return True
            return False
        return self._locked_read_modify_write(do_mark)

    def get_undelivered_transfer_ids(self) -> list[str]:
        """Get transfer_ids of sent items still in flight.

        Excludes transfers with a terminal status — ``failed`` (the
        upload itself didn't complete, so there's nothing to deliver)
        and ``aborted`` (either side cancelled after init; server has
        already wiped the blobs so there's nothing to track). Without
        this guard the delivery tracker would keep polling a server
        row that doesn't exist, and the tray would stay stuck on
        "uploading" forever.
        """
        # Reload from disk to pick up transfers added by other processes
        self._items = self._load()
        with self._lock:
            return [
                item["transfer_id"] for item in self._items
                if item.get("direction") == "sent"
                and item.get("transfer_id")
                and not item.get("delivered")
                and item.get("status") not in _TERMINAL_SENT_STATUSES
            ]

    def remove(self, item: dict) -> None:
        """Remove a specific item from history."""
        ts = item.get("timestamp")
        tid = item.get("transfer_id")
        def do_remove(items):
            before = len(items)
            items[:] = [i for i in items
                        if not (i.get("timestamp") == ts and i.get("transfer_id") == tid)]
            return len(items) != before
        self._locked_read_modify_write(do_remove)

    def clear(self) -> None:
        """Remove all history entries."""
        def do_clear(items):
            if not items:
                return False
            items.clear()
            return True
        self._locked_read_modify_write(do_clear)

    def clear_for_peer(self, peer_device_id: str, *,
                       fallback_device_id: str = "") -> bool:
        """Remove history entries attributed to one connected device.

        ``fallback_device_id`` keeps legacy sent rows, which did not
        persist a peer id, scoped to the currently selected device in
        filtered history views.
        """
        def do_clear(items):
            before = len(items)
            items[:] = [
                item for item in items
                if self.get_peer_device_id(
                    item,
                    fallback_device_id=fallback_device_id,
                ) != peer_device_id
            ]
            return len(items) != before
        return self._locked_read_modify_write(do_clear)

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
