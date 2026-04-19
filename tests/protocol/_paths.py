"""Shared path setup for protocol contract tests."""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DESKTOP_SRC = os.path.join(REPO_ROOT, "desktop", "src")


def ensure_desktop_src_on_path() -> None:
    """Prepend desktop/src to sys.path so tests can import the desktop package."""
    if DESKTOP_SRC not in sys.path:
        sys.path.insert(0, DESKTOP_SRC)
