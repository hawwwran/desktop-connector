"""T3.1 — Vault domain class round-trip tests.

Acceptance: create vault → close → reopen with passphrase → manifest
decrypts. We use a fake in-memory relay so the test doesn't need a
real PHP server, and reduced-cost Argon2id (8 MiB / 2 iterations) so
the suite stays fast.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402


class FakeRelay:
    """In-memory replacement for the PHP relay's create + get-header
    surface. Records what was created so ``get_header`` can return
    the right envelope on reopen.

    The recovery_envelope_id + argon params + wrap-envelope ciphertext
    are returned alongside the header so the recovery flow can locate
    them without first decrypting the header — see ``Vault.open``.
    """

    def __init__(self) -> None:
        self.vaults: dict[str, dict] = {}

    def create_vault(
        self,
        vault_id: str,
        vault_access_token_hash: bytes,
        encrypted_header: bytes,
        header_hash: str,
        initial_manifest_ciphertext: bytes,
        initial_manifest_hash: str,
    ) -> dict:
        # Stash everything the test will need on reopen. We also
        # extract the recovery envelope from the header plaintext
        # JSON; in production, the relay would emit it as a parallel
        # field after a header-write sets it. The vault module's open
        # path expects the field, so we synthesize it here.
        # Note: we can't actually decrypt the header to read the
        # envelope's metadata — we capture it from the create-time
        # caller. This fake relay is collaborative with the test, not
        # an HTTP-faithful mock.
        self.vaults[vault_id] = {
            "encrypted_header": encrypted_header,
            "header_hash": header_hash,
            "header_revision": 1,
            "manifest_envelope_bytes": initial_manifest_ciphertext,
            "manifest_revision": 1,
        }
        return {"vault_id": vault_id}

    def stash_recovery_envelope(self, vault_id: str, envelope: dict) -> None:
        """Test helper: record the recovery envelope's metadata so
        ``get_header`` can return it on reopen.
        """
        self.vaults[vault_id]["recovery_envelope"] = envelope

    def get_header(self, vault_id: str, vault_access_secret: str) -> dict:
        return self.vaults[vault_id]


class VaultRoundTripTests(unittest.TestCase):
    PASSPHRASE = "correct horse battery staple"

    # Reduced cost so tests stay fast.
    ARGON_KIB = 8192
    ARGON_ITERS = 2

    def test_create_then_close_then_reopen_decrypts_manifest(self) -> None:
        """The acceptance criterion."""
        relay = FakeRelay()

        vault = Vault.create_new(
            relay, recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB,
            argon_iterations=self.ARGON_ITERS,
        )

        # Master key + recovery secret are accessible while open.
        self.assertIsNotNone(vault.master_key)
        self.assertEqual(len(vault.master_key), 32)
        self.assertIsNotNone(vault.recovery_secret)
        self.assertEqual(len(vault.recovery_secret), 32)

        # Vault id is the canonical 12-char base32 form.
        self.assertEqual(len(vault.vault_id), 12)
        self.assertRegex(vault.vault_id, r"^[A-Z2-7]{12}$")

        # Capture the kit secret + access secret + envelope metadata
        # before closing. In production, the wizard saves these to disk
        # before close().
        kit_secret = vault.recovery_secret
        access_secret = vault.vault_access_secret

        # Decode the header envelope to extract the recovery envelope's
        # metadata. The relay would normally surface this via a
        # follow-up field on the create response.
        header_subkey = _derive_header_subkey(vault.master_key)
        recovery_envelope_meta = _extract_recovery_envelope(
            relay.vaults[vault.vault_id]["encrypted_header"],
            vault.vault_id,
            vault.master_key,
        )
        relay.stash_recovery_envelope(vault.vault_id, recovery_envelope_meta)

        vault_id = vault.vault_id

        # close() zeros the buffers.
        vault.close()
        self.assertIsNone(vault.master_key)
        self.assertIsNone(vault.recovery_secret)

        # Reopen via passphrase + kit.
        reopened = Vault.open(
            relay, vault_id=vault_id,
            recovery_passphrase=self.PASSPHRASE,
            recovery_secret=kit_secret,
            vault_access_secret=access_secret,
        )

        # Master key recovered.
        self.assertIsNotNone(reopened.master_key)
        self.assertEqual(len(reopened.master_key), 32)

        # Manifest decrypts.
        manifest = reopened.decrypt_manifest()
        self.assertEqual(manifest["schema"], "dc-vault-manifest-v1")
        self.assertEqual(manifest["vault_id"], vault_id)
        self.assertEqual(manifest["revision"], 1)
        self.assertEqual(manifest["parent_revision"], 0)
        self.assertEqual(manifest["remote_folders"], [])

        reopened.close()

    def test_close_is_idempotent(self) -> None:
        relay = FakeRelay()
        vault = Vault.create_new(
            relay, recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        )
        vault.close()
        vault.close()  # second call is a no-op
        self.assertIsNone(vault.master_key)

    def test_decrypt_manifest_after_close_raises(self) -> None:
        relay = FakeRelay()
        vault = Vault.create_new(
            relay, recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        )
        vault.close()
        with self.assertRaises(ValueError):
            vault.decrypt_manifest()

    def test_context_manager_closes_on_exit(self) -> None:
        relay = FakeRelay()
        with Vault.create_new(
            relay, recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        ) as vault:
            self.assertIsNotNone(vault.master_key)
        # Exit should have called close().
        self.assertIsNone(vault.master_key)

    def test_vault_id_dashed_form(self) -> None:
        relay = FakeRelay()
        vault = Vault.create_new(
            relay, recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        )
        try:
            self.assertRegex(vault.vault_id_dashed, r"^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$")
            self.assertEqual(vault.vault_id_dashed.replace("-", ""), vault.vault_id)
        finally:
            vault.close()


class VaultPrepareThenPublishTests(unittest.TestCase):
    """T8-pre: prepare_new + publish_initial split (the wizard's
    defer-the-relay-create safety net).
    """

    PASSPHRASE = "correct horse battery staple"
    ARGON_KIB = 8192
    ARGON_ITERS = 2

    def test_prepare_new_does_not_post_to_relay(self) -> None:
        relay = FakeRelay()
        vault = Vault.prepare_new(
            recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        )
        try:
            self.assertEqual(relay.vaults, {})
            self.assertTrue(vault.has_pending_publish)
            self.assertEqual(len(vault.master_key), 32)
            self.assertEqual(len(vault.recovery_secret), 32)
            self.assertRegex(vault.vault_id, r"^[A-Z2-7]{12}$")
        finally:
            vault.close()

    def test_publish_initial_posts_once_and_clears_pending(self) -> None:
        relay = FakeRelay()
        vault = Vault.prepare_new(
            recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        )
        try:
            vault.publish_initial(relay)
            self.assertIn(vault.vault_id, relay.vaults)
            self.assertFalse(vault.has_pending_publish)

            # A second call must not double-POST — the second relay.create_vault
            # would 409 in production; here we just guarantee we don't
            # hand it the same payload again.
            with self.assertRaises(ValueError):
                vault.publish_initial(relay)
            self.assertEqual(len(relay.vaults), 1)
        finally:
            vault.close()

    def test_publish_failure_keeps_payload_for_retry(self) -> None:
        """A relay flake on the first publish must leave the prepared
        payload in place so the wizard's Retry button can re-POST the
        byte-identical bundle (no fork between local grant and relay
        vault_id).
        """

        class FlakyRelay(FakeRelay):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            def create_vault(self, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("relay unreachable")
                return super().create_vault(**kwargs)

        relay = FlakyRelay()
        vault = Vault.prepare_new(
            recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        )
        try:
            with self.assertRaises(RuntimeError):
                vault.publish_initial(relay)
            # Pending payload survives so a retry uses the same bundle.
            self.assertTrue(vault.has_pending_publish)
            self.assertEqual(relay.vaults, {})

            # Retry succeeds.
            vault.publish_initial(relay)
            self.assertFalse(vault.has_pending_publish)
            self.assertIn(vault.vault_id, relay.vaults)
            self.assertEqual(relay.attempts, 2)
        finally:
            vault.close()

    def test_create_new_is_prepare_plus_publish(self) -> None:
        """The back-compat path goes through prepare + publish so a
        single relay POST happens with the same body shape that
        prepare_new produced.
        """
        relay = FakeRelay()
        vault = Vault.create_new(
            relay,
            recovery_passphrase=self.PASSPHRASE,
            argon_memory_kib=self.ARGON_KIB, argon_iterations=self.ARGON_ITERS,
        )
        try:
            self.assertIn(vault.vault_id, relay.vaults)
            self.assertFalse(vault.has_pending_publish)
        finally:
            vault.close()


# ---------------------------------------------------------------- test helpers


def _derive_header_subkey(master_key: bytes) -> bytes:
    from src.vault_crypto import derive_subkey
    return derive_subkey("dc-vault-v1/header", master_key)


def _extract_recovery_envelope(
    encrypted_header: bytes,
    vault_id: str,
    master_key: bytes,
) -> dict:
    """Decrypt the header envelope, extract the recovery_envelope[0]
    field, and return a dict of the metadata the recovery flow needs.
    Production code uses a parallel relay-side field; this helper
    does the equivalent extraction at test time.
    """
    import base64
    import json

    from src.vault_crypto import (
        aead_decrypt, build_header_aad, derive_subkey,
    )

    # Header envelope plaintext header is 1+12+8+24 = 45 bytes.
    nonce = encrypted_header[1 + 12 + 8 : 1 + 12 + 8 + 24]
    ct = encrypted_header[1 + 12 + 8 + 24:]
    rev = int.from_bytes(encrypted_header[13:21], "big")

    subkey = derive_subkey("dc-vault-v1/header", master_key)
    aad = build_header_aad(vault_id, rev)
    pt = aead_decrypt(ct, subkey, nonce, aad)
    decoded = json.loads(pt.decode("utf-8"))

    env = decoded["recovery_envelopes"][0]
    return {
        "envelope_id": env["envelope_id"],
        "argon_salt": base64.b64decode(env["argon_salt"]),
        "argon_params": env["argon_params"],
        "nonce": base64.b64decode(env["nonce"]),
        "aead_ciphertext_and_tag": base64.b64decode(env["aead_ciphertext_and_tag"]),
    }


if __name__ == "__main__":
    unittest.main()
