"""Logging setup for desktop bootstrap."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..config import Config


def setup_logging(verbose: bool = False, config_dir: Path | None = None) -> None:
    """Configure console logging and optional file rotation logging."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)

    # File logging with 2-file rotation (max 1MB each, 2MB total)
    # Only enabled when user opts in via Settings -> Allow logging
    if config_dir:
        config = Config(config_dir)
        if config.allow_logging:
            log_dir = config_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "desktop-connector.log",
                maxBytes=1_000_000,  # 1 MB
                backupCount=1,  # keeps .log and .log.1 = 2 files max
            )
            file_handler.setFormatter(logging.Formatter(fmt, datefmt))
            file_handler.setLevel(level)
            logging.getLogger().addHandler(file_handler)
