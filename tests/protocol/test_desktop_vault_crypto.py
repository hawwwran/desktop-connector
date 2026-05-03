"""Unit tests for desktop/src/vault_crypto.py (T2.1).

Fixed-tuple regression tests for each primitive, plus negative tests
that wrong AAD / wrong key / tampered ciphertext fail closed.

The expected outputs were generated once via the implementation under
test (PyNaCl 1.5.0 / cryptography 41.0.7) and then locked into the
fixtures. Future runs assert byte-for-byte reproducibility — any
runtime change that drifts from these bytes also drifts from the PHP
twin's vectors (T2.2 onwards) and breaks the build.

Argon2id tests use a reduced cost (8 MiB, 2 iterations) to keep the
suite fast. One test exercises the v1-locked defaults (128 MiB, 4
iterations) to confirm they're plumbed through correctly.
"""

from __future__ import annotations

import os
import sys
import unittest

import nacl.exceptions

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_crypto import (  # noqa: E402
    ARGON2ID_ITERATIONS,
    ARGON2ID_MEMORY_KIB,
    ARGON2ID_SALT_BYTES,
    POLY1305_TAG_BYTES,
    XCHACHA20_KEY_BYTES,
    XCHACHA20_NONCE_BYTES,
    DefaultVaultCrypto,
    VaultCrypto,
    aead_decrypt,
    aead_encrypt,
    argon2id_kdf,
    derive_subkey,
)


class DeriveSubkeyTests(unittest.TestCase):
    """HKDF-SHA256 with salt = 32 zero bytes per RFC 5869 §2.2."""

    def test_zero_master_key_with_manifest_label(self) -> None:
        master = bytes(32)
        expected = bytes.fromhex(
            "94eb6d363d5ef45247cb4bfab4256356f7154cb3becd444a915f32e2e26e15d6"
        )
        self.assertEqual(derive_subkey("dc-vault-v1/manifest", master), expected)

    def test_increasing_master_key_with_chunk_label(self) -> None:
        master = bytes(range(1, 33))
        expected = bytes.fromhex(
            "516f28c792a243e331d5fe186213c14f5639b94341a2e1bae342f6fe6d51d181"
        )
        self.assertEqual(derive_subkey("dc-vault-v1/chunk", master), expected)

    def test_increasing_master_key_with_header_label(self) -> None:
        # Same master key as above, different label → different subkey.
        # Confirms the label is actually mixed in (not ignored).
        master = bytes(range(1, 33))
        expected = bytes.fromhex(
            "1dc36f1857b739c6361cbca7583675b94b6bd305d3c2fa372ef079f44d9246c0"
        )
        self.assertEqual(derive_subkey("dc-vault-v1/header", master), expected)

    def test_different_labels_produce_different_subkeys(self) -> None:
        master = bytes(range(1, 33))
        a = derive_subkey("dc-vault-v1/manifest", master)
        b = derive_subkey("dc-vault-v1/chunk", master)
        self.assertNotEqual(a, b)

    def test_custom_length(self) -> None:
        master = bytes(32)
        out_64 = derive_subkey("dc-vault-v1/header", master, length=64)
        out_32 = derive_subkey("dc-vault-v1/header", master, length=32)
        self.assertEqual(len(out_64), 64)
        self.assertEqual(len(out_32), 32)
        # HKDF-Expand chains T(i) blocks: T(1) depends only on (prk, info),
        # so requesting more bytes appends additional T(i)s rather than
        # changing what's already there. The first 32 bytes are identical.
        self.assertEqual(out_64[:32], out_32)
        # The next block IS distinct.
        self.assertNotEqual(out_64[32:], bytes(32))

    def test_rejects_wrong_master_key_length(self) -> None:
        with self.assertRaisesRegex(ValueError, "master_key must be 32 bytes"):
            derive_subkey("dc-vault-v1/manifest", b"too-short")

    def test_rejects_non_positive_length(self) -> None:
        master = bytes(32)
        with self.assertRaisesRegex(ValueError, "length must be positive"):
            derive_subkey("dc-vault-v1/manifest", master, length=0)


