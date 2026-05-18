"""Vault v1 cross-platform test vectors harness.

Discovers JSON case files under ``tests/protocol/vault-v1/`` and exercises each
case against the desktop Python primitives (``desktop/src/vault_crypto.py``).
The PHP twin (``server/src/Crypto/VaultCrypto.php``, T2.4) runs the same JSON
files via PHPUnit so a divergence between the two runtimes breaks the build.

Schema lock: T0 §A18 in
``temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md``.
Byte-format reference: ``docs/protocol/vault-v1-formats.md``.

Files filled so far:
    root_v1.json + shard_v1.json (2026-05-16) — manifest sharding pair.
    See ``vault-v1/README.md`` for the broader vector catalog.
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

from src.vault.crypto import (  # noqa: E402
    VaultFormatVersionUnsupported,
    aead_decrypt,
    aead_encrypt,
    assert_supported_format_version,
    build_chunk_aad,
    build_chunk_envelope,
    build_device_grant_aad,
    build_device_grant_envelope,
    build_export_outer_header,
    build_export_wrap_aad,
    build_header_aad,
    build_header_envelope,
    build_recovery_aad,
    build_recovery_envelope,
    build_root_aad,
    build_root_envelope,
    build_shard_aad,
    build_shard_envelope,
    derive_content_fingerprint_key,
    derive_device_grant_wrap_key,
    derive_export_wrap_key,
    derive_recovery_wrap_key,
    derive_subkey,
    make_content_fingerprint,
)

VECTORS_DIR = os.path.join(os.path.dirname(__file__), "vault-v1")

EXPECTED_FILES = (
    "root_v1.json",
    "shard_v1.json",
    "chunk_v1.json",
    "header_v1.json",
    "recovery_envelope_v1.json",
    "device_grant_v1.json",
    "export_bundle_v1.json",
    "content_fingerprint_v1.json",
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


def _run_root_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    """Verify one root_v1.json case end-to-end against vault_crypto.

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
    plaintext = base64.b64decode(inputs["root_plaintext"])

    aad = build_root_aad(
        vault_id=inputs["vault_id"],
        root_revision=int(inputs["root_revision"]),
        parent_root_revision=int(inputs["parent_root_revision"]),
        author_device_id=inputs["author_device_id"],
    )
    subkey = derive_subkey("dc-vault-v1/root", master_key)
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    envelope = build_root_envelope(
        vault_id=inputs["vault_id"],
        root_revision=int(inputs["root_revision"]),
        parent_root_revision=int(inputs["parent_root_revision"]),
        author_device_id=inputs["author_device_id"],
        nonce=nonce,
        aead_ciphertext_and_tag=ciphertext,
    )

    if "expected_error" in expected:
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
            # Root envelope plaintext header is 85 bytes (1+12+8+8+32+24);
            # offset 85 is the AEAD ciphertext start.
            decrypt_ct = decrypt_envelope[85:]

        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])

        if expected["expected_error"] == "vault_format_version_unsupported":
            with test.assertRaises(VaultFormatVersionUnsupported):
                assert_supported_format_version(decrypt_envelope, kind="root")
            return

        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_ct, subkey, nonce, decrypt_aad)
        return

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

    recovered = aead_decrypt(ciphertext, subkey, nonce, aad)
    test.assertEqual(recovered, plaintext, "round-trip plaintext mismatch")


