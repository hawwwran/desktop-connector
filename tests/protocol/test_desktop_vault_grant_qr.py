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
    derive_shared_secret,
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
    """Spec §13.4 — derive a NNN-NNN code from the X25519 shared secret."""

    def test_returns_dashed_code(self) -> None:
        code = derive_verification_code(b"\x01" * 32)
        self.assertRegex(code, r"^\d{3}-\d{3}$")
        self.assertEqual(len(code), 7)

    def test_spec_pinned_vector(self) -> None:
        # Spec-pinned: HMAC-SHA256(key=32 zero bytes, msg=b"dc-vault-v1/qr-verification")
        # first 3 bytes interpreted big-endian then mod 1_000_000.
        code = derive_verification_code(b"\x00" * 32)
        import hashlib
        import hmac
        digest = hmac.new(
            key=b"\x00" * 32,
            msg=b"dc-vault-v1/qr-verification",
            digestmod=hashlib.sha256,
        ).digest()
        expected_int = int.from_bytes(digest[:3], "big") % 1_000_000
        expected = f"{expected_int:06d}"
        self.assertEqual(code, f"{expected[:3]}-{expected[3:]}")

    def test_different_secrets_produce_different_codes(self) -> None:
        c1 = derive_verification_code(b"\x00" * 32)
        c2 = derive_verification_code(b"\x01" * 32)
        self.assertNotEqual(c1, c2)

    def test_identical_secret_produces_stable_code(self) -> None:
        c1 = derive_verification_code(b"\x05" * 32)
        c2 = derive_verification_code(b"\x05" * 32)
        self.assertEqual(c1, c2)

    def test_invalid_secret_length_raises(self) -> None:
        with self.assertRaises(VaultGrantQRError):
            derive_verification_code(b"\x00" * 31)
        with self.assertRaises(VaultGrantQRError):
            derive_verification_code(b"\x00" * 33)


class GrantExchangeIntegrationTests(unittest.TestCase):
    """Spec §13.3/13.4 acceptance: X25519 commutativity → both sides match."""

    def test_admin_and_claimant_compute_matching_code(self) -> None:
        """Admin and claimant compute identical codes via X25519 commutativity."""
        try:
            from nacl.public import PrivateKey
        except ImportError:
            self.skipTest("PyNaCl not available")

        admin_priv_obj = PrivateKey.generate()
        admin_priv = bytes(admin_priv_obj.encode())
        admin_pk = bytes(admin_priv_obj.public_key)
        url = make_join_url(
            relay_url=RELAY,
            vault_id=VAULT_ID_DASHED,
            join_request_id=JR_ID,
            ephemeral_pubkey=admin_pk,
            expires_at=2_000_000_000,
        )
        parsed = parse_join_url(url)

        claimant_priv_obj = PrivateKey.generate()
        claimant_priv = bytes(claimant_priv_obj.encode())
        claimant_pk = bytes(claimant_priv_obj.public_key)

        # Each side computes the X25519 shared secret using its own
        # private key + the other side's public key. Commutativity
        # guarantees the two scalars are byte-identical.
        admin_secret = derive_shared_secret(admin_priv, claimant_pk)
        claimant_secret = derive_shared_secret(claimant_priv, parsed.ephemeral_pubkey)
        self.assertEqual(admin_secret, claimant_secret)

        admin_code = derive_verification_code(admin_secret)
        claimant_code = derive_verification_code(claimant_secret)
        self.assertEqual(admin_code, claimant_code)
        self.assertRegex(admin_code, r"^\d{3}-\d{3}$")


if __name__ == "__main__":
    unittest.main()
