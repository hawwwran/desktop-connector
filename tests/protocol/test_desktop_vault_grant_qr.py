"""T13.2 — QR-encoded join URL: build + parse."""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_grant_qr import (  # noqa: E402
    DEFAULT_TTL_SECONDS, SCHEME, VaultGrantQRError, VaultJoinUrl,
    derive_verification_code,
    make_join_url, parse_join_url,
)


PUBKEY = bytes(range(32))
JR_ID = "jr_v1_aabbccdd2345abcd234567cd"
VAULT_ID_DASHED = "ABCD-2345-WXYZ"
VAULT_ID_UNDASHED = "ABCD2345WXYZ"
RELAY = "https://relay.example.com:4441"


class BuildJoinUrlTests(unittest.TestCase):
    def test_round_trip_with_explicit_expiry(self) -> None:
        url = make_join_url(
            relay_url=RELAY,
            vault_id=VAULT_ID_DASHED,
            join_request_id=JR_ID,
            ephemeral_pubkey=PUBKEY,
            expires_at=2_000_000_000,
        )
        self.assertTrue(url.startswith(f"{SCHEME}://relay.example.com:4441/"))
        self.assertIn(JR_ID, url)
        self.assertIn("expires=2000000000", url)

        parsed = parse_join_url(url)
        self.assertEqual(parsed.vault_id_dashed, VAULT_ID_DASHED)
        self.assertEqual(parsed.vault_id_undashed, VAULT_ID_UNDASHED)
        self.assertEqual(parsed.join_request_id, JR_ID)
        self.assertEqual(parsed.ephemeral_pubkey, PUBKEY)
        self.assertEqual(parsed.expires_at, 2_000_000_000)

    def test_default_ttl_is_15_minutes(self) -> None:
        self.assertEqual(DEFAULT_TTL_SECONDS, 15 * 60)
        url = make_join_url(
            relay_url=RELAY, vault_id=VAULT_ID_UNDASHED,
            join_request_id=JR_ID, ephemeral_pubkey=PUBKEY,
            now=1_000_000.0,
        )
        parsed = parse_join_url(url)
        self.assertEqual(parsed.expires_at, 1_000_000 + 15 * 60)

    def test_undashed_vault_id_normalises_to_dashed(self) -> None:
        url = make_join_url(
            relay_url=RELAY, vault_id=VAULT_ID_UNDASHED,
            join_request_id=JR_ID, ephemeral_pubkey=PUBKEY,
            expires_at=1234,
        )
        self.assertIn(VAULT_ID_DASHED, url)

    def test_lowercase_vault_id_is_normalised(self) -> None:
        url = make_join_url(
            relay_url=RELAY, vault_id="abcd-2345-wxyz",
            join_request_id=JR_ID, ephemeral_pubkey=PUBKEY,
            expires_at=1234,
        )
        self.assertIn(VAULT_ID_DASHED, url)

    def test_pubkey_must_be_32_bytes(self) -> None:
        with self.assertRaises(VaultGrantQRError):
            make_join_url(
                relay_url=RELAY, vault_id=VAULT_ID_DASHED,
                join_request_id=JR_ID, ephemeral_pubkey=b"\x00" * 31,
                expires_at=1234,
            )

    def test_relay_must_be_http_or_https(self) -> None:
        with self.assertRaises(VaultGrantQRError):
            make_join_url(
                relay_url="ftp://nope/",
                vault_id=VAULT_ID_DASHED,
                join_request_id=JR_ID, ephemeral_pubkey=PUBKEY,
                expires_at=1234,
            )

    def test_join_request_id_must_match_format(self) -> None:
        with self.assertRaises(VaultGrantQRError):
            make_join_url(
                relay_url=RELAY, vault_id=VAULT_ID_DASHED,
                join_request_id="bad-id",
                ephemeral_pubkey=PUBKEY, expires_at=1234,
            )


