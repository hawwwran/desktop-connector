"""Config and registry tests for desktop multi-device support M.0."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.devices import (  # noqa: E402
    ConnectedDeviceRegistry,
    DuplicateDeviceNameError,
    next_default_device_name,
)
from src.secrets import pairing_symkey_key  # noqa: E402


class _RecordingSecureStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def is_secure(self) -> bool:
        return True


class ConnectedDeviceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        self.config = Config(self.config_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_device(
        self,
        device_id: str,
        *,
        name: str,
        paired_at: int,
    ) -> None:
        self.config.add_paired_device(
            device_id,
            f"pk-{device_id}",
            f"sk-{device_id}",
            name=name,
        )
        self.config._data["paired_devices"][device_id]["paired_at"] = paired_at
        self.config.save()

    def _read_config(self) -> dict:
        return json.loads((self.config_dir / "config.json").read_text())

    def test_list_devices_sorts_newest_first(self) -> None:
        self._add_device("dev-old", name="Old", paired_at=10)
        self._add_device("dev-new", name="New", paired_at=30)
        self._add_device("dev-mid", name="Mid", paired_at=20)

        devices = ConnectedDeviceRegistry(self.config).list_devices()

        self.assertEqual(
            [device.device_id for device in devices],
            ["dev-new", "dev-mid", "dev-old"],
        )

    def test_active_device_falls_back_and_clears_stale_id(self) -> None:
        self._add_device("dev-old", name="Old", paired_at=10)
        self._add_device("dev-new", name="New", paired_at=20)
        self.config.active_device_id = "dev-missing"

        active = ConnectedDeviceRegistry(self.config).get_active_device()

        self.assertIsNotNone(active)
        self.assertEqual(active.device_id, "dev-new")
        self.assertIsNone(self.config.active_device_id)
        self.assertNotIn("active_device_id", self._read_config())

    def test_mark_active_persists_selected_device(self) -> None:
        self._add_device("dev-A", name="A", paired_at=10)

        device = ConnectedDeviceRegistry(self.config).mark_active(
            "dev-A",
            reason="test",
        )

        self.assertEqual(device.device_id, "dev-A")
        self.assertEqual(self.config.active_device_id, "dev-A")
        self.assertEqual(self._read_config()["active_device_id"], "dev-A")

    def test_active_device_update_preserves_pairings_added_by_other_process(self) -> None:
        self._add_device("dev-A", name="A", paired_at=10)
        stale = Config(self.config_dir)
        other_process = Config(self.config_dir)
        other_process.add_paired_device("dev-B", "pk-dev-B", "sk-dev-B", name="B")

        stale.active_device_id = "dev-A"

        data = self._read_config()
        self.assertEqual(set(data["paired_devices"]), {"dev-A", "dev-B"})
        self.assertEqual(data["active_device_id"], "dev-A")

    def test_get_pairing_symkey_reloads_pairings_added_by_other_process(self) -> None:
        stale = Config(self.config_dir)
        other_process = Config(self.config_dir)
        other_process.add_paired_device("dev-B", "pk-dev-B", "sk-dev-B", name="B")

        self.assertEqual(stale.get_pairing_symkey("dev-B"), "sk-dev-B")

    def test_remove_paired_device_preserves_pairings_added_by_other_process(self) -> None:
        self._add_device("dev-A", name="A", paired_at=10)
        stale = Config(self.config_dir)
        other_process = Config(self.config_dir)
        other_process.add_paired_device("dev-B", "pk-dev-B", "sk-dev-B", name="B")

        stale.remove_paired_device("dev-A")

        data = self._read_config()
        self.assertNotIn("dev-A", data["paired_devices"])
        self.assertIn("dev-B", data["paired_devices"])

    def test_next_default_name_starts_at_pair_count_plus_one(self) -> None:
        self.assertEqual(
            next_default_device_name(["Device 1", "Other"]),
            "Device 3",
        )
        self.assertEqual(
            next_default_device_name(["Device 1", "Device 3"]),
            "Device 4",
        )

    def test_rename_rejects_empty_or_duplicate_names(self) -> None:
        self._add_device("dev-A", name="Tablet", paired_at=20)
        self._add_device("dev-B", name="Laptop", paired_at=10)
        registry = ConnectedDeviceRegistry(self.config)

        with self.assertRaises(DuplicateDeviceNameError):
            registry.rename("dev-B", " tablet ")

        with self.assertRaises(ValueError):
            registry.rename("dev-B", "  ")

        renamed = registry.rename("dev-B", " Laptop Pro ")

        self.assertEqual(renamed.name, "Laptop Pro")
        self.assertEqual(
            self._read_config()["paired_devices"]["dev-B"]["name"],
            "Laptop Pro",
        )

    def test_duplicate_names_are_normalized_and_persisted(self) -> None:
        self._add_device("dev-new", name="Device", paired_at=20)
        self._add_device("dev-old", name="device", paired_at=10)
        registry = ConnectedDeviceRegistry(self.config)

        changed = registry.normalize_duplicate_names()
        devices = registry.list_devices(normalize_names=False)

        self.assertTrue(changed)
        self.assertEqual(devices[0].name, "Device")
        self.assertEqual(devices[1].name, "device dev-old")
        self.assertEqual(
            self._read_config()["paired_devices"]["dev-old"]["name"],
            "device dev-old",
        )

    def test_remove_paired_device_clears_active_id(self) -> None:
        self._add_device("dev-A", name="A", paired_at=10)
        self.config.active_device_id = "dev-A"

        self.config.remove_paired_device("dev-A")

        self.assertIsNone(self.config.active_device_id)
        self.assertNotIn("active_device_id", self._read_config())

    def test_secure_store_metadata_updates_do_not_write_symkeys_to_json(self) -> None:
        store = _RecordingSecureStore()
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            config = Config(config_dir, secret_store=store)
            config.add_paired_device("dev-new", "pk-new", "sk-new", name="Device")
            config.add_paired_device("dev-old", "pk-old", "sk-old", name="device")
            config._data["paired_devices"]["dev-new"]["paired_at"] = 20
            config._data["paired_devices"]["dev-old"]["paired_at"] = 10
            config.save()

            ConnectedDeviceRegistry(config).normalize_duplicate_names()

            data = json.loads((config_dir / "config.json").read_text())
            self.assertEqual(
                store.values[pairing_symkey_key("dev-new")],
                "sk-new",
            )
            self.assertEqual(
                store.values[pairing_symkey_key("dev-old")],
                "sk-old",
            )
            for entry in data["paired_devices"].values():
                self.assertNotIn("symmetric_key_b64", entry)
            self.assertEqual(
                data["paired_devices"]["dev-old"]["name"],
                "device dev-old",
            )


if __name__ == "__main__":
    unittest.main()
