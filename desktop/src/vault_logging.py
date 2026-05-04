"""Vault-specific log handler (T17.2).

Adds a second :class:`~logging.handlers.RotatingFileHandler` attached
to the root logger whose ``logging.Filter`` only lets through records
whose message starts with the ``vault.<topic>.<verb>`` prose anchor.
The result is a separate ``<config_dir>/logs/vault.log`` carrying
only vault-flow events while the main desktop-connector.log keeps
the union of every subsystem.

Privacy contract (per §gaps §21 + diagnostics.events.md privacy
rule): the filter does NOT modify message content. Operators must
ensure no caller writes plaintext filenames, keys, passphrases,
recovery secrets, or decrypted file contents into a `vault.*` log
line — the diagnostics catalog enforces this at the design level
and the test suite spot-checks call sites.

Gated on the same ``allow_logging`` flag as the main file log; when
disabled the handler is never attached, so a fresh AppImage with
the toggle OFF leaves no vault.log on disk.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


VAULT_TAG_PREFIX = "vault."
VAULT_LOG_NAME = "vault.log"
VAULT_LOG_MAX_BYTES = 1_000_000  # 1 MB
VAULT_LOG_BACKUPS = 1            # 2 files max → 2 MB ceiling


class _VaultMessageFilter(logging.Filter):
    """Pass records whose message body starts with ``vault.``."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Use ``getMessage()`` so %-substitution + args are applied.
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return False
        return message.startswith(VAULT_TAG_PREFIX)


def attach_vault_log_handler(
    config_dir: Path,
    *,
    level: int = logging.INFO,
    handler_factory=None,
) -> Optional[logging.Handler]:
    """Attach the rotating vault-only handler to the root logger.

    Returns the handler (so the caller can detach it on shutdown), or
    ``None`` if creating the file failed. Idempotent — calling twice
    won't attach two handlers.

    ``handler_factory`` is for tests: a callable that takes ``(path,
    max_bytes, backup_count)`` and returns a ``logging.Handler``. The
    default is :class:`RotatingFileHandler` writing to disk.
    """
    log_dir = Path(config_dir) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    target_path = log_dir / VAULT_LOG_NAME
    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, "_vault_log_marker", False):
            return existing

    factory = handler_factory or _default_handler_factory
    handler = factory(target_path, VAULT_LOG_MAX_BYTES, VAULT_LOG_BACKUPS)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.addFilter(_VaultMessageFilter())
    handler._vault_log_marker = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    return handler


def detach_vault_log_handler() -> bool:
    """Remove a previously-attached vault.log handler. Returns True on hit."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_vault_log_marker", False):
            root.removeHandler(existing)
            try:
                existing.close()
            except Exception:  # noqa: BLE001
                pass
            return True
    return False


def _default_handler_factory(
    path: Path, max_bytes: int, backup_count: int,
) -> logging.Handler:
    return RotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count,
    )


__all__ = [
    "VAULT_LOG_BACKUPS",
    "VAULT_LOG_MAX_BYTES",
    "VAULT_LOG_NAME",
    "VAULT_TAG_PREFIX",
    "attach_vault_log_handler",
    "detach_vault_log_handler",
]