class ParseJoinUrlTests(unittest.TestCase):
    def test_rejects_wrong_scheme(self) -> None:
        with self.assertRaises(VaultGrantQRError):
            parse_join_url(
                "https://relay.example.com/ABCD-2345-WXYZ/"
                + JR_ID + "/AAAA?expires=1234"
            )

    def test_rejects_missing_pubkey_segment(self) -> None:
        with self.assertRaises(VaultGrantQRError):
            parse_join_url(f"vault://relay/{VAULT_ID_DASHED}/{JR_ID}?expires=1234")

    def test_rejects_missing_expires(self) -> None:
        # Build a valid URL then strip the query string.
        url = make_join_url(
            relay_url=RELAY, vault_id=VAULT_ID_DASHED,
            join_request_id=JR_ID, ephemeral_pubkey=PUBKEY,
            expires_at=1234,
        )
        # Drop the ?expires=... bit.
        bare = url.split("?", 1)[0]
        with self.assertRaises(VaultGrantQRError):
            parse_join_url(bare)

    def test_rejects_pubkey_decoded_to_wrong_length(self) -> None:
        # 16-byte pubkey: still valid base64 but wrong length.
        import base64
        bad_b64 = base64.urlsafe_b64encode(b"\x00" * 16).rstrip(b"=").decode()
        with self.assertRaises(VaultGrantQRError):
            parse_join_url(
                f"vault://relay/{VAULT_ID_DASHED}/{JR_ID}/{bad_b64}?expires=1234"
            )

    def test_is_expired_uses_supplied_now(self) -> None:
        url = make_join_url(
            relay_url=RELAY, vault_id=VAULT_ID_DASHED,
            join_request_id=JR_ID, ephemeral_pubkey=PUBKEY,
            expires_at=1_000,
        )
        parsed = parse_join_url(url)
        self.assertTrue(parsed.is_expired(now=2_000))
        self.assertFalse(parsed.is_expired(now=999))

    def test_recovers_relay_url_as_https(self) -> None:
        url = make_join_url(
            relay_url="http://localhost:4441",
            vault_id=VAULT_ID_DASHED, join_request_id=JR_ID,
            ephemeral_pubkey=PUBKEY, expires_at=1_000,
        )
        parsed = parse_join_url(url)
        # The QR-encoded form drops the original scheme; we always
        # recover https since vault joins should never run over plain
        # http in production. Tests that need http can keep using the
        # original relay_url they passed to make_join_url.
        self.assertEqual(parsed.relay_url, "https://localhost:4441")


class VerificationCodeTests(unittest.TestCase):
    """T13.3 — both sides derive the same 6-digit code from both pubkeys."""

    def test_returns_six_digit_string(self) -> None:
        code = derive_verification_code(b"\x01" * 32, b"\x02" * 32)
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(len(code), 6)

    def test_order_independence(self) -> None:
        a, b = bytes(range(32)), bytes(range(31, -1, -1))
        self.assertEqual(
            derive_verification_code(a, b),
            derive_verification_code(b, a),
            "verification code must not depend on argument order",
        )

    def test_different_pubkeys_produce_different_codes(self) -> None:
        c1 = derive_verification_code(b"\x00" * 32, b"\x01" * 32)
        c2 = derive_verification_code(b"\x00" * 32, b"\x02" * 32)
        self.assertNotEqual(c1, c2)

    def test_identical_pubkeys_produce_stable_code(self) -> None:
        c1 = derive_verification_code(b"\x05" * 32, b"\x09" * 32)
        c2 = derive_verification_code(b"\x05" * 32, b"\x09" * 32)
        self.assertEqual(c1, c2)

    def test_invalid_pubkey_length_raises(self) -> None:
        with self.assertRaises(VaultGrantQRError):
            derive_verification_code(b"\x00" * 31, b"\x00" * 32)
        with self.assertRaises(VaultGrantQRError):
            derive_verification_code(b"\x00" * 32, b"\x00" * 33)


class GrantExchangeIntegrationTests(unittest.TestCase):
    """T13.3 acceptance: the QR + claim cycle ends with matching codes."""

    def test_admin_and_claimant_compute_matching_code(self) -> None:
        """Simulates the two halves of the grant exchange.

        Admin generates a join request with its ephemeral pubkey,
        encodes it in a QR, and shows the resulting verification code
        on screen. Claimant scans the QR, generates its own ephemeral
        pubkey, posts a claim, and shows the same verification code.
        Both sides must match — that's the whole point of the manual
        confirmation step.
        """
        try:
            from nacl.public import PrivateKey
        except ImportError:
            self.skipTest("PyNaCl not available")

        admin_priv = PrivateKey.generate()
        admin_pk = bytes(admin_priv.public_key)
        url = make_join_url(
            relay_url=RELAY,
            vault_id=VAULT_ID_DASHED,
            join_request_id=JR_ID,
            ephemeral_pubkey=admin_pk,
            expires_at=2_000_000_000,
        )

        # Claimant side: parse, generate keypair, derive code.
        parsed = parse_join_url(url)
        claimant_priv = PrivateKey.generate()
        claimant_pk = bytes(claimant_priv.public_key)
        claimant_code = derive_verification_code(
            parsed.ephemeral_pubkey, claimant_pk,
        )

        # Admin side: receives claimant_pk via the relay's poll
        # response, derives the same code.
        admin_code = derive_verification_code(admin_pk, claimant_pk)

        self.assertEqual(claimant_code, admin_code)


if __name__ == "__main__":
    unittest.main()
