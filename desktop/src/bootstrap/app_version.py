"""Read the desktop component version from the bundled or source-tree version.json."""

from __future__ import annotations

import json
import os
from pathlib import Path


def get_app_version() -> str:
    """Return the desktop version string, or 'unknown' if not findable."""
    appdir = os.environ.get("APPDIR")
    if appdir:
        bundled = Path(appdir) / "usr/share/desktop-connector/version.json"
        version = _read_desktop_field(bundled)
        if version is not None:
            return version

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "version.json"
        version = _read_desktop_field(candidate)
        if version is not None:
            return version

    return "unknown"


def _read_desktop_field(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    value = data.get("desktop")
    return str(value) if value is not None else None
