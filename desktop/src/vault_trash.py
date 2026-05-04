"""Trash-on-delete helper for the vault sync loop (T11.4).

When a sync cycle would otherwise unlink a local file (because the
remote tombstoned it, or the user removed it elsewhere and the
two-way path needs to mirror the deletion), we move the file to the
OS trash instead. This preserves user intent at the file-manager
level: a wrong remote-side delete can be undone by restoring from
trash, and a real deletion looks identical to "the user moved it
to trash from Nautilus".

Linux trash semantics follow the FreeDesktop.org Trash spec, which
``gio trash`` implements correctly (proper ``info/`` + ``files/``
entries, multi-disk fallback, etc.). We shell out instead of
re-implementing because the spec corner cases (sticky-bit dirs,
trashinfo Restore=, name conflict counters) are non-trivial and
already handled by GLib.

If ``gio`` isn't available the helper logs a warning and falls back
to ``Path.unlink()`` so the sync loop still makes progress — the
caller can opt to skip the deletion entirely instead by checking
``can_use_trash()`` first.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path


log = logging.getLogger(__name__)


def can_use_trash() -> bool:
    """Whether ``gio trash`` is available on PATH (Linux desktop only)."""
    return shutil.which("gio") is not None


def trash_path(path: Path, *, log_event: str = "vault.sync.file_moved_to_trash") -> bool:
    """Move ``path`` to the OS trash. Returns True on success.

    Falls back to ``unlink`` if ``gio`` isn't available; emits a
    warning log line in that case so operators can spot the
    fallback. The fallback path is *not* recoverable — the caller
    should weigh that against just leaving the local file alone.
    """
    target = Path(path)
    if not target.exists():
        return True
    if not can_use_trash():
        log.warning(
            "vault.sync.trash_unavailable path=%s reason=gio-not-installed",
            target,
        )
        try:
            target.unlink()
            return True
        except OSError as exc:
            log.error(
                "vault.sync.trash_fallback_unlink_failed path=%s error=%s",
                target, exc,
            )
            return False

    try:
        result = subprocess.run(
            ["gio", "trash", "--", str(target)],
            check=False, capture_output=True, text=True,
        )
    except OSError as exc:
        log.error(
            "vault.sync.trash_invocation_failed path=%s error=%s",
            target, exc,
        )
        return False

    if result.returncode == 0:
        log.info("%s path=%s", log_event, target)
        return True

    log.warning(
        "vault.sync.trash_failed path=%s exit=%s stderr=%s",
        target, result.returncode, (result.stderr or "").strip(),
    )
    return False


__all__ = ["can_use_trash", "trash_path"]
