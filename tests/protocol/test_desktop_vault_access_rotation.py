"""T13.6 — client-side access-secret rotation + 7-day reminder banner."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_access_rotation import (  # noqa: E402
    REMINDER_TTL_SECONDS, ReminderState, SECRET_BYTES,
    acknowledge_device, clear_reminder, generate_new_secret,
    read_reminder, reminder_path, rotation_request_body,
    secret_to_wire_hash, write_reminder,
)


class SecretGenerationTests(unittest.TestCase):
    def test_default_byte_length_is_32(self) -> None:
        self.assertEqual(SECRET_BYTES, 32)

    def test_secret_is_url_safe_and_high_entropy(self) -> None:
        s = generate_new_secret()
        # token_urlsafe length: ceil(byte_length * 4/3); 32 → 43 chars unpadded.
        self.assertEqual(len(s), 43)
        for ch in s:
            self.assertIn(ch, set(
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789-_"
            ))

    def test_distinct_secrets_per_call(self) -> None:
        a = generate_new_secret()
        b = generate_new_secret()
        self.assertNotEqual(a, b)

    def test_rejects_low_entropy_byte_lengths(self) -> None:
        with self.assertRaises(ValueError):
            generate_new_secret(byte_length=8)


class WireBodyTests(unittest.TestCase):
    def test_secret_to_wire_hash_matches_sha256(self) -> None:
        secret = "abcdefghij1234567890"
        wire = secret_to_wire_hash(secret)
        expected = hashlib.sha256(secret.encode()).digest()
        self.assertEqual(
            base64.b64decode(wire["new_vault_access_token_hash"]), expected,
        )
        self.assertEqual(wire["_hash_hex"], expected.hex())

    def test_rotation_request_body_carries_just_the_hash(self) -> None:
        body = rotation_request_body("super-secret-bytes")
        self.assertEqual(set(body.keys()), {"new_vault_access_token_hash"})
        self.assertEqual(
            base64.b64decode(body["new_vault_access_token_hash"]),
            hashlib.sha256(b"super-secret-bytes").digest(),
        )

    def test_rotation_request_body_with_triggered_by_revoke(self) -> None:
        body = rotation_request_body(
            "secret", triggered_by_revoke_grant_id="dg_v1_abc",
        )
        self.assertEqual(body["triggered_by_revoke_grant_id"], "dg_v1_abc")

    def test_secret_to_wire_hash_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            secret_to_wire_hash("")


class ReminderBannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_rotation_test_"))
        self.config_dir = self.tmpdir / "config"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_active_until_all_devices_acknowledge(self) -> None:
        state = ReminderState(
            rotated_at_epoch=1_000_000,
            paired_device_ids=("dev-a", "dev-b"),
        )
        # No acks yet → active.
        self.assertTrue(state.is_active(now=1_000_001))
        # One ack → still active.
        state = acknowledge_device(state, "dev-a")
        self.assertTrue(state.is_active(now=1_000_001))
        # All devices acked → cleared.
        state = acknowledge_device(state, "dev-b")
        self.assertFalse(state.is_active(now=1_000_001))

    def test_auto_clears_after_7_days(self) -> None:
        state = ReminderState(
            rotated_at_epoch=1_000_000,
            paired_device_ids=("dev-a",),
        )
        # Day 6: still active.
        self.assertTrue(state.is_active(now=1_000_000 + 6 * 86_400))
        # Day 7 boundary: cleared (>= TTL).
        self.assertFalse(state.is_active(now=1_000_000 + REMINDER_TTL_SECONDS))
        # Day 8: cleared.
        self.assertFalse(state.is_active(now=1_000_000 + 8 * 86_400))

    def test_acknowledge_is_idempotent(self) -> None:
        state = ReminderState(
            rotated_at_epoch=1, paired_device_ids=("dev-a",),
        )
        once = acknowledge_device(state, "dev-a")
        twice = acknowledge_device(once, "dev-a")
        self.assertEqual(once, twice)

    def test_round_trip_persistence(self) -> None:
        state = ReminderState(
            rotated_at_epoch=1_777_900_000,
            paired_device_ids=("dev-a", "dev-b"),
            acknowledged_device_ids=("dev-a",),
        )
        write_reminder(self.config_dir, "ABCD-2345-WXYZ", state)
        loaded = read_reminder(self.config_dir, "ABCD-2345-WXYZ")
        self.assertEqual(loaded, state)

    def test_read_returns_none_when_missing(self) -> None:
        loaded = read_reminder(self.config_dir, "ABCD-2345-WXYZ")
        self.assertIsNone(loaded)

    def test_clear_removes_file(self) -> None:
        state = ReminderState(
            rotated_at_epoch=1, paired_device_ids=("dev-a",),
        )
        write_reminder(self.config_dir, "ABCD-2345-WXYZ", state)
        clear_reminder(self.config_dir, "ABCD-2345-WXYZ")
        self.assertFalse(reminder_path(self.config_dir, "ABCD-2345-WXYZ").exists())

    def test_reminder_filename_sanitises_vault_id(self) -> None:
        # Slashes / dots should be filtered so a malformed vault_id can't
        # escape the config dir.
        path = reminder_path(self.config_dir, "../escape")
        self.assertEqual(path.parent, self.config_dir)
        self.assertEqual(path.name, "vault_rotation_escape.json")


if __name__ == "__main__":
    unittest.main()
