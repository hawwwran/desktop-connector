"""T13.4 — wrap / unwrap the vault grant payload via X25519 + XChaCha20-Poly1305."""

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


def _keypair():
    from nacl.public import PrivateKey
    priv = PrivateKey.generate()
    return bytes(priv), bytes(priv.public_key)


def _sample_payload() -> GrantPayload:
    return GrantPayload(
        vault_master_key_b64=base64.b64encode(b"\x10" * 32).decode("ascii"),
        vault_access_secret="super-high-entropy-secret",
        role="sync",
        granted_by_device_id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        granted_at=1_777_900_000,
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
        wrapped = wrap_grant_for_claimant(
            payload=payload,
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
            join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
        )
        recovered = unwrap_grant_for_claimant(
            wrapped=wrapped,
            claimant_priv=self.claimant_priv,
            admin_pub=self.admin_pub,
            join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
        )
        self.assertEqual(recovered, payload)

    def test_wrap_uses_random_nonce_per_call(self) -> None:
        payload = _sample_payload()
        w1 = wrap_grant_for_claimant(
            payload=payload, admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
            join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
        )
        w2 = wrap_grant_for_claimant(
            payload=payload, admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
            join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
        )
        self.assertNotEqual(w1, w2)
        # But both decrypt to the same payload.
        self.assertEqual(
            unwrap_grant_for_claimant(
                wrapped=w1, claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
            ),
            unwrap_grant_for_claimant(
                wrapped=w2, claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
            ),
        )

    def test_wrong_claimant_priv_fails_decrypt(self) -> None:
        wrapped = wrap_grant_for_claimant(
            payload=_sample_payload(),
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
            join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
        )
        attacker_priv, _ = _keypair()
        with self.assertRaises(GrantWrapError):
            unwrap_grant_for_claimant(
                wrapped=wrapped, claimant_priv=attacker_priv,
                admin_pub=self.admin_pub,
                join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
            )

    def test_wrong_join_request_id_fails_decrypt(self) -> None:
        """join_request_id is the AEAD AAD — a mismatched id breaks the tag."""
        wrapped = wrap_grant_for_claimant(
            payload=_sample_payload(),
            admin_priv=self.admin_priv,
            claimant_pub=self.claimant_pub,
            join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
        )
        with self.assertRaises(GrantWrapError):
            unwrap_grant_for_claimant(
                wrapped=wrapped, claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                join_request_id="jr_v1_yyyyyyyyyyyyyyyyyyyyyyyy",
            )

    def test_truncated_wrapped_blob_rejected(self) -> None:
        with self.assertRaises(GrantWrapError):
            unwrap_grant_for_claimant(
                wrapped=b"\x00" * 16,  # less than 24-byte nonce + at least 1 byte ct
                claimant_priv=self.claimant_priv,
                admin_pub=self.admin_pub,
                join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
            )

    def test_invalid_key_lengths_rejected(self) -> None:
        with self.assertRaises(GrantWrapError):
            wrap_grant_for_claimant(
                payload=_sample_payload(),
                admin_priv=b"\x00" * 31,
                claimant_pub=self.claimant_pub,
                join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
            )
        with self.assertRaises(GrantWrapError):
            wrap_grant_for_claimant(
                payload=_sample_payload(),
                admin_priv=self.admin_priv,
                claimant_pub=b"\x00" * 33,
                join_request_id="jr_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
            )

    def test_empty_join_request_id_rejected(self) -> None:
        with self.assertRaises(GrantWrapError):
            wrap_grant_for_claimant(
                payload=_sample_payload(),
                admin_priv=self.admin_priv,
                claimant_pub=self.claimant_pub,
                join_request_id="",
            )


if __name__ == "__main__":
    unittest.main()
