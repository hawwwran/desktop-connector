"""T14.3 — Hard-purge scheduling persistence + T14.5 toggle-OFF clear."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_purge_schedule import (  # noqa: E402
    DEFAULT_DELAY_SECONDS, PENDING_FILE_NAME,
    PendingPurge, VaultPurgeAlreadyScheduledError, VaultPurgeError,
    cancel_purge, clear_all_for_vault, generate_job_id,
    get_pending_purge, list_pending_purges, pending_file_path,
    schedule_purge,
)


VAULT = "ABCD-2345-WXYZ"
DEV = "abcd1234567890ef0123456789abcdef"
DOCS = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"


class JobIdTests(unittest.TestCase):
    def test_format_matches_server_regex(self) -> None:
        for _ in range(50):
            jid = generate_job_id()
            self.assertRegex(jid, r"^jb_v1_[a-z2-7]{24}$")


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_purge_test_"))
        self.config_dir = self.tmpdir / "config"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_default_delay_is_24_hours(self) -> None:
        self.assertEqual(DEFAULT_DELAY_SECONDS, 24 * 60 * 60)

    def test_schedule_writes_persistent_record(self) -> None:
        record = schedule_purge(
            self.config_dir,
            vault_id_dashed=VAULT,
            scope="folder",
            scope_target=DOCS,
            scheduled_by_device_id=DEV,
            now=1_000_000.0,
        )
        self.assertEqual(record.scheduled_for_epoch, 1_000_000 + DEFAULT_DELAY_SECONDS)
        # File exists at the expected path.
        path = pending_file_path(self.config_dir)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, PENDING_FILE_NAME)

        loaded = get_pending_purge(self.config_dir, VAULT)
        self.assertEqual(loaded, record)

    def test_persists_across_restart(self) -> None:
        schedule_purge(
            self.config_dir,
            vault_id_dashed=VAULT,
            scope="vault", scope_target=None,
            scheduled_by_device_id=DEV,
            now=1_000_000.0,
        )
        # "Restart": re-read.
        again = get_pending_purge(self.config_dir, VAULT)
        self.assertIsNotNone(again)
        self.assertEqual(again.scope, "vault")
        self.assertIsNone(again.scope_target)

    def test_second_schedule_for_same_vault_is_rejected(self) -> None:
        schedule_purge(
            self.config_dir,
            vault_id_dashed=VAULT, scope="folder",
            scope_target=DOCS, scheduled_by_device_id=DEV,
            now=1_000_000.0,
        )
        with self.assertRaises(VaultPurgeAlreadyScheduledError):
            schedule_purge(
                self.config_dir,
                vault_id_dashed=VAULT, scope="folder",
                scope_target=DOCS, scheduled_by_device_id=DEV,
                now=1_000_000.0,
            )

    def test_schedule_for_different_vaults_coexist(self) -> None:
        schedule_purge(
            self.config_dir,
            vault_id_dashed=VAULT, scope="folder",
            scope_target=DOCS, scheduled_by_device_id=DEV,
            now=1_000_000.0,
        )
        schedule_purge(
            self.config_dir,
            vault_id_dashed="OTHR-XXXX-YYYY", scope="vault",
            scope_target=None, scheduled_by_device_id=DEV,
            now=1_000_000.0,
        )
        self.assertEqual(len(list_pending_purges(self.config_dir)), 2)

    def test_scope_validation(self) -> None:
        with self.assertRaises(VaultPurgeError):
            schedule_purge(
                self.config_dir, vault_id_dashed=VAULT,
                scope="folder", scope_target=None,  # missing
                scheduled_by_device_id=DEV, now=1.0,
            )
        with self.assertRaises(VaultPurgeError):
            schedule_purge(
                self.config_dir, vault_id_dashed=VAULT,
                scope="vault", scope_target=DOCS,   # extraneous
                scheduled_by_device_id=DEV, now=1.0,
            )
        with self.assertRaises(VaultPurgeError):
            schedule_purge(
                self.config_dir, vault_id_dashed=VAULT,
                scope="bogus",  # type: ignore[arg-type]
                scope_target=None,
                scheduled_by_device_id=DEV, now=1.0,
            )

    def test_negative_delay_rejected(self) -> None:
        with self.assertRaises(VaultPurgeError):
            schedule_purge(
                self.config_dir, vault_id_dashed=VAULT,
                scope="vault", scope_target=None,
                scheduled_by_device_id=DEV,
                delay_seconds=-1, now=1.0,
            )

    def test_is_due_respects_supplied_now(self) -> None:
        record = schedule_purge(
            self.config_dir, vault_id_dashed=VAULT,
            scope="vault", scope_target=None,
            scheduled_by_device_id=DEV,
            delay_seconds=100, now=1_000.0,
        )
        self.assertFalse(record.is_due(now=1_050))
        self.assertTrue(record.is_due(now=1_100))
        self.assertTrue(record.is_due(now=1_200))


class CancelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_purge_cancel_"))
        self.config_dir = self.tmpdir / "config"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cancel_returns_record_and_clears(self) -> None:
        record = schedule_purge(
            self.config_dir, vault_id_dashed=VAULT,
            scope="vault", scope_target=None,
            scheduled_by_device_id=DEV, now=1_000.0,
        )
        cleared = cancel_purge(self.config_dir, VAULT)
        self.assertEqual(cleared, record)
        self.assertIsNone(get_pending_purge(self.config_dir, VAULT))

    def test_cancel_unknown_vault_returns_none(self) -> None:
        self.assertIsNone(cancel_purge(self.config_dir, VAULT))

    def test_clear_all_for_vault_aliases_cancel(self) -> None:
        """T14.5 toggle-OFF entrypoint."""
        schedule_purge(
            self.config_dir, vault_id_dashed=VAULT,
            scope="folder", scope_target=DOCS,
            scheduled_by_device_id=DEV, now=1_000.0,
        )
        cleared = clear_all_for_vault(self.config_dir, VAULT)
        self.assertIsNotNone(cleared)
        self.assertIsNone(get_pending_purge(self.config_dir, VAULT))


if __name__ == "__main__":
    unittest.main()
