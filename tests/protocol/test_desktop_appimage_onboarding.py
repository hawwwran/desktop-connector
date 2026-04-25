"""Tests for the AppImage first-launch onboarding hook (P.4a).

Covers the trigger logic (`needs_onboarding`), the /api/health probe
shape, and the SAVED-vs-CANCELLED detection after the subprocess
returns. The GTK4 dialog itself runs in a subprocess (windows.py) and
is exercised in the real-AppImage smoke check.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.bootstrap import appimage_onboarding as onboarding  # noqa: E402
from src.config import Config  # noqa: E402


class NeedsOnboardingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._config_dir = Path(self._tmp.name)
        self._config = Config(self._config_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_op_outside_appimage(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(onboarding.needs_onboarding(self._config))

    def test_no_op_when_headless(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            self.assertFalse(
                onboarding.needs_onboarding(self._config, headless=True)
            )

    def test_triggers_when_appimage_and_url_unset(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            self.assertTrue(onboarding.needs_onboarding(self._config))

    def test_skips_when_url_already_set(self):
        self._config.server_url = "https://relay.example/dc"
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            self.assertFalse(onboarding.needs_onboarding(self._config))

    def test_appimage_install_hook_flag_does_not_block(self):
        """The install hook saves config.json with appimage_install_hook_done
        before onboarding runs; onboarding must still trigger because the
        server_url key is still absent."""
        self._config.appimage_install_hook_done = True
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            self.assertTrue(onboarding.needs_onboarding(self._config))


class ProbeServerTests(unittest.TestCase):
    def test_recognises_status_ok(self):
        with mock.patch.object(onboarding.requests, "get") as g:
            g.return_value = mock.Mock(status_code=200, json=lambda: {"status": "ok"})
            self.assertTrue(onboarding.probe_server("https://relay.example"))
            g.assert_called_once_with(
                "https://relay.example/api/health",
                timeout=onboarding.HEALTH_PROBE_TIMEOUT_S,
            )

    def test_recognises_legacy_ok(self):
        with mock.patch.object(onboarding.requests, "get") as g:
            g.return_value = mock.Mock(status_code=200, json=lambda: {"ok": True})
            self.assertTrue(onboarding.probe_server("https://relay.example"))

    def test_strips_trailing_slash(self):
        with mock.patch.object(onboarding.requests, "get") as g:
            g.return_value = mock.Mock(status_code=200, json=lambda: {"status": "ok"})
            onboarding.probe_server("https://relay.example/")
            g.assert_called_once_with(
                "https://relay.example/api/health",
                timeout=onboarding.HEALTH_PROBE_TIMEOUT_S,
            )

    def test_non_200_returns_false(self):
        with mock.patch.object(onboarding.requests, "get") as g:
            g.return_value = mock.Mock(status_code=500, json=lambda: {"status": "ok"})
            self.assertFalse(onboarding.probe_server("https://relay.example"))

    def test_network_error_returns_false(self):
        import requests as r

        with mock.patch.object(
            onboarding.requests,
            "get",
            side_effect=r.ConnectionError("boom"),
        ):
            self.assertFalse(onboarding.probe_server("https://relay.example"))

    def test_invalid_json_returns_false(self):
        resp = mock.Mock(status_code=200)
        resp.json.side_effect = ValueError
        with mock.patch.object(onboarding.requests, "get", return_value=resp):
            self.assertFalse(onboarding.probe_server("https://relay.example"))


class CommitOnboardingSettingsTests(unittest.TestCase):
    """Persistence logic extracted from the GTK4 dialog's button
    closure. Direct unit tests so we don't need to drive a GTK4
    event loop to verify the user's choices land where they should."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._config_dir = Path(self._tmp.name)
        self._marker = self._config_dir / onboarding.NO_AUTOSTART_MARKER

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_server_url_to_config(self):
        onboarding.commit_onboarding_settings(
            self._config_dir,
            server_url="https://relay.example.com",
            autostart_enabled=True,
        )
        # Verify by re-loading the config from disk
        from src.config import Config
        cfg = Config(self._config_dir)
        self.assertEqual(cfg.server_url, "https://relay.example.com")

    def test_autostart_enabled_does_not_create_marker(self):
        onboarding.commit_onboarding_settings(
            self._config_dir,
            server_url="https://relay.example.com",
            autostart_enabled=True,
        )
        self.assertFalse(self._marker.exists())

    def test_autostart_disabled_creates_marker(self):
        onboarding.commit_onboarding_settings(
            self._config_dir,
            server_url="https://relay.example.com",
            autostart_enabled=False,
        )
        self.assertTrue(self._marker.exists())

    def test_autostart_re_enabled_removes_existing_marker(self):
        # Pre-create the marker as if the user had previously disabled
        # autostart, then run commit with autostart_enabled=True.
        self._marker.touch()
        self.assertTrue(self._marker.exists())
        onboarding.commit_onboarding_settings(
            self._config_dir,
            server_url="https://relay.example.com",
            autostart_enabled=True,
        )
        self.assertFalse(self._marker.exists())

    def test_preserves_other_config_keys(self):
        # Seed an existing config with unrelated keys; commit must not
        # clobber them.
        from src.config import Config
        cfg = Config(self._config_dir)
        cfg.device_id = "fake-device"
        cfg.auth_token = "fake-token"

        onboarding.commit_onboarding_settings(
            self._config_dir,
            server_url="https://new.example.com",
            autostart_enabled=False,
        )

        cfg2 = Config(self._config_dir)
        self.assertEqual(cfg2.device_id, "fake-device")
        self.assertEqual(cfg2.auth_token, "fake-token")
        self.assertEqual(cfg2.server_url, "https://new.example.com")


