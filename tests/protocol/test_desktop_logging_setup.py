"""Tests for the bootstrap-stage allow_logging helper.

Companion to hardening-plan H.4: ``setup_logging`` must NOT
instantiate ``Config()`` — Config.__init__ has side effects
(perm fixes, secret-store selection, legacy-secret migration)
that emit log lines we want to land in the file handler. The
helper reads ``allow_logging`` directly from ``config.json``
so the file handler is wired up before any ``Config()`` runs
later in startup.
"""

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

from src.bootstrap.logging_setup import _read_allow_logging_flag  # noqa: E402


class AllowLoggingFlagReaderTests(unittest.TestCase):
    def test_missing_config_json_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(_read_allow_logging_flag(Path(td)))

    def test_true_flag_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config.json").write_text(
                json.dumps({"allow_logging": True})
            )
            self.assertTrue(_read_allow_logging_flag(Path(td)))

    def test_false_flag_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config.json").write_text(
                json.dumps({"allow_logging": False})
            )
            self.assertFalse(_read_allow_logging_flag(Path(td)))

    def test_missing_key_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config.json").write_text(
                json.dumps({"server_url": "http://x"})
            )
            self.assertFalse(_read_allow_logging_flag(Path(td)))

    def test_malformed_json_returns_false_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config.json").write_text("{ this is not json")
            # Must not raise.
            self.assertFalse(_read_allow_logging_flag(Path(td)))

    def test_does_not_instantiate_config(self) -> None:
        # Config.__init__ would run the H.4 migration as a side
        # effect. The helper must not import / construct Config —
        # we verify by ensuring the side-effects of Config's
        # __init__ never fire (the dir we point at is empty
        # before AND after the helper call).
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "fresh"
            self.assertFalse(config_dir.exists())
            _read_allow_logging_flag(config_dir)
            # If Config had been instantiated, mkdir(parents=True,
            # exist_ok=True) would have created the dir.
            self.assertFalse(
                config_dir.exists(),
                "_read_allow_logging_flag must not have side effects "
                "on the config_dir — Config() would have created it",
            )


if __name__ == "__main__":
    unittest.main()
