"""Logging setup for desktop bootstrap."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..config import DEFAULT_CONFIG_DIR


def setup_logging(verbose: bool = False, config_dir: Path | None = None) -> None:
    """Configure console logging and optional file rotation logging.

    Reads the ``allow_logging`` flag directly from ``config.json``
    rather than instantiating a :class:`~src.config.Config` —
    Config.__init__ has side effects (H.1 permission fixes, H.4
    secret-store migration) that emit log lines, and we want the
    file handler attached *before* anything else logs. Otherwise
    Config-side migration diagnostics land only on stderr and are
    invisible to users running the AppImage / tray.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)

    # File logging with 2-file rotation (max 1MB each, 2MB total).
    # Only enabled when user opts in via Settings -> Allow logging.
    resolved_dir = config_dir or DEFAULT_CONFIG_DIR
    if _read_allow_logging_flag(resolved_dir):
        log_dir = resolved_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "desktop-connector.log",
            maxBytes=1_000_000,  # 1 MB
            backupCount=1,  # keeps .log and .log.1 = 2 files max
        )
        file_handler.setFormatter(logging.Formatter(fmt, datefmt))
        file_handler.setLevel(level)
        logging.getLogger().addHandler(file_handler)


def _read_allow_logging_flag(config_dir: Path) -> bool:
    """Best-effort read of ``allow_logging`` from ``config.json``.

    Returns ``False`` if the file is missing, unreadable, or
    malformed — same default Config would apply via its
    ``allow_logging`` getter. Crucially, this avoids triggering any
    of Config.__init__'s side effects (perm fixes, secret-store
    selection, legacy-secret migration) at a point in startup
    when the file log handler isn't attached yet.
    """
    config_file = config_dir / "config.json"
    if not config_file.exists():
        return False
    try:
        with open(config_file) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("allow_logging", False))
