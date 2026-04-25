"""Tests for the AppImage version checker (P.6a).

Covers the four acceptance buckets from the plan:
  1. ``$APPIMAGE`` gate — no HTTP without the env var.
  2. Parsing the GitHub Releases JSON, including filter-out of
     non-desktop / draft / prerelease tags.
  3. 24-hour cache: fresh cache skips the network; stale cache
     triggers a request and refreshes; ``If-Modified-Since`` header
     is honoured; 304 just bumps the freshness timestamp.
  4. Failure modes: network error / non-200 / invalid JSON return
     ``stale=True`` from cache, or None if no cache exists.

The GTK4 dialog and tray wiring belong to P.6b; this file is purely
unit tests for the polling module.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.updater import version_check as vc  # noqa: E402


def _release(
    tag,
    asset_name="desktop-connector-0.2.0-x86_64.AppImage",
    draft=False,
    prerelease=False,
):
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/hawwwran/desktop-connector/releases/tag/{tag}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": f"https://github.com/hawwwran/desktop-connector/releases/download/{tag}/{asset_name}",
            }
        ],
    }


def _mock_resp(payload, status=200, last_modified="Mon, 01 Apr 2026 12:00:00 GMT"):
    m = mock.Mock()
    m.status_code = status
    m.json.return_value = payload
    m.headers = {"Last-Modified": last_modified} if last_modified else {}
    return m


class _SandboxedCacheCase(unittest.TestCase):
    """Mixin: each test gets its own XDG_CACHE_HOME and stable
    ``get_app_version() == "0.1.1"``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "APPIMAGE": "/tmp/dc.AppImage",
                "XDG_CACHE_HOME": self._tmp.name,
            },
        )
        self._env.start()
        self._ver = mock.patch.object(vc, "get_app_version", return_value="0.1.1")
        self._ver.start()

    def tearDown(self):
        self._ver.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _seed_cache(
        self,
        fetched_at=None,
        last_modified="Mon, 01 Apr 2026 12:00:00 GMT",
        tag="desktop/v0.2.0",
    ):
        if fetched_at is None:
            fetched_at = int(time.time())
        cache = {
            "fetched_at": fetched_at,
            "last_modified": last_modified,
            "release": {
                "tag_name": tag,
                "html_url": f"https://example/{tag}",
                "asset_url": f"https://example/{tag}.AppImage",
            },
        }
        p = vc.cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache))


# --- Gate -------------------------------------------------------------------