class RunOnboardingIfNeededTests(unittest.TestCase):
    """Verify SAVED vs CANCELLED detection by inspecting config.json
    after the (mocked) subprocess returns. Save-path stub writes
    server_url; cancel-path stub leaves config alone.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._config_dir = Path(self._tmp.name)
        self._config = Config(self._config_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_subprocess_outside_appimage(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(onboarding, "_spawn_onboarding_subprocess") as s:
                result = onboarding.run_onboarding_if_needed(self._config)
        self.assertEqual(result, onboarding.OnboardingResult.NOT_NEEDED)
        s.assert_not_called()

    def test_save_path_returns_saved(self):
        def fake_save(config_dir):
            # Stand-in for the subprocess: write a URL like the real dialog would.
            cfg = Config(config_dir)
            cfg.server_url = "https://relay.example/dc"

        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            with mock.patch.object(
                onboarding, "_spawn_onboarding_subprocess", side_effect=fake_save
            ) as s:
                result = onboarding.run_onboarding_if_needed(self._config)
        self.assertEqual(result, onboarding.OnboardingResult.SAVED)
        s.assert_called_once_with(self._config_dir)
        self.assertEqual(self._config.server_url, "https://relay.example/dc")

    def test_cancel_path_returns_cancelled(self):
        # Subprocess exits without touching config — Cancel.
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            with mock.patch.object(
                onboarding, "_spawn_onboarding_subprocess"
            ) as s:
                result = onboarding.run_onboarding_if_needed(self._config)
        self.assertEqual(result, onboarding.OnboardingResult.CANCELLED)
        s.assert_called_once()

    def test_cancel_then_relaunch_re_triggers(self):
        """Acceptance: re-launching after cancel re-opens the dialog."""
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            with mock.patch.object(onboarding, "_spawn_onboarding_subprocess"):
                onboarding.run_onboarding_if_needed(self._config)
            # Cancel didn't write server_url → still triggers.
            self.assertTrue(onboarding.needs_onboarding(self._config))


class SpawnSubprocessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._config_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_uses_appimage_when_set(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/path/to/dc.AppImage"}):
            with mock.patch.object(
                onboarding.subprocess, "run"
            ) as r:
                onboarding._spawn_onboarding_subprocess(self._config_dir)
        cmd = r.call_args.args[0]
        self.assertEqual(cmd[0], "/path/to/dc.AppImage")
        self.assertEqual(cmd[1], "--gtk-window=onboarding")
        self.assertEqual(cmd[2], f"--config-dir={self._config_dir}")

    def test_falls_back_to_dev_tree_when_unset(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(
                onboarding.subprocess, "run"
            ) as r:
                onboarding._spawn_onboarding_subprocess(self._config_dir)
        cmd = r.call_args.args[0]
        self.assertEqual(cmd[1:], ["-m", "src.windows", "onboarding",
                                    f"--config-dir={self._config_dir}"])

    def test_spawn_failure_does_not_propagate(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            with mock.patch.object(
                onboarding.subprocess, "run", side_effect=OSError("boom")
            ):
                # Must not raise — caller treats this as cancellation.
                onboarding._spawn_onboarding_subprocess(self._config_dir)

    def test_timeout_treated_as_cancellation(self):
        """A hung onboarding dialog (GTK4 init stall, missing fonts) must
        not block the tray boot indefinitely. subprocess.run timeouts
        out at 600 s; the parent sees TimeoutExpired and returns
        normally — caller treats it as Cancel because no server_url
        was written."""
        import subprocess as _sp

        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            with mock.patch.object(
                onboarding.subprocess, "run",
                side_effect=_sp.TimeoutExpired(cmd="x", timeout=600),
            ):
                # Must not raise — caller treats as cancellation.
                onboarding._spawn_onboarding_subprocess(self._config_dir)

    def test_run_called_with_timeout(self):
        """Sanity: the spawn call passes a non-None timeout so a hung
        child can't block forever."""
        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            with mock.patch.object(onboarding.subprocess, "run") as r:
                onboarding._spawn_onboarding_subprocess(self._config_dir)
            self.assertIsNotNone(r.call_args.kwargs.get("timeout"))
            self.assertGreater(r.call_args.kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
