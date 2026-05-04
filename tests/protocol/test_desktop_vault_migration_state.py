"""T9.1 — Migration state machine + persistence."""

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

from src.vault_migration import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    MigrationRecord,
    MigrationTransitionError,
    PREVIOUS_RELAY_GRACE_DAYS,
    clear_state,
    crash_recovery_action,
    default_state_path,
    load_state,
    previous_relay_expired,
    save_state,
    transition,
)


VAULT_ID = "ABCD2345WXYZ"
SOURCE = "https://old.example.com"
TARGET = "https://new.example.com"
T_START = "2026-05-04T10:00:00.000Z"


class MigrationTransitionTests(unittest.TestCase):
    def test_allowed_transitions_match_h2_diagram(self) -> None:
        self.assertEqual(ALLOWED_TRANSITIONS["idle"], {"started"})
        self.assertEqual(ALLOWED_TRANSITIONS["started"], {"copying", "idle"})
        self.assertEqual(ALLOWED_TRANSITIONS["copying"], {"verified", "idle"})
        self.assertEqual(ALLOWED_TRANSITIONS["verified"], {"committed", "idle"})
        self.assertEqual(ALLOWED_TRANSITIONS["committed"], {"idle"})

    def test_started_to_copying_to_verified_to_committed_to_idle(self) -> None:
        record = _started_record()
        record = transition(record, to="copying", now="2026-05-04T10:01:00.000Z")
        self.assertEqual(record.state, "copying")

        record = transition(record, to="verified", now="2026-05-04T10:05:00.000Z")
        self.assertEqual(record.state, "verified")
        self.assertEqual(record.verified_at, "2026-05-04T10:05:00.000Z")

        record = transition(record, to="committed", now="2026-05-04T10:06:00.000Z")
        self.assertEqual(record.state, "committed")
        self.assertEqual(record.committed_at, "2026-05-04T10:06:00.000Z")
        # previous_relay_url stamped on commit.
        self.assertEqual(record.previous_relay_url, SOURCE)

        record = transition(record, to="idle", now="2026-05-04T10:07:00.000Z")
        self.assertEqual(record.state, "idle")

    def test_rollback_from_started_or_copying_or_verified(self) -> None:
        for from_state in ("started", "copying", "verified"):
            with self.subTest(from_state=from_state):
                record = _started_record()
                if from_state != "started":
                    record = transition(record, to="copying")
                if from_state == "verified":
                    record = transition(record, to="verified")
                rolled = transition(record, to="idle")
                self.assertEqual(rolled.state, "idle")

    def test_illegal_transition_raises(self) -> None:
        record = _started_record()
        with self.assertRaises(MigrationTransitionError):
            transition(record, to="committed")  # skipping copy + verify
        with self.assertRaises(MigrationTransitionError):
            transition(record, to="verified")   # skipping copy


class MigrationPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_migration_state_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_round_trip(self) -> None:
        record = _started_record()
        record.migration_token = "mig_token_123"
        save_state(record, self.tmpdir)
        self.assertTrue(default_state_path(self.tmpdir).exists())

        loaded = load_state(self.tmpdir)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.vault_id, record.vault_id)
        self.assertEqual(loaded.state, "started")
        self.assertEqual(loaded.migration_token, "mig_token_123")

    def test_clear_state_removes_file(self) -> None:
        save_state(_started_record(), self.tmpdir)
        self.assertTrue(default_state_path(self.tmpdir).exists())
        clear_state(self.tmpdir)
        self.assertFalse(default_state_path(self.tmpdir).exists())
        self.assertIsNone(load_state(self.tmpdir))

    def test_load_returns_none_when_file_missing(self) -> None:
        self.assertIsNone(load_state(self.tmpdir))

    def test_load_returns_none_on_garbled_state(self) -> None:
        path = default_state_path(self.tmpdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not really json")
        self.assertIsNone(load_state(self.tmpdir))


class MigrationCrashRecoveryTests(unittest.TestCase):
    def test_resume_copy_when_state_is_started_or_copying(self) -> None:
        for state in ("started", "copying"):
            record = _started_record()
            if state == "copying":
                record = transition(record, to="copying")
            self.assertEqual(crash_recovery_action(record).action, "resume_copy")

    def test_prompt_user_when_state_is_verified(self) -> None:
        record = transition(transition(_started_record(), to="copying"), to="verified")
        self.assertEqual(
            crash_recovery_action(record).action,
            "prompt_switch_rollback_resume_verify",
        )

    def test_switch_to_target_when_committed_within_grace(self) -> None:
        record = _committed_record(committed_at="2026-05-04T10:00:00.000Z")
        action = crash_recovery_action(
            record, now="2026-05-06T10:00:00.000Z",  # 2 days later
        )
        self.assertEqual(action.action, "switch_to_target")

    def test_drop_previous_relay_after_grace_period(self) -> None:
        record = _committed_record(committed_at="2026-04-01T10:00:00.000Z")
        action = crash_recovery_action(
            record, now="2026-05-04T10:00:00.000Z",  # 33 days later
        )
        self.assertEqual(action.action, "drop_previous_relay")

    def test_previous_relay_expired_helper_uses_h2_seven_day_window(self) -> None:
        self.assertEqual(PREVIOUS_RELAY_GRACE_DAYS, 7)
        record = _committed_record(committed_at="2026-05-04T10:00:00.000Z")
        # 6 days later: still in grace.
        self.assertFalse(previous_relay_expired(
            record, now="2026-05-10T10:00:00.000Z",
        ))
        # 8 days later: expired.
        self.assertTrue(previous_relay_expired(
            record, now="2026-05-12T10:00:00.000Z",
        ))


def _started_record() -> MigrationRecord:
    return MigrationRecord(
        vault_id=VAULT_ID,
        state="started",
        source_relay_url=SOURCE,
        target_relay_url=TARGET,
        started_at=T_START,
    )


def _committed_record(*, committed_at: str) -> MigrationRecord:
    return MigrationRecord(
        vault_id=VAULT_ID,
        state="committed",
        source_relay_url=SOURCE,
        target_relay_url=TARGET,
        started_at=T_START,
        verified_at="2026-04-01T09:00:00.000Z",
        committed_at=committed_at,
        previous_relay_url=SOURCE,
    )


if __name__ == "__main__":
    unittest.main()
