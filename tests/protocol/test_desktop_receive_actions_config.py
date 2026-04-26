"""Config tests for desktop receive action defaults and migration."""

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

from src.config import (  # noqa: E402
    DEFAULT_RECEIVE_ACTIONS,
    RECEIVE_ACTION_COPY,
    RECEIVE_ACTION_NONE,
    RECEIVE_ACTION_OPEN,
    RECEIVE_KIND_DOCUMENT,
    RECEIVE_KIND_IMAGE,
    RECEIVE_KIND_TEXT,
    RECEIVE_KIND_URL,
    Config,
    allowed_receive_actions,
)


class ReceiveActionsConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_config(self, data: dict) -> None:
        (self.config_dir / "config.json").write_text(json.dumps(data))

    def _read_config(self) -> dict:
        return json.loads((self.config_dir / "config.json").read_text())

    def test_new_config_gets_default_receive_actions(self):
        config = Config(self.config_dir)

        self.assertEqual(config.receive_actions, DEFAULT_RECEIVE_ACTIONS)
        self.assertEqual(
            self._read_config()["receive_actions"],
            DEFAULT_RECEIVE_ACTIONS,
        )

    def test_text_action_defaults_to_copy(self):
        config = Config(self.config_dir)

        self.assertEqual(
            config.get_receive_action(RECEIVE_KIND_TEXT),
            RECEIVE_ACTION_COPY,
        )

    def test_old_auto_open_links_true_maps_url_to_open(self):
        self._write_config({"auto_open_links": True})

        config = Config(self.config_dir)

        self.assertEqual(
            config.get_receive_action(RECEIVE_KIND_URL),
            RECEIVE_ACTION_OPEN,
        )
        self.assertEqual(
            self._read_config()["receive_actions"][RECEIVE_KIND_URL],
            RECEIVE_ACTION_OPEN,
        )

    def test_old_auto_open_links_false_maps_url_to_none(self):
        self._write_config({"auto_open_links": False})

        config = Config(self.config_dir)

        self.assertEqual(
            config.get_receive_action(RECEIVE_KIND_URL),
            RECEIVE_ACTION_NONE,
        )
        self.assertEqual(
            self._read_config()["receive_actions"][RECEIVE_KIND_URL],
            RECEIVE_ACTION_NONE,
        )
        self.assertEqual(
            config.get_receive_action(RECEIVE_KIND_TEXT),
            RECEIVE_ACTION_COPY,
        )

    def test_partial_receive_actions_are_filled_with_defaults(self):
        self._write_config({
            "receive_actions": {
                RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN,
            },
        })

        config = Config(self.config_dir)

        expected = dict(DEFAULT_RECEIVE_ACTIONS)
        expected[RECEIVE_KIND_IMAGE] = RECEIVE_ACTION_OPEN
        self.assertEqual(config.receive_actions, expected)
        self.assertEqual(self._read_config()["receive_actions"], expected)

    def test_invalid_values_and_unknown_kinds_are_removed(self):
        self._write_config({
            "receive_actions": {
                RECEIVE_KIND_URL: "launch",
                RECEIVE_KIND_TEXT: RECEIVE_ACTION_OPEN,
                RECEIVE_KIND_IMAGE: RECEIVE_ACTION_COPY,
                RECEIVE_KIND_DOCUMENT: RECEIVE_ACTION_OPEN,
                "archive": RECEIVE_ACTION_OPEN,
            },
        })

        config = Config(self.config_dir)

        expected = dict(DEFAULT_RECEIVE_ACTIONS)
        expected[RECEIVE_KIND_DOCUMENT] = RECEIVE_ACTION_OPEN
        self.assertEqual(config.receive_actions, expected)
        self.assertEqual(self._read_config()["receive_actions"], expected)

    def test_receive_actions_setter_normalizes_and_persists(self):
        config = Config(self.config_dir)

        config.receive_actions = {
            RECEIVE_KIND_URL: RECEIVE_ACTION_COPY,
            RECEIVE_KIND_IMAGE: RECEIVE_ACTION_OPEN,
            "unknown": RECEIVE_ACTION_OPEN,
        }

        expected = dict(DEFAULT_RECEIVE_ACTIONS)
        expected[RECEIVE_KIND_URL] = RECEIVE_ACTION_COPY
        expected[RECEIVE_KIND_IMAGE] = RECEIVE_ACTION_OPEN
        self.assertEqual(config.receive_actions, expected)
        self.assertEqual(self._read_config()["receive_actions"], expected)

    def test_set_receive_action_ignores_invalid_pairs(self):
        config = Config(self.config_dir)

        config.set_receive_action(RECEIVE_KIND_URL, RECEIVE_ACTION_COPY)
        config.set_receive_action(RECEIVE_KIND_IMAGE, RECEIVE_ACTION_COPY)
        config.set_receive_action("archive", RECEIVE_ACTION_OPEN)

        expected = dict(DEFAULT_RECEIVE_ACTIONS)
        expected[RECEIVE_KIND_URL] = RECEIVE_ACTION_COPY
        self.assertEqual(config.receive_actions, expected)
        self.assertEqual(self._read_config()["receive_actions"], expected)

    def test_allowed_receive_actions_are_kind_specific(self):
        self.assertIn(RECEIVE_ACTION_COPY, allowed_receive_actions(RECEIVE_KIND_URL))
        self.assertIn(RECEIVE_ACTION_COPY, allowed_receive_actions(RECEIVE_KIND_TEXT))
        self.assertNotIn(RECEIVE_ACTION_OPEN, allowed_receive_actions(RECEIVE_KIND_TEXT))
        self.assertNotIn(RECEIVE_ACTION_COPY, allowed_receive_actions(RECEIVE_KIND_IMAGE))
        self.assertEqual(allowed_receive_actions("archive"), set())

    def test_auto_open_links_property_remains_read_write(self):
        config = Config(self.config_dir)

        config.auto_open_links = False

        self.assertFalse(config.auto_open_links)
        self.assertFalse(self._read_config()["auto_open_links"])

    def test_reload_normalizes_receive_actions_written_by_another_process(self):
        config = Config(self.config_dir)
        self._write_config({
            "receive_actions": {
                RECEIVE_KIND_URL: RECEIVE_ACTION_COPY,
                RECEIVE_KIND_IMAGE: RECEIVE_ACTION_COPY,
            },
        })

        config.reload()

        expected = dict(DEFAULT_RECEIVE_ACTIONS)
        expected[RECEIVE_KIND_URL] = RECEIVE_ACTION_COPY
        self.assertEqual(config.receive_actions, expected)
        self.assertEqual(self._read_config()["receive_actions"], expected)


if __name__ == "__main__":
    unittest.main()
