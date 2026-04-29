"""Shared path setup for protocol contract tests."""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DESKTOP_ROOT = os.path.join(REPO_ROOT, "desktop")

# H.4: tests must never write to the developer's real OS keyring.
# Set at import time so any test that constructs Config() without
# an explicit ``secret_store=`` falls back to JsonFallbackStore.
# Tests that want to exercise SecretServiceStore inject a fake
# keyring module — see test_desktop_secrets.py.
os.environ.setdefault("DESKTOP_CONNECTOR_NO_KEYRING", "1")


def ensure_desktop_on_path() -> None:
    """Prepend ``desktop/`` to sys.path so tests can ``import src.*``.

    We import via the ``src.`` prefix so relative imports inside desktop
    modules (e.g. ``from ...interfaces.clipboard import ClipboardBackend``
    in ``src/platform/contract/desktop_platform.py``) resolve the same
    way they do in production (``python3 -m src.main``).
    """
    if DESKTOP_ROOT not in sys.path:
        sys.path.insert(0, DESKTOP_ROOT)