def _run_shard_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    """Verify one shard_v1.json case end-to-end against vault_crypto.

    Same pattern as ``_run_root_case`` but for shard envelopes — the
    envelope plaintext header is 115 bytes (extra 30-byte
    ``remote_folder_id`` after the vault_id) so the ciphertext starts at
    offset 115.
    """
    inputs = case["inputs"]
    expected = case["expected"]

    master_key = bytes.fromhex(inputs["vault_master_key"])
    nonce = bytes.fromhex(inputs["nonce"])
    plaintext = base64.b64decode(inputs["shard_plaintext"])

    aad = build_shard_aad(
        vault_id=inputs["vault_id"],
        remote_folder_id=inputs["remote_folder_id"],
        shard_revision=int(inputs["shard_revision"]),
        parent_shard_revision=int(inputs["parent_shard_revision"]),
        author_device_id=inputs["author_device_id"],
    )
    subkey = derive_subkey("dc-vault-v1/shard", master_key)
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    envelope = build_shard_envelope(
        vault_id=inputs["vault_id"],
        remote_folder_id=inputs["remote_folder_id"],
        shard_revision=int(inputs["shard_revision"]),
        parent_shard_revision=int(inputs["parent_shard_revision"]),
        author_device_id=inputs["author_device_id"],
        nonce=nonce,
        aead_ciphertext_and_tag=ciphertext,
    )

    if "expected_error" in expected:
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
            decrypt_ct = decrypt_envelope[115:]

        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])

        if expected["expected_error"] == "vault_format_version_unsupported":
            with test.assertRaises(VaultFormatVersionUnsupported):
                assert_supported_format_version(decrypt_envelope, kind="shard")
            return

        with test.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(decrypt_ct, subkey, nonce, decrypt_aad)
        return

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
        # Chunk envelope = nonce(24) || ciphertext_and_tag — no
        # format-version byte (formats §11.1; chunk format is pinned by
        # the ``ch_v1_…`` chunk_id namespace).
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
        decrypt_envelope = envelope
        decrypt_ct = ciphertext
        if "envelope_byte_xor" in tamper:
            spec = tamper["envelope_byte_xor"]
            buf = bytearray(envelope)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            decrypt_envelope = bytes(buf)
            decrypt_ct = decrypt_envelope[1 + 12 + 8 + 24:]   # skip plaintext header + nonce
        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])
        if expected["expected_error"] == "vault_format_version_unsupported":
            with test.assertRaises(VaultFormatVersionUnsupported):
                assert_supported_format_version(decrypt_envelope, kind="header")
            return
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
        decrypt_envelope = envelope
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
            decrypt_envelope = bytes(buf)
            # Recovery envelope: 1+12+30+16+24 = 83 bytes plaintext header,
            # then ciphertext_and_tag.
            decrypt_ct = decrypt_envelope[83:]
        if expected["expected_error"] == "vault_format_version_unsupported":
            with test.assertRaises(VaultFormatVersionUnsupported):
                assert_supported_format_version(decrypt_envelope, kind="recovery")
            return
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
        decrypt_envelope = envelope
        decrypt_ct = ciphertext
        if "envelope_byte_xor" in tamper:
            spec = tamper["envelope_byte_xor"]
            buf = bytearray(envelope)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            decrypt_envelope = bytes(buf)
            # Plaintext header: 1 + 12 + 30 + 32 = 75 bytes; nonce: 24
            decrypt_ct = decrypt_envelope[75 + 24:]
        if "aad_override" in tamper:
            decrypt_aad = bytes.fromhex(tamper["aad_override"])
        if expected["expected_error"] == "vault_format_version_unsupported":
            with test.assertRaises(VaultFormatVersionUnsupported):
                assert_supported_format_version(decrypt_envelope, kind="device_grant")
            return
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
        decrypt_outer = outer_header
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
        if "envelope_byte_xor" in tamper:
            # Format-version tamper targets the outer header (after the
            # 4-byte ``DCVE`` magic, byte 4 is format_version).
            spec = tamper["envelope_byte_xor"]
            buf = bytearray(outer_header)
            buf[int(spec["offset"])] ^= int(spec["xor"], 16)
            decrypt_outer = bytes(buf)
        if expected["expected_error"] == "vault_format_version_unsupported":
            with test.assertRaises(VaultFormatVersionUnsupported):
                assert_supported_format_version(
                    decrypt_outer, kind="export_outer", offset=4,
                )
            return
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


