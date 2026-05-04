"""T17.2 — vault.log filter + rotation handler."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_logging import (  # noqa: E402
    VAULT_LOG_BACKUPS, VAULT_LOG_MAX_BYTES, VAULT_LOG_NAME,
    VAULT_TAG_PREFIX,
    attach_vault_log_handler, detach_vault_log_handler,
)


class VaultLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_log_test_"))
        # Ensure no leftover vault handler.
        detach_vault_log_handler()

    def tearDown(self) -> None:
        detach_vault_log_handler()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_constants(self) -> None:
        self.assertEqual(VAULT_LOG_NAME, "vault.log")
        self.assertEqual(VAULT_TAG_PREFIX, "vault.")
        self.assertEqual(VAULT_LOG_MAX_BYTES, 1_000_000)
        self.assertEqual(VAULT_LOG_BACKUPS, 1)

    def test_attach_creates_file_and_filters_to_vault_messages(self) -> None:
        attach_vault_log_handler(self.tmpdir)
        log = logging.getLogger("test.vault_logging")
        log.setLevel(logging.INFO)
        log.info("vault.sync.binding_paused binding=rb_v1_a")
        log.info("transfer.init.accepted transfer_id=abc123")
        log.info("vault.purge.scheduled vault=ABCD-2345-WXYZ job=jb_v1_x")

        # Flush handlers.
        for h in logging.getLogger().handlers:
            h.flush()

        log_path = self.tmpdir / "logs" / VAULT_LOG_NAME
        self.assertTrue(log_path.is_file())
        contents = log_path.read_text()
        self.assertIn("vault.sync.binding_paused", contents)
        self.assertIn("vault.purge.scheduled", contents)
        # Non-vault lines must NOT land here.
        self.assertNotIn("transfer.init.accepted", contents)

    def test_attach_is_idempotent(self) -> None:
        h1 = attach_vault_log_handler(self.tmpdir)
        h2 = attach_vault_log_handler(self.tmpdir)
        self.assertIs(h1, h2)
        # Only one vault-marker handler on the root logger.
        marked = [
            h for h in logging.getLogger().handlers
            if getattr(h, "_vault_log_marker", False)
        ]
        self.assertEqual(len(marked), 1)

    def test_detach_removes_handler(self) -> None:
        attach_vault_log_handler(self.tmpdir)
        self.assertTrue(detach_vault_log_handler())
        marked = [
            h for h in logging.getLogger().handlers
            if getattr(h, "_vault_log_marker", False)
        ]
        self.assertEqual(marked, [])
        # Second detach is a no-op.
        self.assertFalse(detach_vault_log_handler())

    def test_handler_factory_override_for_tests(self) -> None:
        captured = []
        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        def factory(path, max_bytes, backup_count):
            return _Capture()

        attach_vault_log_handler(self.tmpdir, handler_factory=factory)
        log = logging.getLogger("test.vault_logging.factory")
        log.setLevel(logging.INFO)
        log.info("vault.atomic.sweep_removed root=/x count=2")
        log.info("clipboard.write_text.succeeded")
        self.assertEqual(captured, ["vault.atomic.sweep_removed root=/x count=2"])


if __name__ == "__main__":
    unittest.main()