class GateTests(unittest.TestCase):
    def test_no_appimage_returns_none_no_network(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(vc.requests, "get") as g:
                self.assertIsNone(vc.check_for_update())
            g.assert_not_called()


# --- Parsing ----------------------------------------------------------------


class ParsingTests(_SandboxedCacheCase):
    def test_returns_info_with_newer_release(self):
        with mock.patch.object(
            vc.requests, "get", return_value=_mock_resp([_release("desktop/v0.2.0")])
        ):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertEqual(info.current_version, "0.1.1")
        self.assertEqual(info.latest_version, "0.2.0")
        self.assertTrue(info.is_newer)
        self.assertFalse(info.stale)
        self.assertIn("v0.2.0", info.release_url)
        self.assertTrue(info.asset_url.endswith(".AppImage"))

    def test_returns_info_for_same_version(self):
        with mock.patch.object(
            vc.requests, "get", return_value=_mock_resp([_release("desktop/v0.1.1")])
        ):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertFalse(info.is_newer)

    def test_returns_info_for_older_release(self):
        with mock.patch.object(
            vc.requests, "get", return_value=_mock_resp([_release("desktop/v0.0.5")])
        ):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertFalse(info.is_newer)

    def test_filters_non_desktop_tags(self):
        # Android tag is more recent in the response order; desktop release
        # is older but should still be the one we surface.
        payload = [
            _release("android/v0.5.0", asset_name="app-debug.apk"),
            _release("desktop/v0.2.0"),
        ]
        with mock.patch.object(vc.requests, "get", return_value=_mock_resp(payload)):
            info = vc.check_for_update()
        self.assertEqual(info.latest_version, "0.2.0")

    def test_returns_none_when_no_desktop_release(self):
        payload = [_release("android/v0.5.0", asset_name="app.apk")]
        with mock.patch.object(vc.requests, "get", return_value=_mock_resp(payload)):
            self.assertIsNone(vc.check_for_update())

    def test_filters_drafts_and_prereleases(self):
        # Both newer-looking releases are filtered out; we land on v0.2.0.
        payload = [
            _release("desktop/v0.3.0", draft=True),
            _release("desktop/v0.2.5", prerelease=True),
            _release("desktop/v0.2.0"),
        ]
        with mock.patch.object(vc.requests, "get", return_value=_mock_resp(payload)):
            info = vc.check_for_update()
        self.assertEqual(info.latest_version, "0.2.0")

    def test_returns_none_when_release_lacks_appimage_asset(self):
        bad = _release("desktop/v0.2.0", asset_name="something.tar.gz")
        with mock.patch.object(vc.requests, "get", return_value=_mock_resp([bad])):
            self.assertIsNone(vc.check_for_update())

    def test_unrecognised_version_format_is_not_newer(self):
        # rc-suffix tag -> _parse_version returns () -> is_newer is False.
        with mock.patch.object(
            vc.requests, "get", return_value=_mock_resp([_release("desktop/v0.2.0-rc.1")])
        ):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertEqual(info.latest_version, "0.2.0-rc.1")
        self.assertFalse(info.is_newer)


# --- Cache ------------------------------------------------------------------


class CacheTests(_SandboxedCacheCase):
    def test_fresh_cache_skips_network(self):
        self._seed_cache()
        with mock.patch.object(vc.requests, "get") as g:
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertEqual(info.latest_version, "0.2.0")
        g.assert_not_called()

    def test_stale_cache_triggers_request(self):
        self._seed_cache(fetched_at=int(time.time()) - 25 * 3600)
        with mock.patch.object(
            vc.requests,
            "get",
            return_value=_mock_resp([_release("desktop/v0.3.0")]),
        ) as g:
            info = vc.check_for_update()
        g.assert_called_once()
        self.assertEqual(info.latest_version, "0.3.0")

    def test_force_overrides_freshness(self):
        self._seed_cache()  # fresh
        with mock.patch.object(
            vc.requests,
            "get",
            return_value=_mock_resp([_release("desktop/v0.4.0")]),
        ) as g:
            info = vc.check_for_update(force=True)
        g.assert_called_once()
        self.assertEqual(info.latest_version, "0.4.0")

    def test_if_modified_since_header_sent_when_cache_present(self):
        self._seed_cache(fetched_at=int(time.time()) - 25 * 3600)
        with mock.patch.object(
            vc.requests,
            "get",
            return_value=_mock_resp([_release("desktop/v0.3.0")]),
        ) as g:
            vc.check_for_update()
        sent_headers = g.call_args.kwargs["headers"]
        self.assertEqual(
            sent_headers["If-Modified-Since"],
            "Mon, 01 Apr 2026 12:00:00 GMT",
        )

    def test_304_refreshes_cache_timestamp(self):
        old_ts = int(time.time()) - 25 * 3600
        self._seed_cache(fetched_at=old_ts)
        m = mock.Mock(status_code=304, headers={})
        m.json.return_value = None
        with mock.patch.object(vc.requests, "get", return_value=m):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertFalse(info.stale)
        cache = json.loads(vc.cache_path().read_text())
        self.assertGreater(cache["fetched_at"], old_ts + 24 * 3600)

    def test_200_overwrites_cache(self):
        self._seed_cache(fetched_at=int(time.time()) - 25 * 3600, tag="desktop/v0.2.0")
        new_release = _release("desktop/v0.3.0")
        with mock.patch.object(
            vc.requests, "get", return_value=_mock_resp([new_release])
        ):
            vc.check_for_update()
        cache = json.loads(vc.cache_path().read_text())
        self.assertEqual(cache["release"]["tag_name"], "desktop/v0.3.0")


# --- Failure modes ----------------------------------------------------------


class FailureTests(_SandboxedCacheCase):
    def test_network_error_no_cache_returns_none(self):
        import requests as r

        with mock.patch.object(vc.requests, "get", side_effect=r.ConnectionError):
            self.assertIsNone(vc.check_for_update())

    def test_network_error_with_cache_returns_stale(self):
        import requests as r

        self._seed_cache(fetched_at=0)
        with mock.patch.object(vc.requests, "get", side_effect=r.ConnectionError):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertTrue(info.stale)
        self.assertEqual(info.latest_version, "0.2.0")

    def test_500_with_cache_returns_stale(self):
        self._seed_cache(fetched_at=0)
        m = mock.Mock(status_code=500, headers={})
        m.json.return_value = {}
        with mock.patch.object(vc.requests, "get", return_value=m):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertTrue(info.stale)

    def test_invalid_json_with_cache_returns_stale(self):
        self._seed_cache(fetched_at=0)
        m = mock.Mock(status_code=200, headers={"Last-Modified": "x"})
        m.json.side_effect = ValueError("bad json")
        with mock.patch.object(vc.requests, "get", return_value=m):
            info = vc.check_for_update()
        self.assertIsNotNone(info)
        self.assertTrue(info.stale)

    def test_500_no_cache_returns_none(self):
        m = mock.Mock(status_code=500, headers={})
        m.json.return_value = {}
        with mock.patch.object(vc.requests, "get", return_value=m):
            self.assertIsNone(vc.check_for_update())


if __name__ == "__main__":
    unittest.main()
