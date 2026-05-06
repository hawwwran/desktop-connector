"""T13.4 — wrap / unwrap the vault grant payload via X25519 + XChaCha20-Poly1305.

Verifies the spec-correct primitives in ``desktop/src/vault_grant_wrap.py``
match the formats §13.5 / §14 envelope, AAD, and plaintext schema.
"""

from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_grant_wrap import (  # noqa: E402
    GrantPayload, GrantWrapError,
    unwrap_grant_for_claimant, wrap_grant_for_claimant,
)


VAULT_ID = "ABCD2345WXYZ"
GRANT_ID = "gr_v1_aaaaaaaaaaaaaaaaaaaaaaaa"  # gr_v1_ + 24 base32 lowercase
ADMIN_DEVICE_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
CLAIMANT_DEVICE_ID = "ffeeddccbbaa99887766554433221100"


def _keypair():
    from nacl.public import PrivateKey
    priv = PrivateKey.generate()
    return bytes(priv), bytes(priv.public_key)


def _sample_payload(*, role: str = "sync") -> GrantPayload:
    return GrantPayload(
        vault_id=VAULT_ID,
        grant_id=GRANT_ID,
        claimant_device_id=CLAIMANT_DEVICE_ID,
        approved_role=role,
        granted_by_device_id=ADMIN_DEVICE_ID,
        granted_at="2026-05-03T12:00:00.000Z",
        vault_master_key_b64=base64.b64encode(b"\x10" * 32).decode("ascii"),
        vault_access_secret="super-high-entropy-secret",
    )


class WrapUnwrapTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.admin_priv, self.admin_pub = _keypair()
            self.claimant_priv, self.claimant_pub = _keypair()
        except ImportError:
            self.skipTest("PyNaCl not available")

    def test_wrap_unwrap_round_trip(self) -> None:
        payload = _sample_payload()
        envelope = wrap_grant_for_claimant(
            payload=payload,
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
        )
        recovered = unwrap_grant_for_claimant(
            envelope=envelope,
            claimant_priv=self.claimant_priv,
            admin_pub=self.admin_pub,
            expected_vault_id=VAULT_ID,
            expected_claimant_device_id=CLAIMANT_DEVICE_ID,
        )
        self.assertEqual(recovered, payload)

    def test_envelope_carries_99_byte_prefix(self) -> None:
        # formats §14.1: 1 + 12 + 30 + 32 + 24 = 99 bytes deterministic prefix.
        env = wrap_grant_for_claimant(
            payload=_sample_payload(),
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
        )
        self.assertGreater(len(env), 99)
        self.assertEqual(env[0], 0x01)                        # format_version
        self.assertEqual(env[1:13].decode("ascii"), VAULT_ID)
        self.assertEqual(env[13:43].decode("ascii"), GRANT_ID)
        self.assertEqual(env[43:75], self.claimant_pub)

    def test_wrap_uses_random_nonce_per_call(self) -> None:
        payload = _sample_payload()
        e1 = wrap_grant_for_claimant(
            payload=payload, admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
        )
        e2 = wrap_grant_for_claimant(
            payload=payload, admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
        )
        self.assertNotEqual(e1, e2)
        # But both decrypt to the same payload.
        self.assertEqual(
            unwrap_grant_for_claimant(
                envelope=e1, claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                expected_vault_id=VAULT_ID,
                expected_claimant_device_id=CLAIMANT_DEVICE_ID,
            ),
            unwrap_grant_for_claimant(
                envelope=e2, claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                expected_vault_id=VAULT_ID,
                expected_claimant_device_id=CLAIMANT_DEVICE_ID,
            ),
        )

    def test_wrong_claimant_priv_fails_decrypt(self) -> None:
        envelope = wrap_grant_for_claimant(
            payload=_sample_payload(),
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
        )
        attacker_priv, _ = _keypair()
        with self.assertRaises(GrantWrapError):
            unwrap_grant_for_claimant(
                envelope=envelope, claimant_priv=attacker_priv,
                admin_pub=self.admin_pub,
                expected_vault_id=VAULT_ID,
                expected_claimant_device_id=CLAIMANT_DEVICE_ID,
            )

    def test_wrong_claimant_device_id_fails_decrypt(self) -> None:
        """Claimant device id is in the AAD; a mismatched id breaks the tag."""
        envelope = wrap_grant_for_claimant(
            payload=_sample_payload(),
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
        )
        with self.assertRaises(GrantWrapError):
            unwrap_grant_for_claimant(
                envelope=envelope, claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                expected_vault_id=VAULT_ID,
                expected_claimant_device_id="ffffffffffffffffffffffffffffffff",
            )

    def test_wrong_vault_id_rejected(self) -> None:
        envelope = wrap_grant_for_claimant(
            payload=_sample_payload(),
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
        )
        with self.assertRaises(GrantWrapError):
            unwrap_grant_for_claimant(
                envelope=envelope, claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                expected_vault_id="WXYZ2345ABCD",
                expected_claimant_device_id=CLAIMANT_DEVICE_ID,
            )

    def test_truncated_envelope_rejected(self) -> None:
        with self.assertRaises(GrantWrapError):
            unwrap_grant_for_claimant(
                envelope=b"\x00" * 16,  # < 99-byte prefix + tag
                claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                expected_vault_id=VAULT_ID,
                expected_claimant_device_id=CLAIMANT_DEVICE_ID,
            )

    def test_invalid_key_lengths_rejected(self) -> None:
        with self.assertRaises(GrantWrapError):
            wrap_grant_for_claimant(
                payload=_sample_payload(),
                admin_priv=b"\x00" * 31,
                claimant_pub=self.claimant_pub,
            )
        with self.assertRaises(GrantWrapError):
            wrap_grant_for_claimant(
                payload=_sample_payload(),
                admin_priv=self.admin_priv,
                claimant_pub=b"\x00" * 33,
            )

    def test_invalid_payload_role_rejected_at_wrap(self) -> None:
        bad = GrantPayload(
            vault_id=VAULT_ID, grant_id=GRANT_ID,
            claimant_device_id=CLAIMANT_DEVICE_ID,
            approved_role="superuser",  # not in §D11
            granted_by_device_id=ADMIN_DEVICE_ID,
            granted_at="2026-05-03T12:00:00.000Z",
            vault_master_key_b64=base64.b64encode(b"\x10" * 32).decode("ascii"),
            vault_access_secret="x",
        )
        with self.assertRaises(GrantWrapError):
            wrap_grant_for_claimant(
                payload=bad,
                admin_priv=self.admin_priv,
                claimant_pub=self.claimant_pub,
            )


