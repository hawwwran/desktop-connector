"""§5.C2 — typed client around the join-request HTTP surface."""

from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.grant.join_client import (  # noqa: E402
    JoinRequest,
    JoinRequestAuthError,
    JoinRequestError,
    JoinRequestNotFoundError,
    JoinRequestRateLimitedError,
    JoinRequestStateError,
    approve_join_request,
    claim_join_request,
    create_join_request,
    get_join_request,
    reject_join_request,
)
from src.vault.relay_errors import VaultRelayError  # noqa: E402


VAULT_ID = "aaaabbbbccccddddeeeeffff00001111"
VAULT_ACCESS_SECRET = "vas_test_value"
JOIN_REQUEST_ID = "jr_v1_abcdefghijklmnopqrstuvwx"
ADMIN_PUB = b"\x01" * 32
CLAIMANT_PUB = b"\x02" * 32
CLAIMANT_DEVICE = "1" * 32


class _FakeRelay:
    """Scripted stand-in for ``VaultHttpRelay``."""

    def __init__(self) -> None:
        self.create_response: dict | None = None
        self.get_response: dict | None = None
        self.claim_response: dict | None = None
        self.approve_response: dict | None = None
        self.reject_response = None
        self.error_to_raise: Exception | None = None
        self.calls: list[tuple[str, tuple, dict]] = []

    def create_join_request(self, vault_id, vault_access_secret, *, ephemeral_admin_pubkey):
        self.calls.append(("create", (vault_id, vault_access_secret), {"pubkey": ephemeral_admin_pubkey}))
        if self.error_to_raise:
            raise self.error_to_raise
        return self.create_response

    def get_join_request(self, vault_id, req_id, *, vault_access_secret=None):
        self.calls.append(("get", (vault_id, req_id), {"vault_access_secret": vault_access_secret}))
        if self.error_to_raise:
            raise self.error_to_raise
        return self.get_response

    def claim_join_request(self, vault_id, req_id, *, claimant_pubkey, device_name):
        self.calls.append(("claim", (vault_id, req_id), {"pubkey": claimant_pubkey, "name": device_name}))
        if self.error_to_raise:
            raise self.error_to_raise
        return self.claim_response

    def approve_join_request(self, vault_id, vault_access_secret, req_id, *, approved_role, wrapped_vault_grant):
        self.calls.append((
            "approve", (vault_id, vault_access_secret, req_id),
            {"role": approved_role, "wrapped": wrapped_vault_grant},
        ))
        if self.error_to_raise:
            raise self.error_to_raise
        return self.approve_response

    def reject_join_request(self, vault_id, vault_access_secret, req_id):
        self.calls.append(("reject", (vault_id, vault_access_secret, req_id), {}))
        if self.error_to_raise:
            raise self.error_to_raise


def _err(*, status: int, code: str, message: str = "") -> VaultRelayError:
    return VaultRelayError(
        {"code": code, "message": message, "details": {}},
        status_code=status,
    )


def _pending_payload(**overrides) -> dict:
    base = {
        "join_request_id": JOIN_REQUEST_ID,
        "vault_id": "AAAA-BBBB-CCCC",
        "state": "pending",
        "ephemeral_admin_pubkey": base64.b64encode(ADMIN_PUB).decode("ascii"),
        "expires_at": "2026-05-18T18:00:00.000Z",
        "created_at": "2026-05-18T17:45:00.000Z",
        "claimed_at": None,
        "approved_at": None,
        "rejected_at": None,
        "claimant_device_id": None,
        "claimant_pubkey": None,
        "device_name": None,
        "approved_role": None,
        "granted_by_device_id": None,
        "wrapped_vault_grant": None,
    }
    base.update(overrides)
    return base


class CreateJoinRequestTests(unittest.TestCase):
    def test_parses_pending_row_into_dataclass(self) -> None:
        relay = _FakeRelay()
        relay.create_response = _pending_payload()
        jr = create_join_request(
            relay, VAULT_ID, VAULT_ACCESS_SECRET,
            ephemeral_admin_pubkey=ADMIN_PUB,
        )
        self.assertIsInstance(jr, JoinRequest)
        self.assertEqual(jr.state, "pending")
        self.assertEqual(jr.ephemeral_admin_pubkey, ADMIN_PUB)
        self.assertIsNone(jr.claimant_pubkey)
        self.assertIsNone(jr.wrapped_vault_grant)
        # Caller threaded the admin pubkey through.
        self.assertEqual(relay.calls[0][2]["pubkey"], ADMIN_PUB)

    def test_429_maps_to_rate_limited(self) -> None:
        relay = _FakeRelay()
        relay.error_to_raise = _err(
            status=429, code="vault_rate_limited",
            message="too many pending join-requests for this vault",
        )
        with self.assertRaises(JoinRequestRateLimitedError):
            create_join_request(
                relay, VAULT_ID, VAULT_ACCESS_SECRET,
                ephemeral_admin_pubkey=ADMIN_PUB,
            )

    def test_403_maps_to_auth_error(self) -> None:
        relay = _FakeRelay()
        relay.error_to_raise = _err(status=403, code="vault_forbidden")
        with self.assertRaises(JoinRequestAuthError):
            create_join_request(
                relay, VAULT_ID, VAULT_ACCESS_SECRET,
                ephemeral_admin_pubkey=ADMIN_PUB,
            )