def _run_content_fingerprint_case(test: unittest.TestCase, case: dict[str, Any]) -> None:
    """Verify a content_fingerprint_v1.json case (formats §10.2).

    Review §7.H2: optional ``expected.diverges_from_b64`` field pins
    that the computed fingerprint MUST NOT equal the named
    base64-encoded fingerprint. Used by the
    "different-master-key" / "different-plaintext" negative cases to
    pin that the HMAC keying actually mixes both inputs — a future
    regression that drops either would silently widen the dedup
    collision surface, which a single positive-case vector cannot
    catch.
    """
    inputs = case["inputs"]
    expected = case["expected"]
    master_key = bytes.fromhex(inputs["vault_master_key"])
    plaintext_sha256 = bytes.fromhex(inputs["plaintext_sha256"])

    subkey = derive_content_fingerprint_key(master_key)
    fingerprint = make_content_fingerprint(subkey, plaintext_sha256)

    test.assertEqual(subkey.hex(), expected["subkey"])
    test.assertEqual(fingerprint, expected["fingerprint_b64"])

    diverges = expected.get("diverges_from_b64")
    if diverges is not None:
        test.assertNotEqual(
            fingerprint, diverges,
            f"{case['name']}: fingerprint must differ from {diverges!r} "
            "(HMAC keying / plaintext binding regression)",
        )


