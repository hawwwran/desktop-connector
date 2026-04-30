"""Config tests for the desktop theme_mode pref."""

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

# Tests must not write to the dev machine's keyring.
os.environ["DESKTOP_CONNECTOR_NO_KEYRING"] = "1"

from src.config import (  # noqa: E402
    DEFAULT_THEME_MODE,
    THEME_MODE_DARK,
    THEME_MODE_LIGHT,
    THEME_MODE_SYSTEM,
    Config,
)


class ThemeModeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _read_config(self) -> dict:
        return json.loads((self.config_dir / "config.json").read_text())

    def test_default_is_system(self):
        config = Config(self.config_dir)
        self.assertEqual(config.theme_mode, THEME_MODE_SYSTEM)
        self.assertEqual(DEFAULT_THEME_MODE, THEME_MODE_SYSTEM)

    def test_setter_persists_each_allowed_value(self):
        config = Config(self.config_dir)
        for value in (THEME_MODE_LIGHT, THEME_MODE_DARK, THEME_MODE_SYSTEM):
            config.theme_mode = value
            self.assertEqual(config.theme_mode, value)
            self.assertEqual(self._read_config()["theme_mode"], value)

    def test_setter_rejects_unknown_value(self):
        config = Config(self.config_dir)
        config.theme_mode = "neon"  # type: ignore[assignment]
        self.assertEqual(config.theme_mode, DEFAULT_THEME_MODE)

    def test_getter_falls_back_when_stored_value_corrupt(self):
        (self.config_dir / "config.json").write_text(
            json.dumps({"theme_mode": "blueprint"})
        )
        config = Config(self.config_dir)
        self.assertEqual(config.theme_mode, DEFAULT_THEME_MODE)


if __name__ == "__main__":
    unittest.main()