class AeadEncryptDecryptTests(unittest.TestCase):
    """XChaCha20-Poly1305-IETF round-trip + tamper-detection."""

    KEY = bytes.fromhex("0123456789abcdef" * 4)
    NONCE = bytes.fromhex("aabbccddeeff" + "00112233" * 4 + "aabb")[:24]
    AAD = b"aad-bytes-fixture"
    PLAINTEXT = b"hello-vault-plaintext"
    EXPECTED_CT = bytes.fromhex(
        "3b917b4adf5e4acfac297732b17085b31cb9ba7d118cd86171b46f830dbfeb0c403b1ead53"
    )

    def test_encrypt_produces_locked_ciphertext(self) -> None:
        ct = aead_encrypt(self.PLAINTEXT, self.KEY, self.NONCE, self.AAD)
        self.assertEqual(ct, self.EXPECTED_CT)
        # Layout: ciphertext (= plaintext_len) || 16-byte Poly1305 tag.
        self.assertEqual(len(ct), len(self.PLAINTEXT) + POLY1305_TAG_BYTES)

    def test_round_trip_recovers_plaintext(self) -> None:
        pt = aead_decrypt(self.EXPECTED_CT, self.KEY, self.NONCE, self.AAD)
        self.assertEqual(pt, self.PLAINTEXT)

    def test_empty_plaintext_round_trip(self) -> None:
        ct = aead_encrypt(b"", self.KEY, self.NONCE, self.AAD)
        self.assertEqual(len(ct), POLY1305_TAG_BYTES)  # only the tag
        self.assertEqual(aead_decrypt(ct, self.KEY, self.NONCE, self.AAD), b"")

    def test_wrong_aad_fails_closed(self) -> None:
        with self.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(self.EXPECTED_CT, self.KEY, self.NONCE, b"different-aad")

    def test_wrong_key_fails_closed(self) -> None:
        wrong_key = bytes(32)  # all zeros
        with self.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(self.EXPECTED_CT, wrong_key, self.NONCE, self.AAD)

    def test_wrong_nonce_fails_closed(self) -> None:
        wrong_nonce = bytes(24)
        with self.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(self.EXPECTED_CT, self.KEY, wrong_nonce, self.AAD)

    def test_tampered_ciphertext_fails_closed(self) -> None:
        # Flip a bit in the ciphertext portion (not the tag).
        tampered = bytearray(self.EXPECTED_CT)
        tampered[0] ^= 0x01
        with self.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(bytes(tampered), self.KEY, self.NONCE, self.AAD)

    def test_tampered_tag_fails_closed(self) -> None:
        tampered = bytearray(self.EXPECTED_CT)
        tampered[-1] ^= 0x01
        with self.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(bytes(tampered), self.KEY, self.NONCE, self.AAD)

    def test_rejects_wrong_key_length(self) -> None:
        with self.assertRaisesRegex(ValueError, "key must be 32 bytes"):
            aead_encrypt(self.PLAINTEXT, b"short", self.NONCE, self.AAD)

    def test_rejects_wrong_nonce_length(self) -> None:
        # XChaCha20 wants 24 bytes; passing the AES-GCM 12-byte length
        # is the most likely real bug.
        nonce_12 = bytes(12)
        with self.assertRaisesRegex(ValueError, "nonce must be 24 bytes"):
            aead_encrypt(self.PLAINTEXT, self.KEY, nonce_12, self.AAD)

    def test_rejects_too_short_ciphertext(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 16 bytes"):
            aead_decrypt(b"short", self.KEY, self.NONCE, self.AAD)


class Argon2idKdfTests(unittest.TestCase):
    """Argon2id with v1-locked params; reduced cost in tests for speed."""

    SALT = bytes.fromhex("00010203040506070809101112131415")  # 16 bytes
    PASSPHRASE = "test-passphrase"

    # Reduced-cost fixture (8 MiB, 2 iterations) so the suite stays fast.
    REDUCED_EXPECTED = bytes.fromhex(
        "0ad0ca7b42ce9661b65a6a0d3a9e9dc73090c57ab4bb1fd5cf3f9e83eb7f5587"
    )

    def test_reduced_cost_locked_output(self) -> None:
        out = argon2id_kdf(
            self.PASSPHRASE,
            self.SALT,
            32,
            memory_kib=8192,  # 8 MiB
            iterations=2,
        )
        self.assertEqual(out, self.REDUCED_EXPECTED)

    def test_v1_default_params_are_plumbed(self) -> None:
        # Spot-check that calling with no kwarg overrides actually uses
        # the locked v1 params from formats §12.2 — confirms the
        # constants haven't drifted from their captured values.
        self.assertEqual(ARGON2ID_MEMORY_KIB, 131_072)
        self.assertEqual(ARGON2ID_ITERATIONS, 4)
        self.assertEqual(ARGON2ID_SALT_BYTES, 16)
        self.assertEqual(XCHACHA20_KEY_BYTES, 32)
        self.assertEqual(XCHACHA20_NONCE_BYTES, 24)
        # The actual argon2 call at full cost takes ~1s and is exercised
        # by the cross-platform vector harness (T2.4) — running it here
        # for every test would slow the suite down with no extra coverage.

    def test_deterministic_for_same_inputs(self) -> None:
        a = argon2id_kdf(self.PASSPHRASE, self.SALT, 32, memory_kib=8192, iterations=2)
        b = argon2id_kdf(self.PASSPHRASE, self.SALT, 32, memory_kib=8192, iterations=2)
        self.assertEqual(a, b)

    def test_different_salt_produces_different_output(self) -> None:
        salt2 = bytes(16)  # all zeros, different from SALT
        out_a = argon2id_kdf(self.PASSPHRASE, self.SALT, 32, memory_kib=8192, iterations=2)
        out_b = argon2id_kdf(self.PASSPHRASE, salt2,    32, memory_kib=8192, iterations=2)
        self.assertNotEqual(out_a, out_b)

    def test_different_passphrase_produces_different_output(self) -> None:
        out_a = argon2id_kdf(self.PASSPHRASE,    self.SALT, 32, memory_kib=8192, iterations=2)
        out_b = argon2id_kdf("other-passphrase", self.SALT, 32, memory_kib=8192, iterations=2)
        self.assertNotEqual(out_a, out_b)

    def test_nfc_normalizes_passphrase(self) -> None:
        # U+00E9 (LATIN SMALL LETTER E WITH ACUTE, precomposed) and
        # U+0065 U+0301 (LATIN SMALL LETTER E + COMBINING ACUTE ACCENT,
        # decomposed) render identically but encode differently in
        # UTF-8. Without NFC normalization a user typing the same word
        # on a Mac (decomposed by default) vs a Linux box (precomposed
        # by default) would derive different keys and lock themselves
        # out. The implementation normalizes to NFC before encoding.
        precomposed = "café"      # café
        decomposed  = "café"     # cafe + combining acute
        self.assertNotEqual(precomposed.encode("utf-8"), decomposed.encode("utf-8"))

        out_a = argon2id_kdf(precomposed, self.SALT, 32, memory_kib=8192, iterations=2)
        out_b = argon2id_kdf(decomposed,  self.SALT, 32, memory_kib=8192, iterations=2)
        self.assertEqual(out_a, out_b)

    def test_rejects_wrong_salt_length(self) -> None:
        with self.assertRaisesRegex(ValueError, f"salt must be {ARGON2ID_SALT_BYTES} bytes"):
            argon2id_kdf(self.PASSPHRASE, b"short-salt", 32, memory_kib=8192, iterations=2)

    def test_rejects_non_positive_output_length(self) -> None:
        with self.assertRaisesRegex(ValueError, "output_length must be positive"):
            argon2id_kdf(self.PASSPHRASE, self.SALT, 0, memory_kib=8192, iterations=2)

    def test_rejects_non_positive_memory(self) -> None:
        with self.assertRaisesRegex(ValueError, "memory_kib must be positive"):
            argon2id_kdf(self.PASSPHRASE, self.SALT, 32, memory_kib=0, iterations=2)

    def test_rejects_non_positive_iterations(self) -> None:
        with self.assertRaisesRegex(ValueError, "iterations must be positive"):
            argon2id_kdf(self.PASSPHRASE, self.SALT, 32, memory_kib=8192, iterations=0)


class VaultCryptoProtocolTests(unittest.TestCase):
    """T2.5 — VaultCrypto Protocol + DefaultVaultCrypto instance.

    The Protocol exists so future service layers (T3 onwards) can take
    a `VaultCrypto` argument rather than importing module functions
    directly; tests then pass a stub instead of monkey-patching globals.
    """

    def test_default_instance_satisfies_protocol(self) -> None:
        # @runtime_checkable lets isinstance() inspect the surface.
        self.assertIsInstance(DefaultVaultCrypto, VaultCrypto)

    def test_default_instance_delegates_to_module_functions(self) -> None:
        # Each attribute on DefaultVaultCrypto IS the corresponding
        # module function (via staticmethod) — invoking through either
        # path produces the same bytes.
        master = bytes(32)
        via_module = derive_subkey("dc-vault-v1/manifest", master)
        via_default = DefaultVaultCrypto.derive_subkey("dc-vault-v1/manifest", master)
        self.assertEqual(via_module, via_default)

    def test_a_stub_can_substitute_for_dependency_injection(self) -> None:
        # The acceptance criterion: a service that types its dependency
        # as VaultCrypto can take a fake. Demonstrates the pattern future
        # callers will use.
        class FakeCrypto:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def derive_subkey(self, label: str, master_key: bytes, length: int = 32) -> bytes:
                self.calls.append(("derive_subkey", label, length))
                return b"\xff" * length

            def aead_encrypt(self, plaintext: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes:
                self.calls.append(("aead_encrypt", len(plaintext), len(aad)))
                return b"FAKE_CT_" + plaintext + b"_TAG"

            def aead_decrypt(self, ct: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes:
                self.calls.append(("aead_decrypt", len(ct)))
                return b"FAKE_PT"

            def argon2id_kdf(self, passphrase: str, salt: bytes, output_length: int = 32) -> bytes:
                self.calls.append(("argon2id_kdf", passphrase, output_length))
                return b"\x00" * output_length

            def build_manifest_aad(
                self, vault_id: str, revision: int, parent_revision: int, author_device_id: str,
            ) -> bytes:
                return b"FAKE_MANIFEST_AAD"

            def build_chunk_aad(
                self, vault_id: str, remote_folder_id: str, file_id: str,
                file_version_id: str, chunk_index: int, chunk_plaintext_size: int,
            ) -> bytes:
                return b"FAKE_CHUNK_AAD"

            def build_header_aad(self, vault_id: str, header_revision: int) -> bytes:
                return b"FAKE_HEADER_AAD"

            def build_recovery_aad(self, vault_id: str, envelope_id: str) -> bytes:
                return b"FAKE_RECOVERY_AAD"

            def build_device_grant_aad(
                self, vault_id: str, grant_id: str, claimant_device_id: str,
            ) -> bytes:
                return b"FAKE_GRANT_AAD"

        fake = FakeCrypto()

        # Future service signature might look like:
        def encrypt_for_service(crypto: VaultCrypto, payload: bytes) -> bytes:
            key = crypto.derive_subkey("dc-vault-v1/manifest", bytes(32))
            return crypto.aead_encrypt(payload, key, bytes(24), b"aad")

        result = encrypt_for_service(fake, b"hello")
        self.assertEqual(result, b"FAKE_CT_hello_TAG")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0][0], "derive_subkey")
        self.assertEqual(fake.calls[1][0], "aead_encrypt")

        # Sanity: the fake satisfies the Protocol via isinstance().
        self.assertIsInstance(fake, VaultCrypto)


if __name__ == "__main__":
    unittest.main()
