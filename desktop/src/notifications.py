"""
Desktop notifications via notify-send.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_pending_files: list[Path] = []
_batch_timer: threading.Timer | None = None
_lock = threading.Lock()
BATCH_DELAY = 5.0  # seconds to batch notifications


def notify(title: str, body: str, icon: str = "dialog-information") -> None:
    try:
        subprocess.run(
            ["notify-send", "-a", "Desktop Connector", "-i", icon, title, body],
            timeout=5,
            capture_output=True,
        )
    except FileNotFoundError:
        log.warning("notification.tool.missing")
    except subprocess.TimeoutExpired:
        log.warning("notification.send.failed reason=timeout")
    except Exception as e:
        log.warning("notification.send.failed error_kind=%s", type(e).__name__)


def notify_file_received(filepath: Path) -> None:
    """Batch-notify for received files (groups arrivals within 5s)."""
    global _batch_timer
    with _lock:
        _pending_files.append(filepath)
        if _batch_timer is not None:
            _batch_timer.cancel()
        _batch_timer = threading.Timer(BATCH_DELAY, _flush_notifications)
        _batch_timer.daemon = True
        _batch_timer.start()


def _flush_notifications() -> None:
    global _batch_timer
    with _lock:
        files = list(_pending_files)
        _pending_files.clear()
        _batch_timer = None

    if not files:
        return

    if len(files) == 1:
        notify("File received", files[0].name)
    else:
        notify("Files received", f"{len(files)} files saved")


def notify_connection_lost() -> None:
    notify("Connection lost", "Lost connection to relay server", "dialog-warning")


def notify_connection_restored() -> None:
    notify("Connection restored", "Reconnected to relay server")
