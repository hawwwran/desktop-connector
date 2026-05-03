"""Vault v1 cross-platform test vectors harness.

Discovers JSON case files under ``tests/protocol/vault-v1/`` and exercises each
case against the desktop Python primitives (``desktop/src/vault_crypto.py``).
The PHP twin (``server/src/Crypto/VaultCrypto.php``, T2.4) runs the same JSON
files via PHPUnit so a divergence between the two runtimes breaks the build.

Schema lock: T0 §A18 in
``docs/plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md``.
Byte-format reference: ``docs/protocol/vault-v1-formats.md``.

Files filled so far:
    manifest_v1.json (T2.2 + T4.1) — base manifest vectors plus T4
    remote-folder add/remove cases.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from typing import Any

import nacl.exceptions

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_crypto import (  # noqa: E402
    aead_decrypt,
    aead_encrypt,
    build_chunk_aad,
    build_chunk_envelope,
    build_device_grant_aad,
    build_device_grant_envelope,
    build_export_outer_header,
    build_export_wrap_aad,
    build_header_aad,
    build_header_envelope,
    build_manifest_aad,
    build_manifest_envelope,
    build_recovery_aad,
    build_recovery_envelope,
    derive_device_grant_wrap_key,
    derive_export_wrap_key,
    derive_recovery_wrap_key,
    derive_subkey,
)

VECTORS_DIR = os.path.join(os.path.dirname(__file__), "vault-v1")

EXPECTED_FILES = (
    "manifest_v1.json",
    "chunk_v1.json",
    "header_v1.json",
    "recovery_envelope_v1.json",
    "device_grant_v1.json",
    "export_bundle_v1.json",
)


def _load_cases(filename: str) -> list[dict[str, Any]]:
    path = os.path.join(VECTORS_DIR, filename)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{filename}: expected JSON array, got {type(data).__name__}")
    return data


def _validate_case_shape(filename: str, index: int, case: dict[str, Any]) -> None:
    if not isinstance(case, dict):
        raise ValueError(f"{filename}[{index}]: case must be an object")
    for required in ("name", "description", "inputs", "expected"):
        if required not in case:
            raise ValueError(f"{filename}[{index}]: missing required key '{required}'")
    if not isinstance(case["inputs"], dict):
        raise ValueError(f"{filename}[{index}]: 'inputs' must be an object")
    if not isinstance(case["expected"], dict):
        raise ValueError(f"{filename}[{index}]: 'expected' must be an object")


# ====================================================================
#  Per-primitive case runners. Each takes a parsed case dict and
#  asserts its expected outputs (positive case) or its expected error
#  (negative case via "tamper" / "expected_error").
# ====================================================================


def _run_manifest_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    """Verify one manifest_v1.json case end-to-end against vault_crypto.

    Positive cases assert byte-exact outputs at every stage (AAD, subkey,
    AEAD ciphertext, envelope) and round-trip-decrypt to the original
    plaintext. Negative cases apply the tampering described under the
    case's ``tamper`` key and assert ``aead_decrypt`` raises
    ``nacl.exceptions.CryptoError``.
    """
    inputs = case["inputs"]
    expected = case["expected"]

    master_key = bytes.fromhex(inputs["vault_master_key"])
    nonce = bytes.fromhex(inputs["nonce"])
    plaintext = base64.b64decode(inputs["manifest_plaintext"])

    # Compute AAD + subkey + ciphertext + envelope from the inputs alone.
    aad = build_manifest_aad(
        vault_id=inputs["vault_id"],
        revision=int(inputs["revision"]),
        parent_revision=int(inputs["parent_revision"]),
        author_device_id=inputs["author_device_id"],
    )
    subkey = derive_subkey("dc-vault-v1/manifest", master_key)
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    envelope = build_manifest_envelope(
        vault_id=inputs["vault_id"],
        revision=int(inputs["revision"]),
        parent_revision=int(inputs["parent_revision"]),
        author_device_id=inputs["author_device_id"],
        nonce=nonce,
        aead_ciphertext_and_tag=ciphertext,
    )

    if "expected_error" in expected:
        # Negative case: apply the tampering, attempt decryption, assert
        # it fails closed.
        tamper = case.get("tamper", {})
        decrypt_aad = aad
        decrypt_envelope = envelope
        decrypt_ct = ciphertext

        if "envelope_byte_xor" in tamper:
            spec = tamper["envelope_byte_xor"]
            offset = int(spec["offset"])
            xor_byte = int(spec["xor"], 16)
            buf = bytearray(envelope)
            buf[offset] ^= xor_byte
            decrypt_envelope = bytes(buf)
            # Manifest envelope plaintext header already includes the
            # nonce; byte 85 is the AEAD ciphertext start.
            decrypt_ct = decrypt_envelope[85:]

        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])

        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_ct, subkey, nonce, decrypt_aad)
        return

    # Positive case: every expected.* must match byte-exact.
    if "aad" in expected:
        test.assertEqual(aad.hex(), expected["aad"], "AAD mismatch")
    if "subkey" in expected:
        test.assertEqual(subkey.hex(), expected["subkey"], "subkey mismatch")
    if "aead_ciphertext_and_tag" in expected:
        test.assertEqual(
            ciphertext.hex(),
            expected["aead_ciphertext_and_tag"],
            "ciphertext mismatch",
        )
    if "envelope_bytes" in expected:
        test.assertEqual(envelope.hex(), expected["envelope_bytes"], "envelope mismatch")

    # Round-trip: decrypt and verify plaintext is recovered.
    recovered = aead_decrypt(ciphertext, subkey, nonce, aad)
    test.assertEqual(recovered, plaintext, "round-trip plaintext mismatch")


def _run_chunk_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    inputs = case["inputs"]
    expected = case["expected"]

    master_key = bytes.fromhex(inputs["vault_master_key"])
    nonce = bytes.fromhex(inputs["nonce"])
    plaintext = base64.b64decode(inputs["chunk_plaintext"])

    aad = build_chunk_aad(
        inputs["vault_id"],
        inputs["remote_folder_id"],
        inputs["file_id"],
        inputs["file_version_id"],
        int(inputs["chunk_index"]),
        int(inputs["chunk_plaintext_size"]),
    )
    subkey = derive_subkey("dc-vault-v1/chunk", master_key)
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    envelope = build_chunk_envelope(nonce=nonce, aead_ciphertext_and_tag=ciphertext)

    if "expected_error" in expected:
        tamper = case.get("tamper", {})
        decrypt_aad = aad
        decrypt_envelope = envelope
        if "envelope_byte_xor" in tamper:
            spec = tamper["envelope_byte_xor"]
            buf = bytearray(envelope)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            decrypt_envelope = bytes(buf)
        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])
        # Chunk envelope = nonce(24) || ciphertext_and_tag.
        decrypt_ct = decrypt_envelope[24:]
        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_ct, subkey, nonce, decrypt_aad)
        return

    test.assertEqual(aad.hex(), expected["aad"], "AAD mismatch")
    test.assertEqual(subkey.hex(), expected["subkey"], "subkey mismatch")
    test.assertEqual(ciphertext.hex(), expected["aead_ciphertext_and_tag"])
    test.assertEqual(envelope.hex(), expected["envelope_bytes"])
    test.assertEqual(aead_decrypt(ciphertext, subkey, nonce, aad), plaintext)


def _run_header_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    inputs = case["inputs"]
    expected = case["expected"]

    master_key = bytes.fromhex(inputs["vault_master_key"])
    nonce = bytes.fromhex(inputs["nonce"])
    plaintext = base64.b64decode(inputs["header_plaintext"])
    revision = int(inputs["header_revision"])

    aad = build_header_aad(inputs["vault_id"], revision)
    subkey = derive_subkey("dc-vault-v1/header", master_key)
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    envelope = build_header_envelope(
        vault_id=inputs["vault_id"], header_revision=revision,
        nonce=nonce, aead_ciphertext_and_tag=ciphertext,
    )

    if "expected_error" in expected:
        tamper = case.get("tamper", {})
        decrypt_aad = aad
        decrypt_ct = ciphertext
        if "envelope_byte_xor" in tamper:
            spec = tamper["envelope_byte_xor"]
            buf = bytearray(envelope)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            decrypt_ct = bytes(buf)[1 + 12 + 8 + 24:]   # skip plaintext header + nonce
        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])
        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_ct, subkey, nonce, decrypt_aad)
        return

    test.assertEqual(aad.hex(), expected["aad"])
    test.assertEqual(subkey.hex(), expected["subkey"])
    test.assertEqual(ciphertext.hex(), expected["aead_ciphertext_and_tag"])
    test.assertEqual(envelope.hex(), expected["envelope_bytes"])
    test.assertEqual(aead_decrypt(ciphertext, subkey, nonce, aad), plaintext)


def _run_recovery_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    inputs = case["inputs"]
    expected = case["expected"]

    master_key = bytes.fromhex(inputs["vault_master_key"])
    salt = bytes.fromhex(inputs["argon_salt"])
    nonce = bytes.fromhex(inputs["nonce"])
    secret = bytes.fromhex(inputs["recovery_secret"])
    memory_kib = int(inputs["argon_memory_kib"])
    iterations = int(inputs["argon_iterations"])

    aad = build_recovery_aad(inputs["vault_id"], inputs["envelope_id"])
    wrap_key = derive_recovery_wrap_key(
        passphrase=inputs["passphrase"],
        recovery_secret=secret,
        argon_salt=salt,
        memory_kib=memory_kib,
        iterations=iterations,
    )
    ciphertext = aead_encrypt(master_key, wrap_key, nonce, aad)
    envelope = build_recovery_envelope(
        vault_id=inputs["vault_id"], envelope_id=inputs["envelope_id"],
        argon_salt=salt, nonce=nonce, aead_ciphertext_and_tag=ciphertext,
    )

    if "expected_error" in expected:
        # Negative paths: re-derive wrap key with overridden passphrase, OR
        # tamper the envelope byte-by-byte before decrypting.
        decrypt_wrap_key = wrap_key
        decrypt_ct = ciphertext
        if "decrypt_passphrase_override" in inputs:
            decrypt_wrap_key = derive_recovery_wrap_key(
                passphrase=inputs["decrypt_passphrase_override"],
                recovery_secret=secret, argon_salt=salt,
                memory_kib=memory_kib, iterations=iterations,
            )
        tamper = case.get("tamper", {})
        if "envelope_byte_xor" in tamper:
            spec = tamper["envelope_byte_xor"]
            buf = bytearray(envelope)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            # Recovery envelope: 1+12+30+16+24 = 83 bytes plaintext header,
            # then ciphertext_and_tag.
            decrypt_ct = bytes(buf)[83:]
        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_ct, decrypt_wrap_key, nonce, aad)
        return

    test.assertEqual(aad.hex(), expected["aad"])
    test.assertEqual(wrap_key.hex(), expected["wrap_key"])
    test.assertEqual(ciphertext.hex(), expected["aead_ciphertext_and_tag"])
    test.assertEqual(envelope.hex(), expected["envelope_bytes"])
    test.assertEqual(aead_decrypt(ciphertext, wrap_key, nonce, aad), master_key)


def _run_device_grant_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    import nacl.bindings

    inputs = case["inputs"]
    expected = case["expected"]

    nonce = bytes.fromhex(inputs["nonce"])
    plaintext = base64.b64decode(inputs["grant_plaintext"])
    admin_priv = bytes.fromhex(inputs["admin_priv_seed"])
    claimant_pub = bytes.fromhex(inputs["claimant_pubkey"])
    shared_secret = nacl.bindings.crypto_scalarmult(admin_priv, claimant_pub)
    test.assertEqual(shared_secret.hex(), inputs["shared_secret"], "shared secret mismatch")

    aad = build_device_grant_aad(
        inputs["vault_id"], inputs["grant_id"], inputs["claimant_device_id"],
    )
    wrap_key = derive_device_grant_wrap_key(shared_secret)
    ciphertext = aead_encrypt(plaintext, wrap_key, nonce, aad)
    envelope = build_device_grant_envelope(
        vault_id=inputs["vault_id"], grant_id=inputs["grant_id"],
        claimant_pubkey=claimant_pub, nonce=nonce,
        aead_ciphertext_and_tag=ciphertext,
    )

    if "expected_error" in expected:
        tamper = case.get("tamper", {})
        decrypt_aad = aad
        decrypt_ct = ciphertext
        if "envelope_byte_xor" in tamper:
            spec = tamper["envelope_byte_xor"]
            buf = bytearray(envelope)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            # Plaintext header: 1 + 12 + 30 + 32 = 75 bytes; nonce: 24
            decrypt_ct = bytes(buf)[75 + 24:]
        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])
        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_ct, wrap_key, nonce, decrypt_aad)
        return

    test.assertEqual(aad.hex(), expected["aad"])
    test.assertEqual(wrap_key.hex(), expected["wrap_key"])
    test.assertEqual(ciphertext.hex(), expected["aead_ciphertext_and_tag"])
    test.assertEqual(envelope.hex(), expected["envelope_bytes"])
    test.assertEqual(aead_decrypt(ciphertext, wrap_key, nonce, aad), plaintext)


def _run_export_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    inputs = case["inputs"]
    expected = case["expected"]

    salt = bytes.fromhex(inputs["argon_salt"])
    outer_nonce = bytes.fromhex(inputs["outer_nonce"])
    export_file_key = bytes.fromhex(inputs["export_file_key"])
    memory_kib = int(inputs["argon_memory_kib"])
    iterations = int(inputs["argon_iterations"])

    outer_header = build_export_outer_header(
        argon_memory_kib=memory_kib, argon_iterations=iterations,
        argon_parallelism=int(inputs["argon_parallelism"]),
        argon_salt=salt, outer_nonce=outer_nonce,
    )
    wrap_key = derive_export_wrap_key(
        passphrase=inputs["passphrase"], argon_salt=salt,
        memory_kib=memory_kib, iterations=iterations,
    )
    wrap_aad = build_export_wrap_aad(inputs["vault_id"])
    wrapped_key_envelope = aead_encrypt(export_file_key, wrap_key, outer_nonce, wrap_aad)

    if "expected_error" in expected:
        decrypt_wrap_key = wrap_key
        decrypt_envelope = wrapped_key_envelope
        if "decrypt_passphrase_override" in inputs:
            decrypt_wrap_key = derive_export_wrap_key(
                passphrase=inputs["decrypt_passphrase_override"],
                argon_salt=salt, memory_kib=memory_kib, iterations=iterations,
            )
        tamper = case.get("tamper", {})
        if "wrapped_key_byte_xor" in tamper:
            spec = tamper["wrapped_key_byte_xor"]
            buf = bytearray(wrapped_key_envelope)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            decrypt_envelope = bytes(buf)
        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_envelope, decrypt_wrap_key, outer_nonce, wrap_aad)
        return

    test.assertEqual(outer_header.hex(), expected["outer_header_bytes"])
    test.assertEqual(wrap_key.hex(), expected["wrap_key"])
    test.assertEqual(wrap_aad.hex(), expected["wrap_aad"])
    test.assertEqual(wrapped_key_envelope.hex(), expected["wrapped_key_envelope"])
    test.assertEqual(
        aead_decrypt(wrapped_key_envelope, wrap_key, outer_nonce, wrap_aad),
        export_file_key,
    )


# Map filename → runner. Every primitive file has a runner now; T2.4
# adds the PHP twin so the same JSON files exercise both runtimes.
_RUNNERS = {
    "manifest_v1.json": _run_manifest_case,
    "chunk_v1.json": _run_chunk_case,
    "header_v1.json": _run_header_case,
    "recovery_envelope_v1.json": _run_recovery_case,
    "device_grant_v1.json": _run_device_grant_case,
    "export_bundle_v1.json": _run_export_case,
}


class VaultV1VectorsTests(unittest.TestCase):
    """Discovery + per-primitive verification harness."""

    def test_vectors_directory_exists(self) -> None:
        self.assertTrue(
            os.path.isdir(VECTORS_DIR),
            f"missing {VECTORS_DIR} — see T0.4 in VAULT-progress.md",
        )

    def test_all_primitive_files_present(self) -> None:
        for name in EXPECTED_FILES:
            with self.subTest(file=name):
                self.assertTrue(
                    os.path.isfile(os.path.join(VECTORS_DIR, name)),
                    f"expected stub file {name} under {VECTORS_DIR}",
                )

    def test_files_parse_as_json_arrays(self) -> None:
        for name in EXPECTED_FILES:
            with self.subTest(file=name):
                cases = _load_cases(name)
                self.assertIsInstance(cases, list)

    def test_case_shape_matches_a18(self) -> None:
        for name in EXPECTED_FILES:
            cases = _load_cases(name)
            for index, case in enumerate(cases):
                with self.subTest(file=name, index=index, case=case.get("name", "?")):
                    _validate_case_shape(name, index, case)

    def test_manifest_v1_cases_round_trip_byte_exact(self) -> None:
        cases = _load_cases("manifest_v1.json")
        self.assertGreaterEqual(len(cases), 5,
            "T2.2 requires at least 5 manifest cases (happy / tombstone / "
            "op-log-with-archived / tampered / wrong-AAD)")
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_manifest_case(self, case)

    def test_chunk_v1_cases(self) -> None:
        cases = _load_cases("chunk_v1.json")
        self.assertGreaterEqual(len(cases), 3, "T2.3 requires ≥ 3 chunk cases")
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_chunk_case(self, case)

    def test_header_v1_cases(self) -> None:
        cases = _load_cases("header_v1.json")
        self.assertGreaterEqual(len(cases), 3, "T2.3 requires ≥ 3 header cases")
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_header_case(self, case)

    def test_recovery_envelope_v1_cases(self) -> None:
        cases = _load_cases("recovery_envelope_v1.json")
        self.assertGreaterEqual(len(cases), 3, "T2.3 requires ≥ 3 recovery cases")
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_recovery_case(self, case)

    def test_device_grant_v1_cases(self) -> None:
        cases = _load_cases("device_grant_v1.json")
        self.assertGreaterEqual(len(cases), 3, "T2.3 requires ≥ 3 device-grant cases")
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_device_grant_case(self, case)

    def test_export_bundle_v1_cases(self) -> None:
        cases = _load_cases("export_bundle_v1.json")
        self.assertGreaterEqual(len(cases), 3, "T2.3 requires ≥ 3 export cases")
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_export_case(self, case)

    def test_total_loaded_count_reported(self) -> None:
        total = sum(len(_load_cases(name)) for name in EXPECTED_FILES)
        # Stdout for human visibility when running pytest -s.
        print(
            f"\n[vault-v1 vectors] {total} vectors loaded across {len(EXPECTED_FILES)} files "
            f"({len(_RUNNERS)} primitive runner(s) wired)"
        )


if __name__ == "__main__":
    unittest.main()