class DeviceGrantV1VectorRoundTripTests(unittest.TestCase):
    """F-C17: every happy-path vector in
    ``tests/protocol/vault-v1/device_grant_v1.json`` must round-trip
    through the runtime's ``_decode_payload`` (which requires
    ``vault_access_secret`` per spec §14.2 v1 extension).

    Pre-fix the vectors omitted ``vault_access_secret`` because the
    cross-runtime harness skipped ``_decode_payload`` and exercised
    only the AEAD primitives directly. The divergence was silent —
    a real device-grant flow would have raised ``GrantWrapError``
    at decode time. This test plugs that gap so a future regenerator
    can't accidentally drop the field again.
    """

    def test_each_vector_decodes_with_vault_access_secret(self) -> None:
        import json
        import os
        from pathlib import Path
        from src.vault_grant_wrap import _decode_payload

        vectors = Path(
            os.path.dirname(__file__) or ".",
        ).resolve().parent.parent / (
            "tests/protocol/vault-v1/device_grant_v1.json"
        )
        cases = json.loads(vectors.read_text(encoding="utf-8"))
        decoded = 0
        for case in cases:
            if "expected_error" in case["expected"]:
                continue  # negative path; rejection happens before decode
            with self.subTest(case=case["name"]):
                plaintext = base64.b64decode(case["inputs"]["grant_plaintext"])
                payload = _decode_payload(plaintext)
                self.assertTrue(
                    payload.vault_access_secret,
                    "vault_access_secret must be a non-empty string",
                )
                decoded += 1
        self.assertGreaterEqual(
            decoded, 4,
            "expected >= 4 happy-path device_grant vectors to round-trip",
        )


if __name__ == "__main__":
    unittest.main()
