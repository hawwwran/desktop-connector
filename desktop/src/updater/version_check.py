"""Version check for the AppImage in-app updater (P.6a).

Polls the GitHub Releases API for the most recent ``desktop/v*`` tag
and compares it against the locally bundled ``version.json``. Caches
the response on disk for 24 hours and supplements with
``If-Modified-Since`` so re-checks within that window cost zero
network bytes.

Gated on ``$APPIMAGE`` — when the env var is unset the module
short-circuits before any HTTP request and returns ``None``. apt-pip
and dev-tree installs can't apply an update via AppImageUpdate
anyway, so surfacing one would be misleading; P.6b's tray menu
items observe the same gate.

Caller contract::

    info = version_check.check_for_update()
    if info and info.is_newer:
        # show "Update available -> {info.latest_version}" in the tray menu

``info.stale`` is True when the most recent network attempt failed
and we're returning a cached value — the caller may want to mute the
prompt or silently retry later.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ..bootstrap.app_version import get_app_version

log = logging.getLogger(__name__)

REPO = "hawwwran/desktop-connector"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"
TAG_PREFIX = "desktop/v"
CACHE_TTL_S = 24 * 60 * 60
HTTP_TIMEOUT_S = 10.0
USER_AGENT = "desktop-connector-updater"


@dataclass(frozen=True)
class UpdateInfo:
    """What the caller needs to decide whether to surface an update.

    Attributes:
        current_version: From bundled ``version.json`` ("0.1.1" etc.).
        latest_version: Stripped from the matching ``desktop/v*`` tag.
        release_url: HTML URL of the GitHub Release (for the menu's
            "View release notes" follow-up if we add one later).
        asset_url: ``browser_download_url`` of the ``.AppImage`` asset.
            What AppImageUpdate's zsync URL ultimately resolves to.
        is_newer: True iff ``latest_version > current_version`` under
            naive tuple-of-int semver compare. Caller decides whether
            to surface based on this.
        stale: True iff returning a cached value because the most
            recent network attempt failed. Caller may choose to mute
            the prompt or retry sooner.
    """

    current_version: str
    latest_version: str
    release_url: str
    asset_url: str
    is_newer: bool
    stale: bool = False


def cache_path() -> Path:
    """Disk location for the cached release dict.

    Honours ``XDG_CACHE_HOME`` (used by tests to sandbox); defaults to
    ``~/.cache/desktop-connector/update-check.json``.
    """
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "desktop-connector" / "update-check.json"


def check_for_update(*, force: bool = False) -> UpdateInfo | None:
    """Return ``UpdateInfo`` describing the latest desktop release, or None.

    Returns None when:
      - ``$APPIMAGE`` is unset (not running inside an AppImage).
      - GitHub has no ``desktop/v*`` release at all.
      - The latest release lacks a ``.AppImage`` asset.
      - Network is unreachable AND no usable cache exists.

    Returns ``UpdateInfo`` with ``stale=True`` when network failed but a
    prior cache hit is being replayed.

    ``force=True`` bypasses the 24-hour cache freshness check (still
    honours ``If-Modified-Since`` for bandwidth, just doesn't skip the
    HTTP call entirely). Hooked by P.6b's "Check for updates" menu
    item.
    """
    if not os.environ.get("APPIMAGE"):
        return None

    current_version = get_app_version()
    cache = _read_cache()
    now = int(time.time())

    if not force and cache and (now - int(cache.get("fetched_at", 0))) < CACHE_TTL_S:
        return _info_from_cache(cache, current_version, stale=False)

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if cache and cache.get("last_modified"):
        headers["If-Modified-Since"] = cache["last_modified"]

    try:
        resp = requests.get(
            RELEASES_URL,
            headers=headers,
            params={"per_page": 30},
            timeout=HTTP_TIMEOUT_S,
        )
    except requests.RequestException as e:
        log.warning("update_check.network_error error_kind=%s", type(e).__name__)
        return _info_from_cache(cache, current_version, stale=True)

    if resp.status_code == 304 and cache:
        # Body unchanged; just refresh the cache freshness so subsequent
        # checks within the next 24 h skip the network entirely.
        cache["fetched_at"] = now
        _write_cache(cache)
        return _info_from_cache(cache, current_version, stale=False)

    if resp.status_code != 200:
        log.warning("update_check.api_status code=%d", resp.status_code)
        return _info_from_cache(cache, current_version, stale=True)

    try:
        payload = resp.json()
    except ValueError:
        log.warning("update_check.invalid_json")
        return _info_from_cache(cache, current_version, stale=True)

    release = _pick_latest_desktop_release(payload)
    if release is None:
        # Don't drop a working cache for a transient "no desktop release on
        # the most recent page" answer — keep prior good cache for next try.
        return None

    asset_url = _extract_appimage_asset(release)
    if not asset_url:
        return None

    new_cache = {
        "fetched_at": now,
        "last_modified": resp.headers.get("Last-Modified", ""),
        "release": {
            "tag_name": release.get("tag_name", ""),
            "html_url": release.get("html_url", ""),
            "asset_url": asset_url,
        },
    }
    _write_cache(new_cache)
    return _build_info(new_cache["release"], current_version, stale=False)


# --- internals ---------------------------------------------------------------


def _info_from_cache(
    cache: dict | None, current_version: str, *, stale: bool
) -> UpdateInfo | None:
    if not cache:
        return None
    release = cache.get("release")
    if not release:
        return None
    return _build_info(release, current_version, stale=stale)


def _build_info(
    release: dict, current_version: str, *, stale: bool
) -> UpdateInfo | None:
    tag = release.get("tag_name", "")
    if not tag.startswith(TAG_PREFIX):
        return None
    latest_version = tag[len(TAG_PREFIX) :]
    asset_url = release.get("asset_url", "")
    if not asset_url:
        return None
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=release.get("html_url", ""),
        asset_url=asset_url,
        is_newer=_is_newer(latest_version, current_version),
        stale=stale,
    )


def _pick_latest_desktop_release(payload: object) -> dict | None:
    """Return the most recent non-draft, non-prerelease ``desktop/v*`` release.

    The /releases endpoint returns items sorted by created_at descending,
    so the first match wins. Drafts and prereleases are filtered out so a
    fat-finger draft tag doesn't show up in users' tray menus.
    """
    if not isinstance(payload, list):
        return None
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("draft", False) or item.get("prerelease", False):
            continue
        if item.get("tag_name", "").startswith(TAG_PREFIX):
            return item
    return None


def _extract_appimage_asset(release: dict) -> str:
    """Return the ``browser_download_url`` of the first .AppImage asset, or ''."""
    for asset in release.get("assets", []) or []:
        if not isinstance(asset, dict):
            continue
        if asset.get("name", "").endswith(".AppImage"):
            url = asset.get("browser_download_url", "")
            if isinstance(url, str):
                return url
    return ""


def _parse_version(s: str) -> tuple[int, ...]:
    """Naive dotted-int parse. Returns () on any non-int component so an
    unrecognised tag (``desktop/v0.2.0-rc.1``) is treated as NOT newer
    rather than crashing on int()."""
    parts: list[int] = []
    for piece in s.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            return ()
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    pl = _parse_version(latest)
    pc = _parse_version(current)
    if not pl or not pc:
        return False
    return pl > pc


def _read_cache() -> dict | None:
    p = cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(data: dict) -> None:
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)
