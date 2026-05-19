"""§6.H2 — typed client around the device-grants HTTP surface."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.grant.client import (  # noqa: E402
    CannotRevokeSelfError,
    DeviceGrant,
    DeviceGrantNotFoundError,
    DeviceGrantsAuthError,
    DeviceGrantsError,
    RevokeResult,
    list_device_grants,
    revoke_device_grant,
)
from src.vault.relay_errors import VaultRelayError  # noqa: E402


VAULT_ID = "aaaabbbbccccddddeeeeffff00001111"
VAULT_ACCESS_SECRET = "vas_test_value"
TARGET_DEVICE = "1" * 32
CALLER_DEVICE = "2" * 32


class _FakeRelay:
    """Stand-in for ``VaultHttpRelay`` that returns scripted payloads
    or raises scripted errors. The two methods the client calls are
    ``list_device_grants`` and ``revoke_device_grant``."""

    def __init__(self) -> None:
        self.list_response: dict | None = None
        self.list_error: Exception | None = None
        self.revoke_response: dict | None = None
        self.revoke_error: Exception | None = None
        self.list_calls: list[tuple[str, str]] = []
        self.revoke_calls: list[tuple[str, str, str]] = []

    def list_device_grants(self, vault_id, vault_access_secret):
        self.list_calls.append((vault_id, vault_access_secret))
        if self.list_error is not None:
            raise self.list_error
        return self.list_response

    def revoke_device_grant(self, vault_id, vault_access_secret, target):
        self.revoke_calls.append((vault_id, vault_access_secret, target))
        if self.revoke_error is not None:
            raise self.revoke_error
        return self.revoke_response


def _server_error(*, status: int, code: str, message: str = "") -> VaultRelayError:
    return VaultRelayError(
        {"code": code, "message": message, "details": {}},
        status_code=status,
    )


class ListDeviceGrantsTests(unittest.TestCase):
    def test_parses_typed_dataclasses_from_server_payload(self) -> None:
        relay = _FakeRelay()
        relay.list_response = {
            "vault_id": VAULT_ID,
            "grants": [
                {
                    "grant_id": "g_1",
                    "device_id": CALLER_DEVICE,
                    "device_name": "Laptop A",
                    "role": "admin",
                    "granted_by": "self",
                    "granted_via": "create",
                    "granted_at": "2026-04-01T10:00:00.000Z",
                    "revoked_at": None,
                    "revoked_by": None,
                    "last_seen_at": "2026-05-18T12:00:00.000Z",
                    "is_revoked": False,
                    "is_caller": True,
                },
                {
                    "grant_id": "g_2",
                    "device_id": TARGET_DEVICE,
                    "device_name": None,
                    "role": "sync",
                    "granted_by": CALLER_DEVICE,
                    "granted_via": "qr_grant",
                    "granted_at": "2026-04-15T14:00:00.000Z",
                    "revoked_at": "2026-05-10T11:00:00.000Z",
                    "revoked_by": CALLER_DEVICE,
                    "last_seen_at": None,
                    "is_revoked": True,
                    "is_caller": False,
                },
            ],
        }

        grants = list_device_grants(relay, VAULT_ID, VAULT_ACCESS_SECRET)

        self.assertEqual(len(grants), 2)
        admin = grants[0]
        self.assertIsInstance(admin, DeviceGrant)
        self.assertEqual(admin.role, "admin")
        self.assertTrue(admin.is_caller)
        self.assertFalse(admin.is_revoked)
        self.assertEqual(admin.device_name, "Laptop A")
        revoked = grants[1]
        self.assertEqual(revoked.device_name, None)
        self.assertTrue(revoked.is_revoked)
        self.assertFalse(revoked.is_caller)
        self.assertEqual(revoked.revoked_at, "2026-05-10T11:00:00.000Z")
        # Caller threaded auth correctly through to the relay.
        self.assertEqual(relay.list_calls, [(VAULT_ID, VAULT_ACCESS_SECRET)])

    def test_missing_grants_array_raises_typed_error(self) -> None:
        relay = _FakeRelay()
        relay.list_response = {"vault_id": VAULT_ID}  # no 'grants' key

        with self.assertRaises(DeviceGrantsError):
            list_device_grants(relay, VAULT_ID, VAULT_ACCESS_SECRET)

    def test_401_translates_to_auth_error(self) -> None:
        relay = _FakeRelay()
        relay.list_error = _server_error(
            status=401, code="vault_auth_failed", message="auth expired",
        )
        with self.assertRaises(DeviceGrantsAuthError):
            list_device_grants(relay, VAULT_ID, VAULT_ACCESS_SECRET)

    def test_403_translates_to_auth_error(self) -> None:
        relay = _FakeRelay()
        relay.list_error = _server_error(
            status=403, code="vault_forbidden", message="role required",
        )
        with self.assertRaises(DeviceGrantsAuthError):
            list_device_grants(relay, VAULT_ID, VAULT_ACCESS_SECRET)


class RevokeDeviceGrantTests(unittest.TestCase):
    def test_revoke_success_parses_typed_result(self) -> None:
        relay = _FakeRelay()
        relay.revoke_response = {
            "vault_id": VAULT_ID,
            "device_id": TARGET_DEVICE,
            "revoked_at": "2026-05-18T18:00:00.000Z",
            "already_revoked": False,
        }

        out = revoke_device_grant(
            relay, VAULT_ID, VAULT_ACCESS_SECRET, TARGET_DEVICE,
        )

        self.assertIsInstance(out, RevokeResult)
        self.assertEqual(out.device_id, TARGET_DEVICE)
        self.assertFalse(out.already_revoked)
        self.assertEqual(out.revoked_at, "2026-05-18T18:00:00.000Z")
        self.assertEqual(
            relay.revoke_calls,
            [(VAULT_ID, VAULT_ACCESS_SECRET, TARGET_DEVICE)],
        )

    def test_revoke_success_emits_audit_log_line(self) -> None:
        # Phase 3 collateral fix per docs/plans/activity-timeline.md:
        # revoke_device_grant had no log emission, so the consumer
        # side's ``vault.revoke.completed`` row would never appear in
        # the Activity tab.
        import logging
        relay = _FakeRelay()
        relay.revoke_response = {
            "vault_id": VAULT_ID, "device_id": TARGET_DEVICE,
            "revoked_at": "2026-05-19T18:00:00.000Z",
            "already_revoked": False,
        }
        with self.assertLogs(
            "src.vault.grant.client", level=logging.INFO,
        ) as captured:
            revoke_device_grant(
                relay, VAULT_ID, VAULT_ACCESS_SECRET, TARGET_DEVICE,
            )
        joined = "\n".join(captured.output)
        self.assertIn("vault.revoke.completed", joined)
        self.assertIn("already_revoked=false", joined)

    def test_revoke_idempotent_already_revoked(self) -> None:
        relay = _FakeRelay()
        relay.revoke_response = {
            "vault_id": VAULT_ID,
            "device_id": TARGET_DEVICE,
            "revoked_at": "2026-05-10T11:00:00.000Z",
            "already_revoked": True,
        }
        out = revoke_device_grant(
            relay, VAULT_ID, VAULT_ACCESS_SECRET, TARGET_DEVICE,
        )
        self.assertTrue(out.already_revoked)

    def test_self_revoke_translates_to_typed_error(self) -> None:
        relay = _FakeRelay()
        relay.revoke_error = _server_error(
            status=400, code="vault_invalid_request",
            message="admin cannot revoke their own grant; transfer admin role first",
        )
        with self.assertRaises(CannotRevokeSelfError):
            revoke_device_grant(
                relay, VAULT_ID, VAULT_ACCESS_SECRET, CALLER_DEVICE,
            )

    def test_unknown_device_translates_to_typed_error(self) -> None:
        relay = _FakeRelay()
        relay.revoke_error = _server_error(
            status=404, code="vault_join_request_state",
            message="no grant for device ...",
        )
        with self.assertRaises(DeviceGrantNotFoundError):
            revoke_device_grant(
                relay, VAULT_ID, VAULT_ACCESS_SECRET, TARGET_DEVICE,
            )

    def test_not_admin_translates_to_auth_error(self) -> None:
        relay = _FakeRelay()
        relay.revoke_error = _server_error(
            status=403, code="vault_forbidden",
            message="admin role required",
        )
        with self.assertRaises(DeviceGrantsAuthError):
            revoke_device_grant(
                relay, VAULT_ID, VAULT_ACCESS_SECRET, TARGET_DEVICE,
            )

    def test_generic_400_propagates_relay_error_untyped(self) -> None:
        """A 400 that isn't the self-revoke case (e.g. malformed id)
        should not be silently re-typed; the tab surfaces it as a
        generic error.
        """
        relay = _FakeRelay()
        relay.revoke_error = _server_error(
            status=400, code="vault_invalid_request",
            message="device_id must be 32 lowercase hex chars",
        )
        with self.assertRaises(VaultRelayError):
            revoke_device_grant(
                relay, VAULT_ID, VAULT_ACCESS_SECRET, "bad-id",
            )


if __name__ == "__main__":
    unittest.main()
