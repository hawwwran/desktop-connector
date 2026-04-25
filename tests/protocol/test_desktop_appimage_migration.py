"""Tests for the apt-pip → AppImage migration (P.4b).

Covers: trigger detection (only fires when old install dir + AppImage env
match), key-fingerprint verification (passes / fails / ambiguous), and
the cleanup mechanics (install dir + launcher wrapper removed,
config dir untouched).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.bootstrap import appimage_migration as migration  # noqa: E402
from src.config import Config  # noqa: E402
from src.crypto import KeyManager  # noqa: E402


class _RecorderNotifications:
    """Stand-in NotificationBackend that records notify() calls."""

    def __init__(self):
        self.events = []

    def notify(self, title, body, icon="dialog-information"):
        self.events.append((title, body))

    def notify_file_received(self, filepath):
        pass

    def notify_connection_lost(self):
        pass

    def notify_connection_restored(self):
        pass


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_root = Path(self._tmp.name)

        # Sandbox config dir + key dir.
        self._config_dir = self._tmp_root / "config/desktop-connector"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._config = Config(self._config_dir)
        self._crypto = KeyManager(self._config_dir)
        # Default to "registered" with the matching device_id so verification passes.
        self._config.device_id = self._crypto.get_device_id()
        self._config.auth_token = "fake-token"

        # Sandbox the old install paths used by the migration module.
        self._old_install = self._tmp_root / "share/desktop-connector"
        self._old_marker = self._old_install / "src/main.py"
        self._old_launcher = self._tmp_root / "bin/desktop-connector"

        self._patches = [
            mock.patch.object(migration, "OLD_INSTALL_DIR", self._old_install),
            mock.patch.object(migration, "OLD_INSTALL_MARKER", self._old_marker),
            mock.patch.object(migration, "OLD_LAUNCHER", self._old_launcher),
        ]
        for p in self._patches:
            p.start()

        self._notifications = _RecorderNotifications()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _seed_old_install(self):
        """Lay down the apt-pip install layout from install.sh."""
        (self._old_install / "src").mkdir(parents=True, exist_ok=True)
        self._old_marker.write_text("# old src/main.py\n")
        (self._old_install / "install.sh").write_text("#!/bin/bash\n")
        (self._old_install / "uninstall.sh").write_text("#!/bin/bash\n")
        self._old_launcher.parent.mkdir(parents=True, exist_ok=True)
        self._old_launcher.write_text(
            "#!/bin/bash\nexec python3 -m src.main \"$@\"\n"
        )
        self._old_launcher.chmod(0o755)

    def _run(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/tmp/dc.AppImage"}):
            migration.migrate_from_apt_pip_if_needed(
                self._config, self._crypto, self._notifications
            )

    # --- Trigger detection -------------------------------------------------

    def test_no_op_outside_appimage(self):
        self._seed_old_install()
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            migration.migrate_from_apt_pip_if_needed(
                self._config, self._crypto, self._notifications
            )
        self.assertTrue(self._old_marker.exists())
        self.assertTrue(self._old_launcher.exists())
        self.assertEqual(self._notifications.events, [])

    def test_no_op_when_marker_missing(self):
        # No old install at all (fresh AppImage user).
        self._run()
        self.assertEqual(self._notifications.events, [])

    def test_stale_config_no_src_skips_migration(self):
        """Acceptance: launch with stale config (orphaned keys but no
        src/) — uses existing config, no migration prompt."""
        # Old install dir exists but src/main.py was already deleted
        # in a prior partial run.
        self._old_install.mkdir(parents=True, exist_ok=True)
        (self._old_install / "uninstall.sh").write_text("#!/bin/bash\n")
        self._run()
        self.assertEqual(self._notifications.events, [])
        # The orphaned uninstall.sh stays — we don't touch it because
        # the marker isn't there.
        self.assertTrue((self._old_install / "uninstall.sh").exists())

    # --- Happy path --------------------------------------------------------

    def test_successful_migration_removes_apt_pip_files_and_launcher(self):
        """Acceptance: migration completes silently; old install files
        gone; one info notification."""
        self._seed_old_install()
        self._run()

        # apt-pip artifacts gone (src/, install.sh)…
        self.assertFalse(self._old_marker.exists())
        self.assertFalse((self._old_install / "install.sh").exists())
        # …launcher wrapper gone…
        self.assertFalse(self._old_launcher.exists())
        # …uninstall.sh PRESERVED (install.sh re-drops the AppImage-shape
        # one with the same filename; we conservatively keep it because
        # we can't tell apart "left over from apt-pip" from "just dropped
        # by the new install.sh" without content-sniffing).
        self.assertTrue((self._old_install / "uninstall.sh").exists())
        # …user-data dir untouched.
        self.assertTrue((self._config_dir / "config.json").exists())
        self.assertTrue((self._config_dir / "keys" / "private_key.pem").exists())
        # Exactly one info notification with the expected wording.
        self.assertEqual(len(self._notifications.events), 1)
        title, body = self._notifications.events[0]
        self.assertIn("Migrated", title)
        self.assertIn("preserved", body)

    def test_migration_preserves_appimage_and_local_uninstaller(self):
        """install.sh places the AppImage AND an uninstall.sh into
        OLD_INSTALL_DIR. Migration must NOT delete those — otherwise it
        wipes the running AppImage from under itself on first launch.
        """
        self._seed_old_install()
        # Lay down what install.sh would have placed there alongside the
        # apt-pip leftovers.
        appimage = self._old_install / "desktop-connector.AppImage"
        appimage.write_text("#!/bin/bash\nfake AppImage\n")
        appimage.chmod(0o755)
        new_uninstaller = self._old_install / "uninstall.sh"
        new_uninstaller.write_text("#!/bin/bash\nfake new uninstaller\n")
        new_uninstaller.chmod(0o755)

        self._run()

        # Preserved
        self.assertTrue(appimage.exists())
        self.assertEqual(appimage.read_text(), "#!/bin/bash\nfake AppImage\n")
        self.assertTrue(new_uninstaller.exists())
        # apt-pip artifacts gone
        self.assertFalse(self._old_marker.exists())
        self.assertFalse((self._old_install / "install.sh").exists())

    def test_successful_migration_with_unregistered_config(self):
        """Pre-register apt-pip install (no device_id yet) — verification
        skips the fingerprint check and migration proceeds."""
        # Wipe the registered creds we set in setUp.
        self._config._data.pop("device_id", None)
        self._config._data.pop("auth_token", None)
        self._config.save()
        self._seed_old_install()
        self._run()
        self.assertFalse(self._old_marker.exists())
        self.assertEqual(len(self._notifications.events), 1)

    # --- Verification failure ---------------------------------------------

    def test_fingerprint_mismatch_does_not_delete(self):
        """Acceptance: migration verification failure (key fingerprint
        mismatch) — both installs remain, warning notification surfaced."""
        self._seed_old_install()
        # Force a mismatch by writing a foreign device_id.
        self._config.device_id = "0" * 32
        self._run()
        self.assertTrue(self._old_marker.exists())
        self.assertTrue(self._old_launcher.exists())
        self.assertEqual(len(self._notifications.events), 1)
        title, _ = self._notifications.events[0]
        self.assertIn("Could not migrate", title)

    def test_notify_failure_is_swallowed(self):
        """Migration must not crash if the notification backend raises."""
        self._seed_old_install()

        class Boom:
            def notify(self, *a, **kw):
                raise RuntimeError("notify broke")

            notify_file_received = lambda *a, **kw: None
            notify_connection_lost = lambda *a, **kw: None
            notify_connection_restored = lambda *a, **kw: None

        with mock.patch.dict(os.environ, {"APPIMAGE": "/x.AppImage"}):
            migration.migrate_from_apt_pip_if_needed(
                self._config, self._crypto, Boom()
            )
        # Cleanup still happened despite the broken notifier.
        self.assertFalse(self._old_marker.exists())


class _DeriveTests(unittest.TestCase):
    """The verification helper is small — guard the SHA-256 truncation
    against accidental re-shape."""

    def test_derive_matches_keymanager_get_device_id(self):
        with tempfile.TemporaryDirectory() as d:
            crypto = KeyManager(Path(d))
            self.assertEqual(
                migration._derive_device_id(crypto), crypto.get_device_id()
            )


if __name__ == "__main__":
    unittest.main()
