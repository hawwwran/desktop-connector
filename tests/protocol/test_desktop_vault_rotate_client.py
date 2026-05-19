"""§5.H3 — typed client around the access-secret rotation endpoint."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.grant.access_rotation import (  # noqa: E402
    _compute_secret_hex_for_tests,
    secret_to_wire_hash,
)
from src.vault.grant.rotate_client import (  # noqa: E402
    RotationAuthError,
    RotationError,
    RotationNotFoundError,
    RotationRateLimitedError,
    RotationResponse,
    rotate_access_secret,
)
from src.vault.relay_errors import VaultRelayError  # noqa: E402


VAULT_ID = "aaaabbbbccccddddeeeeffff00001111"
OLD_SECRET = "vas_old_secret_value_for_test"


class _FakeRelay:
    def __init__(self) -> None:
        self.response: dict | None = None
        self.error: Exception | None = None
        self.calls: list[tuple] = []

    def rotate_access_secret(
        self, vault_id, vault_access_secret,
        *, new_vault_access_token_hash,
        triggered_by_revoke_grant_id=None,
    ):
        self.calls.append((
            vault_id, vault_access_secret,
            new_vault_access_token_hash,
            triggered_by_revoke_grant_id,
        ))
        if self.error is not None:
            raise self.error
        return self.response


def _err(*, status: int, code: str, message: str = "") -> VaultRelayError:
    return VaultRelayError(
        {"code": code, "message": message, "details": {}},
        status_code=status,
    )


class RotateClientTests(unittest.TestCase):
    def test_success_parses_typed_response(self) -> None:
        relay = _FakeRelay()
        relay.response = {
            "vault_id": "AAAA-BBBB-CCCC",
            "rotated_at": "2026-05-18T18:00:00.000Z",
        }
        out = rotate_access_secret(relay, VAULT_ID, OLD_SECRET, "vas_new_secret")
        self.assertIsInstance(out, RotationResponse)
        self.assertEqual(out.vault_id_dashed, "AAAA-BBBB-CCCC")
        self.assertEqual(out.rotated_at, "2026-05-18T18:00:00.000Z")

    def test_posts_sha256_of_new_secret_not_plaintext(self) -> None:
        relay = _FakeRelay()
        relay.response = {
            "vault_id": "AAAA-BBBB-CCCC",
            "rotated_at": "2026-05-18T18:00:00.000Z",
        }
        new_secret = "vas_new_secret_for_test"
        rotate_access_secret(relay, VAULT_ID, OLD_SECRET, new_secret)

        sent_hash = relay.calls[0][2]
        expected_hex = _compute_secret_hex_for_tests(new_secret)
        self.assertEqual(sent_hash.hex(), expected_hex)
        # Ensure the plaintext new_secret isn't anywhere in the
        # outgoing hash bytes (defense-in-depth).
        self.assertNotIn(new_secret.encode("utf-8"), sent_hash)

    def test_success_emits_audit_log_line(self) -> None:
        # Phase 3 collateral fix per docs/plans/activity-timeline.md:
        # rotate_access_secret had no log emission, so the consumer
        # side's ``vault.rotation.completed`` row would never appear in
        # the Activity tab.
        import logging
        relay = _FakeRelay()
        relay.response = {
            "vault_id": "AAAA-BBBB-CCCC",
            "rotated_at": "2026-05-19T18:00:00.000Z",
        }
        with self.assertLogs(
            "src.vault.grant.rotate_client", level=logging.INFO,
        ) as captured:
            rotate_access_secret(relay, VAULT_ID, OLD_SECRET, "vas_new")
        joined = "\n".join(captured.output)
        self.assertIn("vault.rotation.completed", joined)
        self.assertIn("AAAA-BBBB-CCCC", joined)

    def test_triggered_by_revoke_threaded_through(self) -> None:
        relay = _FakeRelay()
        relay.response = {
            "vault_id": "AAAA-BBBB-CCCC",
            "rotated_at": "2026-05-18T18:00:00.000Z",
        }
        rotate_access_secret(
            relay, VAULT_ID, OLD_SECRET, "new",
            triggered_by_revoke_grant_id="gr_v1_abc",
        )
        self.assertEqual(relay.calls[0][3], "gr_v1_abc")

    def test_403_maps_to_auth_error(self) -> None:
        relay = _FakeRelay()
        relay.error = _err(
            status=403, code="vault_forbidden",
            message="admin role required",
        )
        with self.assertRaises(RotationAuthError):
            rotate_access_secret(relay, VAULT_ID, OLD_SECRET, "new")

    def test_404_maps_to_not_found(self) -> None:
        relay = _FakeRelay()
        relay.error = _err(
            status=404, code="vault_not_found",
            message="Vault not found",
        )
        with self.assertRaises(RotationNotFoundError):
            rotate_access_secret(relay, VAULT_ID, OLD_SECRET, "new")

    def test_429_maps_to_rate_limited(self) -> None:
        relay = _FakeRelay()
        relay.error = _err(
            status=429, code="vault_rate_limited",
            message="cooldown active",
        )
        with self.assertRaises(RotationRateLimitedError):
            rotate_access_secret(relay, VAULT_ID, OLD_SECRET, "new")

    def test_other_errors_propagate_as_relay_error(self) -> None:
        relay = _FakeRelay()
        relay.error = _err(
            status=500, code="internal_error", message="boom",
        )
        with self.assertRaises(VaultRelayError):
            rotate_access_secret(relay, VAULT_ID, OLD_SECRET, "new")


if __name__ == "__main__":
    unittest.main()