class GetJoinRequestTests(unittest.TestCase):
    def test_admin_poll_passes_vault_secret(self) -> None:
        relay = _FakeRelay()
        relay.get_response = _pending_payload(state="claimed")
        get_join_request(
            relay, VAULT_ID, JOIN_REQUEST_ID,
            vault_access_secret=VAULT_ACCESS_SECRET,
        )
        self.assertEqual(
            relay.calls[0][2]["vault_access_secret"], VAULT_ACCESS_SECRET,
        )

    def test_claimant_poll_omits_vault_secret(self) -> None:
        relay = _FakeRelay()
        relay.get_response = _pending_payload(state="claimed")
        get_join_request(relay, VAULT_ID, JOIN_REQUEST_ID)
        self.assertIsNone(relay.calls[0][2]["vault_access_secret"])

    def test_approved_state_carries_wrapped_grant(self) -> None:
        relay = _FakeRelay()
        wrapped_bytes = b"wrapped envelope contents..."
        relay.get_response = _pending_payload(
            state="approved",
            claimant_device_id=CLAIMANT_DEVICE,
            claimant_pubkey=base64.b64encode(CLAIMANT_PUB).decode("ascii"),
            approved_role="sync",
            approved_at="2026-05-18T17:55:00.000Z",
            wrapped_vault_grant=base64.b64encode(wrapped_bytes).decode("ascii"),
        )
        jr = get_join_request(
            relay, VAULT_ID, JOIN_REQUEST_ID,
            vault_access_secret=VAULT_ACCESS_SECRET,
        )
        self.assertEqual(jr.state, "approved")
        self.assertEqual(jr.wrapped_vault_grant, wrapped_bytes)
        self.assertEqual(jr.claimant_pubkey, CLAIMANT_PUB)
        self.assertEqual(jr.approved_role, "sync")

    def test_404_maps_to_not_found(self) -> None:
        relay = _FakeRelay()
        relay.error_to_raise = _err(
            status=404, code="vault_join_request_state",
            message="unknown join-request",
        )
        with self.assertRaises(JoinRequestNotFoundError):
            get_join_request(relay, VAULT_ID, JOIN_REQUEST_ID)


class ClaimJoinRequestTests(unittest.TestCase):
    def test_claim_returns_claimed_row(self) -> None:
        relay = _FakeRelay()
        relay.claim_response = _pending_payload(
            state="claimed",
            claimant_device_id=CLAIMANT_DEVICE,
            claimant_pubkey=base64.b64encode(CLAIMANT_PUB).decode("ascii"),
            device_name="MX-laptop",
            claimed_at="2026-05-18T17:52:00.000Z",
        )
        jr = claim_join_request(
            relay, VAULT_ID, JOIN_REQUEST_ID,
            claimant_pubkey=CLAIMANT_PUB, device_name="MX-laptop",
        )
        self.assertEqual(jr.state, "claimed")
        self.assertEqual(jr.claimant_pubkey, CLAIMANT_PUB)
        self.assertEqual(jr.device_name, "MX-laptop")

    def test_409_maps_to_state_error(self) -> None:
        relay = _FakeRelay()
        relay.error_to_raise = _err(
            status=409, code="vault_join_request_state",
            message="join-request not in pending state: claimed",
        )
        with self.assertRaises(JoinRequestStateError):
            claim_join_request(
                relay, VAULT_ID, JOIN_REQUEST_ID,
                claimant_pubkey=CLAIMANT_PUB, device_name="x",
            )


class ApproveJoinRequestTests(unittest.TestCase):
    def test_approve_passes_wrapped_grant(self) -> None:
        relay = _FakeRelay()
        relay.approve_response = _pending_payload(
            state="approved", approved_role="sync",
        )
        wrapped = b"wrap" + b"\xaa" * 200
        approve_join_request(
            relay, VAULT_ID, VAULT_ACCESS_SECRET, JOIN_REQUEST_ID,
            approved_role="sync", wrapped_vault_grant=wrapped,
        )
        self.assertEqual(relay.calls[0][2]["role"], "sync")
        self.assertEqual(relay.calls[0][2]["wrapped"], wrapped)

    def test_403_maps_to_auth_error(self) -> None:
        relay = _FakeRelay()
        relay.error_to_raise = _err(status=403, code="vault_forbidden")
        with self.assertRaises(JoinRequestAuthError):
            approve_join_request(
                relay, VAULT_ID, VAULT_ACCESS_SECRET, JOIN_REQUEST_ID,
                approved_role="sync", wrapped_vault_grant=b"x" * 200,
            )


class RejectJoinRequestTests(unittest.TestCase):
    def test_reject_is_void(self) -> None:
        relay = _FakeRelay()
        reject_join_request(relay, VAULT_ID, VAULT_ACCESS_SECRET, JOIN_REQUEST_ID)
        self.assertEqual(relay.calls[0][0], "reject")

    def test_reject_404_maps_to_not_found(self) -> None:
        relay = _FakeRelay()
        relay.error_to_raise = _err(
            status=404, code="vault_join_request_state",
        )
        with self.assertRaises(JoinRequestNotFoundError):
            reject_join_request(
                relay, VAULT_ID, VAULT_ACCESS_SECRET, JOIN_REQUEST_ID,
            )


class ParsingEdgeCaseTests(unittest.TestCase):
    def test_malformed_ephemeral_pubkey_raises(self) -> None:
        relay = _FakeRelay()
        relay.get_response = _pending_payload(
            ephemeral_admin_pubkey=base64.b64encode(b"\x00" * 16).decode("ascii"),
        )
        with self.assertRaises(JoinRequestError):
            get_join_request(relay, VAULT_ID, JOIN_REQUEST_ID)


if __name__ == "__main__":
    unittest.main()
