"""Shared path setup for protocol contract tests."""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DESKTOP_ROOT = os.path.join(REPO_ROOT, "desktop")


def ensure_desktop_on_path() -> None:
    """Prepend ``desktop/`` to sys.path so tests can ``import src.*``.

    We import via the ``src.`` prefix so relative imports inside desktop
    modules (e.g. ``from ...interfaces.clipboard import ClipboardBackend``
    in ``src/platform/contract/desktop_platform.py``) resolve the same
    way they do in production (``python3 -m src.main``).
    """
    if DESKTOP_ROOT not in sys.path:
        sys.path.insert(0, DESKTOP_ROOT)
