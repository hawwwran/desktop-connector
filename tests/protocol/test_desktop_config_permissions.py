"""Tests for hardening-plan H.1 — restrictive perms on config + history."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import (  # noqa: E402
    CONFIG_DIR_MODE,
    CONFIG_FILE_MODE,
    Config,
)
from src.history import (  # noqa: E402
    HISTORY_FILE_MODE,
    TransferHistory,
)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class ConfigPermissionsTests(unittest.TestCase):
    def test_init_creates_dir_with_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "fresh"
            Config(config_dir=config_dir)
            self.assertEqual(_mode(config_dir), CONFIG_DIR_MODE)

    def test_init_tightens_existing_loose_dir(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "preexisting"
            config_dir.mkdir(mode=0o755)
            self.assertEqual(_mode(config_dir), 0o755)
            Config(config_dir=config_dir)
            self.assertEqual(_mode(config_dir), CONFIG_DIR_MODE)

    def test_save_writes_file_with_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            cfg = Config(config_dir=config_dir)
            cfg.device_id = "dev-001"  # triggers save()
            cfg_path = config_dir / "config.json"
            self.assertTrue(cfg_path.exists())
            self.assertEqual(_mode(cfg_path), CONFIG_FILE_MODE)

    def test_save_self_heals_pre_existing_loose_file(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            config_dir.mkdir(exist_ok=True)
            cfg_path = config_dir / "config.json"
            cfg_path.write_text("{}")
            cfg_path.chmod(0o644)
            self.assertEqual(_mode(cfg_path), 0o644)
            cfg = Config(config_dir=config_dir)
            cfg.device_id = "dev-002"
            self.assertEqual(_mode(cfg_path), CONFIG_FILE_MODE)

    def test_save_leaves_no_orphan_tmp_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            cfg = Config(config_dir=config_dir)
            cfg.device_id = "dev-003"
            stragglers = list(config_dir.glob(".config.json.*.tmp"))
            self.assertEqual(stragglers, [], f"orphan tmp files: {stragglers}")

    def test_save_atomic_under_concurrent_reader_view(self):
        # Sanity: after save() returns, the file at config_file is the
        # finished one (no truncated intermediate state visible to readers).
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            cfg = Config(config_dir=config_dir)
            cfg.device_id = "dev-004"
            content = (config_dir / "config.json").read_text()
            self.assertIn("dev-004", content)
            self.assertTrue(content.startswith("{"))
            self.assertTrue(content.rstrip().endswith("}"))


class HistoryPermissionsTests(unittest.TestCase):
    def test_locked_write_sets_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            history = TransferHistory(config_dir=config_dir)
            history.add(
                filename="x.txt",
                display_label="x.txt",
                direction="sent",
                size=10,
                transfer_id="t-001",
            )
            history_path = config_dir / "history.json"
            self.assertTrue(history_path.exists())
            self.assertEqual(_mode(history_path), HISTORY_FILE_MODE)

    def test_locked_write_self_heals_pre_existing_loose_file(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            history_path = config_dir / "history.json"
            history_path.write_text("[]")
            history_path.chmod(0o644)
            self.assertEqual(_mode(history_path), 0o644)
            history = TransferHistory(config_dir=config_dir)
            history.add(
                filename="x.txt",
                display_label="x.txt",
                direction="sent",
                size=10,
                transfer_id="t-002",
            )
            self.assertEqual(_mode(history_path), HISTORY_FILE_MODE)


if __name__ == "__main__":
    unittest.main()
