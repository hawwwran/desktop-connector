"""File-manager integration sync tests for desktop M.6."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.devices import ConnectedDeviceRegistry  # noqa: E402
from src.file_manager_integration import (  # noqa: E402
    APP_NAME,
    CONFIG_ID_PREFIX,
    DOLPHIN_SERVICE_FILENAME,
    LEGACY_NAUTILUS_NAME,
    MANAGED_SENTINEL,
    PAIRING_ID_PREFIX,
    sync_file_manager_targets,
)


def _key_b64() -> str:
    return base64.b64encode(b"k" * 32).decode()


class FileManagerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config/desktop-connector"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config = Config(self.config_dir)

        self.appimage = self.home / "Apps/desktop-connector.AppImage"
        self.appimage.parent.mkdir(parents=True, exist_ok=True)
        self.appimage.write_text("#!/bin/bash\nexit 0\n")
        self.appimage.chmod(0o755)

        self.nautilus_dir = self.home / ".local/share/nautilus/scripts"
        self.nemo_dir = self.home / ".local/share/nemo/scripts"
        self.dolphin_path = (
            self.home
            / ".local/share/kservices5/ServiceMenus"
            / DOLPHIN_SERVICE_FILENAME
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, device_id: str, *, name: str, paired_at: int) -> None:
        self.config.add_paired_device(
            device_id, f"pk-{device_id}", _key_b64(), name=name,
        )
        self.config._data["paired_devices"][device_id]["paired_at"] = paired_at
        self.config.save()

    def _sync(self, *, file_managers=("nautilus", "nemo", "dolphin")) -> None:
        sync_file_manager_targets(
            self.config,
            appimage_path=self.appimage,
            home=self.home,
            file_managers=set(file_managers),
        )

    def test_no_op_when_no_launcher_resolved(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        # Neither AppImage nor source-bin is provided, and ~/.local/bin
        # under the sandbox HOME is empty.
        sync_file_manager_targets(
            self.config,
            home=self.home,
            file_managers={"nautilus"},
        )
        self.assertFalse(self.nautilus_dir.exists())

    def test_per_device_scripts_for_each_paired_device(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=20)
        self._add("dev-B", name="Beta", paired_at=10)

        self._sync()

        alpha = self.nautilus_dir / "Send to Alpha"
        beta = self.nautilus_dir / "Send to Beta"
        self.assertTrue(alpha.exists())
        self.assertTrue(beta.exists())
        self.assertTrue(alpha.stat().st_mode & 0o111)

        alpha_text = alpha.read_text()
        self.assertIn(MANAGED_SENTINEL, alpha_text)
        self.assertIn(f"{PAIRING_ID_PREFIX}dev-A", alpha_text)
        self.assertIn(f"{CONFIG_ID_PREFIX}{APP_NAME}", alpha_text)
        self.assertIn(str(self.appimage), alpha_text)
        self.assertIn('TARGET_DEVICE_ID = "dev-A"', alpha_text)
        self.assertIn('--target-device-id=', alpha_text)

        # Nemo gets the same content.
        nemo_alpha = self.nemo_dir / "Send to Alpha"
        self.assertTrue(nemo_alpha.exists())
        self.assertEqual(nemo_alpha.read_text(), alpha_text)

    def test_dolphin_single_file_with_one_action_per_device(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=20)
        self._add("dev-B", name="Beta", paired_at=10)

        self._sync()

        text = self.dolphin_path.read_text()
        self.assertIn(MANAGED_SENTINEL, text)
        self.assertIn("[Desktop Action sendToDevice_dev-A", text)
        self.assertIn("[Desktop Action sendToDevice_dev-B", text)
        self.assertIn("Name=Send to Alpha", text)
        self.assertIn("Name=Send to Beta", text)
        self.assertIn("--target-device-id=dev-A", text)
        self.assertIn("--target-device-id=dev-B", text)
        self.assertRegex(text, r"Actions=sendToDevice_[^;]+;sendToDevice_")

    def test_unpair_removes_only_that_devices_script(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=20)
        self._add("dev-B", name="Beta", paired_at=10)
        self._sync()

        registry = ConnectedDeviceRegistry(self.config)
        registry.unpair("dev-B")
        self._sync()

        self.assertTrue((self.nautilus_dir / "Send to Alpha").exists())
        self.assertFalse((self.nautilus_dir / "Send to Beta").exists())
        self.assertFalse((self.nemo_dir / "Send to Beta").exists())

        # Dolphin file no longer has Beta but still has Alpha.
        text = self.dolphin_path.read_text()
        self.assertIn("Name=Send to Alpha", text)
        self.assertNotIn("Name=Send to Beta", text)
        self.assertNotIn("dev-B", text)

    def test_unpair_last_device_deletes_dolphin_file(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        self._sync()
        self.assertTrue(self.dolphin_path.exists())

        ConnectedDeviceRegistry(self.config).unpair("dev-A")
        self._sync()
        self.assertFalse(self.dolphin_path.exists())

    def test_rename_renames_filename_and_keeps_no_stale(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        self._sync()
        self.assertTrue((self.nautilus_dir / "Send to Alpha").exists())

        ConnectedDeviceRegistry(self.config).rename("dev-A", "Workstation")
        self._sync()

        self.assertFalse((self.nautilus_dir / "Send to Alpha").exists())
        renamed = self.nautilus_dir / "Send to Workstation"
        self.assertTrue(renamed.exists())
        self.assertIn(f"{PAIRING_ID_PREFIX}dev-A", renamed.read_text())

    def test_unmarked_user_file_is_never_deleted(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        # Pre-existing user-authored "Send to Alpha" with no managed sentinel.
        self.nautilus_dir.mkdir(parents=True, exist_ok=True)
        user_script = self.nautilus_dir / "User custom script"
        user_script.write_text("#!/bin/bash\necho user wrote this\n")
        user_script.chmod(0o755)

        self._sync()

        # Sync should leave foreign files alone.
        self.assertTrue(user_script.exists())
        self.assertEqual(
            user_script.read_text(),
            "#!/bin/bash\necho user wrote this\n",
        )

    def test_unmarked_matching_script_name_is_never_overwritten(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        self.nautilus_dir.mkdir(parents=True, exist_ok=True)
        user_script = self.nautilus_dir / "Send to Alpha"
        user_script.write_text("#!/bin/bash\necho custom alpha\n")
        user_script.chmod(0o755)

        self._sync(file_managers=("nautilus",))

        self.assertEqual(
            user_script.read_text(),
            "#!/bin/bash\necho custom alpha\n",
        )
        self.assertNotIn(MANAGED_SENTINEL, user_script.read_text())

    def test_unmarked_send_to_phone_user_file_is_never_deleted(self) -> None:
        # If a user happens to have an unrelated script literally named
        # "Send to Phone" (no sentinel, no legacy fingerprint), sync
        # must NOT delete it on adoption.
        self._add("dev-A", name="Alpha", paired_at=10)
        self.nautilus_dir.mkdir(parents=True, exist_ok=True)
        user_legacy_name = self.nautilus_dir / LEGACY_NAUTILUS_NAME
        user_legacy_name.write_text(
            "#!/bin/bash\n"
            "# unrelated user script with our filename but not our content\n"
            "exec my-tool \"$@\"\n",
        )
        user_legacy_name.chmod(0o755)

        self._sync()

        self.assertTrue(user_legacy_name.exists())
        self.assertIn("my-tool", user_legacy_name.read_text())

    def test_legacy_send_to_phone_script_is_adopted_and_removed(self) -> None:
        # AppImage hook (pre-M.6) wrote a script with the fingerprint
        # "Send selected files to phone via Desktop Connector".
        self._add("dev-A", name="Alpha", paired_at=10)
        self.nautilus_dir.mkdir(parents=True, exist_ok=True)
        legacy = self.nautilus_dir / LEGACY_NAUTILUS_NAME
        legacy.write_text(
            "#!/usr/bin/env python3\n"
            '"""Send selected files to phone via Desktop Connector (AppImage)."""\n'
            "# old code here\n",
        )
        legacy.chmod(0o755)

        self._sync()

        self.assertFalse(legacy.exists())
        self.assertTrue((self.nautilus_dir / "Send to Alpha").exists())

    def test_legacy_dolphin_service_is_replaced_with_managed(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        self.dolphin_path.parent.mkdir(parents=True, exist_ok=True)
        self.dolphin_path.write_text(
            "[Desktop Entry]\nType=Service\n"
            "ServiceTypes=KonqPopupMenu/Plugin\n"
            "MimeType=application/octet-stream;\n"
            "Actions=sendToPhone\n\n"
            "[Desktop Action sendToPhone]\n"
            "Name=Send to Phone\n"
            "Icon=desktop-connector\n"
            "Exec=/old/bin --headless --send=%f\n"
        )

        self._sync()

        text = self.dolphin_path.read_text()
        self.assertIn(MANAGED_SENTINEL, text)
        self.assertIn("Name=Send to Alpha", text)
        self.assertNotIn("Name=Send to Phone", text)
        self.assertNotIn("/old/bin", text)

    def test_unmarked_dolphin_service_is_never_overwritten(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        self.dolphin_path.parent.mkdir(parents=True, exist_ok=True)
        original = (
            "[Desktop Entry]\n"
            "Type=Service\n"
            "Name=Custom service menu\n"
            "Actions=custom\n\n"
            "[Desktop Action custom]\n"
            "Name=Custom action\n"
            "Exec=/usr/bin/custom %f\n"
        )
        self.dolphin_path.write_text(original)

        self._sync(file_managers=("dolphin",))

        self.assertEqual(self.dolphin_path.read_text(), original)
        self.assertNotIn(MANAGED_SENTINEL, self.dolphin_path.read_text())

    def test_idempotent_no_rewrite_when_unchanged(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        self._sync()
        target = self.nautilus_dir / "Send to Alpha"
        first_mtime = target.stat().st_mtime_ns

        self._sync()

        self.assertEqual(target.stat().st_mtime_ns, first_mtime)

    def test_skipped_file_managers_not_touched(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)
        self._sync(file_managers=("nautilus",))

        self.assertTrue((self.nautilus_dir / "Send to Alpha").exists())
        self.assertFalse(self.nemo_dir.exists())
        self.assertFalse(self.dolphin_path.exists())

    def test_source_bin_path_used_when_no_appimage(self) -> None:
        self._add("dev-A", name="Alpha", paired_at=10)

        bin_dir = self.home / ".local/bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        source_bin = bin_dir / "desktop-connector"
        source_bin.write_text("#!/bin/bash\nexit 0\n")
        source_bin.chmod(0o755)

        sync_file_manager_targets(
            self.config,
            home=self.home,
            file_managers={"nautilus"},
        )

        target = self.nautilus_dir / "Send to Alpha"
        self.assertTrue(target.exists())
        self.assertIn(str(source_bin), target.read_text())

    def test_filename_unsafe_chars_are_replaced(self) -> None:
        self._add("dev-A", name="Slash/Backslash\\Pipe", paired_at=10)
        self._sync(file_managers=("nautilus",))
        # / and \\ both become "-" so the filename is FS-safe.
        self.assertTrue(
            (self.nautilus_dir / "Send to Slash-Backslash-Pipe").exists()
        )


class FileManagerCrossConfigIsolationTests(unittest.TestCase):
    """Sibling configs sharing one host's XDG dirs must not clobber each
    other's managed scripts. Reproduces the 2026-05-06 vault-test bug
    where a dev twin (``--config-dir=~/.config/desktop-connector-dev``)
    deleted the canonical install's ``Send to <peer>`` Nautilus script.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

        self.canonical_dir = self.home / ".config/desktop-connector"
        self.canonical_dir.mkdir(parents=True, exist_ok=True)
        self.canonical_config = Config(self.canonical_dir)

        self.dev_dir = self.home / ".config/desktop-connector-dev"
        self.dev_dir.mkdir(parents=True, exist_ok=True)
        self.dev_config = Config(self.dev_dir)

        self.appimage = self.home / "Apps/desktop-connector.AppImage"
        self.appimage.parent.mkdir(parents=True, exist_ok=True)
        self.appimage.write_text("#!/bin/bash\nexit 0\n")
        self.appimage.chmod(0o755)

        self.nautilus_dir = self.home / ".local/share/nautilus/scripts"
        self.dolphin_path = (
            self.home
            / ".local/share/kservices5/ServiceMenus"
            / DOLPHIN_SERVICE_FILENAME
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, config: Config, device_id: str, *, name: str) -> None:
        config.add_paired_device(device_id, f"pk-{device_id}", _key_b64(), name=name)
        config._data["paired_devices"][device_id]["paired_at"] = 10
        config.save()

    def _sync(self, config: Config, *, file_managers=("nautilus", "dolphin")) -> None:
        sync_file_manager_targets(
            config,
            appimage_path=self.appimage,
            home=self.home,
            file_managers=set(file_managers),
        )

    def test_dev_twin_does_not_delete_canonical_managed_script(self) -> None:
        # Canonical install pairs Alpha and writes its script.
        self._add(self.canonical_config, "dev-A", name="Alpha")
        self._sync(self.canonical_config)
        alpha = self.nautilus_dir / "Send to Alpha"
        self.assertTrue(alpha.exists())
        original = alpha.read_text()
        self.assertIn(f"{CONFIG_ID_PREFIX}{APP_NAME}", original)

        # Dev twin (no pairs) syncs into the same shared dir.
        self._sync(self.dev_config, file_managers=("nautilus",))

        # Canonical script must still be there, byte-identical.
        self.assertTrue(alpha.exists())
        self.assertEqual(alpha.read_text(), original)

    def test_dev_twin_does_not_remove_legacy_send_to_phone(self) -> None:
        # Pre-fix legacy AppImage hook script (no managed sentinel,
        # only the legacy fingerprint).
        self.nautilus_dir.mkdir(parents=True, exist_ok=True)
        legacy = self.nautilus_dir / LEGACY_NAUTILUS_NAME
        legacy.write_text(
            "#!/usr/bin/env python3\n"
            '"""Send selected files to phone via Desktop Connector."""\n'
            "# old single-pair body\n",
        )
        legacy.chmod(0o755)

        self._sync(self.dev_config, file_managers=("nautilus",))

        # Dev twin must leave it alone — only canonical adopts legacy.
        self.assertTrue(legacy.exists())

    def test_dev_twin_does_not_delete_unmarked_managed_script(self) -> None:
        # Pre-fix managed script (sentinel + pairing id, no config id):
        # came from a canonical install built before this fix.
        self.nautilus_dir.mkdir(parents=True, exist_ok=True)
        legacy_managed = self.nautilus_dir / "Send to Vivo Phone"
        legacy_managed.write_text(
            "#!/usr/bin/env python3\n"
            '"""auto-managed."""\n'
            f"# {MANAGED_SENTINEL}\n"
            f"# {PAIRING_ID_PREFIX}old-peer-id\n"
            "# (no config-id marker — pre-fix script)\n"
            "TARGET_DEVICE_ID = 'old-peer-id'\n",
        )
        legacy_managed.chmod(0o755)

        self._sync(self.dev_config, file_managers=("nautilus",))

        self.assertTrue(legacy_managed.exists())

    def test_canonical_adopts_unmarked_managed_script(self) -> None:
        # Same pre-fix unmarked managed script — canonical install
        # must still be able to clean it up (so older scripts get
        # eventually rewritten with the new marker on first sync).
        self._add(self.canonical_config, "dev-A", name="Alpha")
        self.nautilus_dir.mkdir(parents=True, exist_ok=True)
        legacy_managed = self.nautilus_dir / "Send to Vivo Phone"
        legacy_managed.write_text(
            "#!/usr/bin/env python3\n"
            '"""auto-managed."""\n'
            f"# {MANAGED_SENTINEL}\n"
            f"# {PAIRING_ID_PREFIX}old-peer-id\n"
            "TARGET_DEVICE_ID = 'old-peer-id'\n",
        )
        legacy_managed.chmod(0o755)

        self._sync(self.canonical_config, file_managers=("nautilus",))

        # Canonical removed it (its peer isn't in the canonical pairs).
        self.assertFalse(legacy_managed.exists())
        # And wrote the new script (with config marker).
        new_alpha = self.nautilus_dir / "Send to Alpha"
        self.assertTrue(new_alpha.exists())
        self.assertIn(f"{CONFIG_ID_PREFIX}{APP_NAME}", new_alpha.read_text())

    def test_filename_collision_preserves_other_configs_script(self) -> None:
        # Both installs paired the same peer with the same display
        # name — filenames collide. Canonical wrote first; dev twin's
        # write must refuse and not corrupt the file.
        self._add(self.canonical_config, "shared-peer", name="Phone")
        self._add(self.dev_config, "shared-peer", name="Phone")
        self._sync(self.canonical_config, file_managers=("nautilus",))
        target = self.nautilus_dir / "Send to Phone"
        canonical_text = target.read_text()
        self.assertIn(f"{CONFIG_ID_PREFIX}{APP_NAME}", canonical_text)

        self._sync(self.dev_config, file_managers=("nautilus",))

        self.assertEqual(target.read_text(), canonical_text)

    def test_dev_twin_does_not_remove_canonical_dolphin_service(self) -> None:
        self._add(self.canonical_config, "dev-A", name="Alpha")
        self._sync(self.canonical_config, file_managers=("dolphin",))
        original = self.dolphin_path.read_text()
        self.assertIn(f"{CONFIG_ID_PREFIX}{APP_NAME}", original)

        # Dev twin with no pairs would historically delete the
        # Dolphin service file in the no-devices branch. With the
        # ownership check it must leave it alone.
        self._sync(self.dev_config, file_managers=("dolphin",))

        self.assertTrue(self.dolphin_path.exists())
        self.assertEqual(self.dolphin_path.read_text(), original)

    def test_dev_twin_does_not_overwrite_canonical_dolphin_service(self) -> None:
        self._add(self.canonical_config, "dev-A", name="Alpha")
        self._sync(self.canonical_config, file_managers=("dolphin",))
        canonical_text = self.dolphin_path.read_text()

        # Dev twin pairs a different peer and tries to write — the
        # collision check refuses to clobber a managed file owned by
        # another config.
        self._add(self.dev_config, "dev-B", name="Beta")
        self._sync(self.dev_config, file_managers=("dolphin",))

        self.assertEqual(self.dolphin_path.read_text(), canonical_text)

    def test_dev_twin_writes_its_own_scripts_normally(self) -> None:
        # Sanity check: even though we lock the dev twin out of
        # cross-config writes, it can still write its own per-pair
        # scripts when the shared dir has no canonical entries.
        self._add(self.dev_config, "dev-Z", name="Zeta")
        self._sync(self.dev_config, file_managers=("nautilus",))
        zeta = self.nautilus_dir / "Send to Zeta"
        self.assertTrue(zeta.exists())
        self.assertIn(
            f"{CONFIG_ID_PREFIX}desktop-connector-dev",
            zeta.read_text(),
        )

    def test_canonical_can_still_clean_its_own_stale_scripts(self) -> None:
        # Regression: ownership gating must not prevent the canonical
        # install from cleaning up its OWN stale managed entries
        # (e.g. after an unpair).
        self._add(self.canonical_config, "dev-A", name="Alpha")
        self._sync(self.canonical_config, file_managers=("nautilus",))
        self.assertTrue((self.nautilus_dir / "Send to Alpha").exists())

        ConnectedDeviceRegistry(self.canonical_config).unpair("dev-A")
        self._sync(self.canonical_config, file_managers=("nautilus",))
        self.assertFalse((self.nautilus_dir / "Send to Alpha").exists())


if __name__ == "__main__":
    unittest.main()