# Map filename → runner. Every primitive file has a runner now; T2.4
# adds the PHP twin so the same JSON files exercise both runtimes.
_RUNNERS = {
    "root_v1.json": _run_root_case,
    "shard_v1.json": _run_shard_case,
    "chunk_v1.json": _run_chunk_case,
    "header_v1.json": _run_header_case,
    "recovery_envelope_v1.json": _run_recovery_case,
    "device_grant_v1.json": _run_device_grant_case,
    "export_bundle_v1.json": _run_export_case,
    "content_fingerprint_v1.json": _run_content_fingerprint_case,
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

    # F-T20: per-primitive expected case-name pins. These are *exact*
    # frozensets, not the prior ``assertGreaterEqual(N)`` floor. The
    # floor caught "someone deleted enough cases to fall below N" but
    # silently accepted "someone deleted a specific case and added an
    # unrelated one to keep N steady". Pinning exact names traps the
    # second pattern. To add a vector, append it to the JSON file AND
    # add the name here in the same commit; the test failure message
    # tells you exactly which side is out of sync.
    EXPECTED_ROOT_V1_CASES = frozenset({
        "root-v1-genesis-happy-path",
        "root-v1-multi-folder",
        "root-v1-folder-removed",
        "root-v1-tampered-ciphertext",
        "root-v1-wrong-aad-revision",
        "root-v1-format-version-bumped",
    })
    EXPECTED_SHARD_V1_CASES = frozenset({
        "shard-v1-genesis-happy-path",
        "shard-v1-tombstone-only",
        "shard-v1-empty-newly-added-folder",
        "shard-v1-tampered-ciphertext",
        "shard-v1-wrong-aad-folder-id",
        "shard-v1-format-version-bumped",
    })
    EXPECTED_CHUNK_V1_CASES = frozenset({
        # Review §7.M5 — per-field AAD-tamper vector for chunk_index.
        "chunk-v1-aad-chunk-index-flipped",
        "chunk-v1-small",
        "chunk-v1-medium",
        "chunk-v1-tampered-ciphertext",
    })
    EXPECTED_HEADER_V1_CASES = frozenset({
        "header-v1-genesis",
        "header-v1-revision-n",
        "header-v1-tampered-revision-in-aad",
        "header-v1-format-version-bumped",
    })
    EXPECTED_RECOVERY_V1_CASES = frozenset({
        "recovery-v1-kit-plus-passphrase-happy",
        "recovery-v1-wrong-passphrase",
        "recovery-v1-tampered-ciphertext",
        "recovery-v1-format-version-bumped",
    })
    EXPECTED_DEVICE_GRANT_V1_CASES = frozenset({
        "device-grant-v1-read-only",
        "device-grant-v1-browse-upload",
        "device-grant-v1-sync",
        "device-grant-v1-admin",
        "device-grant-v1-tampered-claimant-id-in-aad",
        "device-grant-v1-format-version-bumped",
    })
    EXPECTED_EXPORT_BUNDLE_V1_CASES = frozenset({
        "export-v1-outer-and-wrapped-key",
        "export-v1-wrong-passphrase",
        "export-v1-tampered-wrapped-key",
        "export-v1-format-version-bumped",
    })
    EXPECTED_CONTENT_FINGERPRINT_V1_CASES = frozenset({
        "content-fingerprint-v1-happy-path",
        "content-fingerprint-v1-empty-plaintext",
        # Review §7.H2 — divergence negatives pin that the HMAC keying
        # mixes both master_key and plaintext_sha256.
        "content-fingerprint-v1-different-master-key-diverges",
        "content-fingerprint-v1-different-plaintext-diverges",
    })

    def _assert_case_names(
        self, file_name: str, cases: list, expected: frozenset,
    ) -> None:
        actual = {case["name"] for case in cases}
        self.assertEqual(
            actual, expected,
            f"F-T20: {file_name} case-name set drifted. "
            f"Missing in JSON: {expected - actual!r}; "
            f"unexpected in JSON: {actual - expected!r}. "
            "Update both the JSON file and the EXPECTED_*_CASES "
            "frozenset in the same commit.",
        )

    def test_root_v1_cases_round_trip_byte_exact(self) -> None:
        cases = _load_cases("root_v1.json")
        self._assert_case_names(
            "root_v1.json", cases, self.EXPECTED_ROOT_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_root_case(self, case)

    def test_shard_v1_cases_round_trip_byte_exact(self) -> None:
        cases = _load_cases("shard_v1.json")
        self._assert_case_names(
            "shard_v1.json", cases, self.EXPECTED_SHARD_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_shard_case(self, case)

    def test_chunk_v1_cases(self) -> None:
        cases = _load_cases("chunk_v1.json")
        self._assert_case_names(
            "chunk_v1.json", cases, self.EXPECTED_CHUNK_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_chunk_case(self, case)

    def test_header_v1_cases(self) -> None:
        cases = _load_cases("header_v1.json")
        self._assert_case_names(
            "header_v1.json", cases, self.EXPECTED_HEADER_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_header_case(self, case)

    def test_recovery_envelope_v1_cases(self) -> None:
        cases = _load_cases("recovery_envelope_v1.json")
        self._assert_case_names(
            "recovery_envelope_v1.json", cases,
            self.EXPECTED_RECOVERY_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_recovery_case(self, case)

    def test_device_grant_v1_cases(self) -> None:
        cases = _load_cases("device_grant_v1.json")
        self._assert_case_names(
            "device_grant_v1.json", cases,
            self.EXPECTED_DEVICE_GRANT_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_device_grant_case(self, case)

    def test_export_bundle_v1_cases(self) -> None:
        cases = _load_cases("export_bundle_v1.json")
        self._assert_case_names(
            "export_bundle_v1.json", cases,
            self.EXPECTED_EXPORT_BUNDLE_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_export_case(self, case)

    def test_content_fingerprint_v1_cases(self) -> None:
        cases = _load_cases("content_fingerprint_v1.json")
        self._assert_case_names(
            "content_fingerprint_v1.json", cases,
            self.EXPECTED_CONTENT_FINGERPRINT_V1_CASES,
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                _run_content_fingerprint_case(self, case)

    def test_total_loaded_count_reported(self) -> None:
        total = sum(len(_load_cases(name)) for name in EXPECTED_FILES)
        # Stdout for human visibility when running pytest -s.
        print(
            f"\n[vault-v1 vectors] {total} vectors loaded across {len(EXPECTED_FILES)} files "
            f"({len(_RUNNERS)} primitive runner(s) wired)"
        )


if __name__ == "__main__":
    unittest.main()
