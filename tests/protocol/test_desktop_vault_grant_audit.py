"""Wire 4 — best-effort audit publishes for grant lifecycle events.

Producer-side wires for ``vault.grant.created`` /
``vault.revoke.completed`` / ``vault.rotation.completed``: each
publishes one fresh root revision with the corresponding op-log
row appended after the relay-side grant op succeeds. Mirrors
Wire 2 / Wire 3's audit-publish shape but lives in
``vault/grant/audit.py`` because the three grant-side GTK4
callers share this surface.

The GTK4 callers (``grant_device_dialog``, ``tab_devices``,
``windows_vault_rotate``) reference the helper directly; live
coverage is exercised by the AT-SPI suites under
``docs/testing/vault-tests.md``. The unit tests here drive
``publish_grant_lifecycle_audit`` against a ``FakeRootRelay``.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.grant.audit import publish_grant_lifecycle_audit  # noqa: E402

from tests.protocol.test_desktop_vault_folders import (  # noqa: E402
    FakeRootRelay, _seed_empty_root,
)


DEVICE_A = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
TARGET_DEVICE = "0102030405060708090a0b0c0d0e0f10"
VAULT_ID = "ABCD2345WXYZ"
MASTER_KEY = bytes.fromhex(
    "0102030405060708090a0b0c0d0e0f10"
    "1112131415161718191a1b1c1d1e1f20"
)


def _build_vault_and_relay():
    relay = FakeRootRelay()
    vault = Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret="bearer",
        header_revision=0, manifest_revision=0,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )
    _seed_empty_root(relay, vault, created_at="2026-05-19T12:00:00.000Z")
    return vault, relay


class GrantCreatedAuditTests(unittest.TestCase):
    def test_publishes_grant_created_with_role_and_claimant(self) -> None:
        vault, relay = _build_vault_and_relay()
        ok = publish_grant_lifecycle_audit(
            vault=vault, relay=relay,
            event_type="vault.grant.created",
            author_device_id=DEVICE_A,
            extra={
                "approved_role": "member",
                "claimant_device_id": TARGET_DEVICE,
            },
        )
        self.assertTrue(ok)
        fetched = vault.fetch_root_manifest(relay)
        self.assertEqual(fetched["root_revision"], 2)
        tail = fetched.get("operation_log_tail") or []
        grant_rows = [
            e for e in tail
            if isinstance(e, dict)
            and e.get("type") == "vault.grant.created"
        ]
        self.assertEqual(len(grant_rows), 1)
        row = grant_rows[0]
        self.assertEqual(row["device_id"], DEVICE_A)
        self.assertEqual(row["approved_role"], "member")
        self.assertEqual(row["claimant_device_id"], TARGET_DEVICE)
        self.assertEqual(row["revision"], 2)


class RevokeCompletedAuditTests(unittest.TestCase):
    def test_publishes_revoke_completed_with_target_and_timestamp(self) -> None:
        vault, relay = _build_vault_and_relay()
        ok = publish_grant_lifecycle_audit(
            vault=vault, relay=relay,
            event_type="vault.revoke.completed",
            author_device_id=DEVICE_A,
            extra={
                "target_device_id": TARGET_DEVICE,
                "revoked_at": "2026-05-19T13:00:00.000Z",
                "already_revoked": False,
            },
        )
        self.assertTrue(ok)
        fetched = vault.fetch_root_manifest(relay)
        tail = fetched.get("operation_log_tail") or []
        rows = [
            e for e in tail
            if isinstance(e, dict)
            and e.get("type") == "vault.revoke.completed"
        ]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["target_device_id"], TARGET_DEVICE)
        self.assertEqual(row["revoked_at"], "2026-05-19T13:00:00.000Z")
        self.assertFalse(row["already_revoked"])


class RotationCompletedAuditTests(unittest.TestCase):
    def test_publishes_rotation_completed_with_timestamp(self) -> None:
        vault, relay = _build_vault_and_relay()
        ok = publish_grant_lifecycle_audit(
            vault=vault, relay=relay,
            event_type="vault.rotation.completed",
            author_device_id=DEVICE_A,
            extra={"rotated_at": "2026-05-19T14:00:00.000Z"},
        )
        self.assertTrue(ok)
        fetched = vault.fetch_root_manifest(relay)
        tail = fetched.get("operation_log_tail") or []
        rows = [
            e for e in tail
            if isinstance(e, dict)
            and e.get("type") == "vault.rotation.completed"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rotated_at"], "2026-05-19T14:00:00.000Z")


class GrantAuditEdgeCasesTests(unittest.TestCase):
    def test_empty_author_device_id_returns_false_without_publish(self) -> None:
        vault, relay = _build_vault_and_relay()
        # Snapshot root revision before — no publish should happen.
        rev_before = relay.root_revision
        ok = publish_grant_lifecycle_audit(
            vault=vault, relay=relay,
            event_type="vault.grant.created",
            author_device_id="",
            extra={"approved_role": "member"},
        )
        self.assertFalse(ok)
        self.assertEqual(relay.root_revision, rev_before)

    def test_returns_false_on_publish_exception(self) -> None:
        # Build a vault but pair it with a broken relay so fetch raises.
        vault = Vault(
            vault_id=VAULT_ID, master_key=MASTER_KEY,
            recovery_secret=None, vault_access_secret="bearer",
            header_revision=0, manifest_revision=0,
            manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
        )

        class BrokenRelay:
            def get_root(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        with self.assertLogs(
            "src.vault.grant.audit", level=logging.WARNING,
        ) as captured:
            ok = publish_grant_lifecycle_audit(
                vault=vault, relay=BrokenRelay(),
                event_type="vault.grant.created",
                author_device_id=DEVICE_A,
            )
        self.assertFalse(ok)
        # Warning log line carries the event type so an operator can
        # tell which grant op's audit row went missing.
        self.assertTrue(
            any("vault.grant.created" in line for line in captured.output),
            captured.output,
        )

    def test_vault_create_rides_alongside_on_first_followup(self) -> None:
        vault, relay = _build_vault_and_relay()
        ok = publish_grant_lifecycle_audit(
            vault=vault, relay=relay,
            event_type="vault.grant.created",
            author_device_id=DEVICE_A,
            extra={"approved_role": "member"},
        )
        self.assertTrue(ok)
        tail = vault.fetch_root_manifest(relay).get(
            "operation_log_tail",
        ) or []
        types = sorted(
            e.get("type") for e in tail if isinstance(e, dict)
        )
        # Both the grant audit AND the deferred vault.create row land
        # on the same publish.
        self.assertEqual(types, ["vault.create", "vault.grant.created"])


if __name__ == "__main__":
    unittest.main()
