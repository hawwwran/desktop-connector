"""Vault-v1 cryptographic primitives.

Locked-byte-exact construction per ``docs/protocol/vault-v1-formats.md``.
This module is the single Python implementation of the four primitives
the rest of the vault code calls; both controllers and the cross-platform
test-vector harness import from here so any byte-shape change is caught
in one spot.

Primitives
----------

- ``derive_subkey(label, master_key, length=32)`` — HKDF-SHA256 per §4.1.
  Salt is the empty string (32 SHA-256 zero bytes per RFC 5869 §2.2),
  IKM is the 32-byte vault master key, info is the UTF-8 label.

- ``aead_encrypt(plaintext, key, nonce, aad)`` /
  ``aead_decrypt(ciphertext_and_tag, key, nonce, aad)`` —
  XChaCha20-Poly1305 (IETF) per §2. Tag is appended to ciphertext.

- ``argon2id_kdf(passphrase, salt, output_length=32)`` — libsodium
  Argon2id with the v1-locked params from §12.2 (m=128 MiB, t=4, p=1).
  Passphrase is UTF-8-NFC normalized before hashing.

Plus per-envelope AAD + frame builders (manifest, chunk, header, op-log
segment, recovery, device grant, export bundle outer / wrap / record)
defined inline below — each one mirrors the corresponding §6 layout.

Implementations
---------------

PyNaCl (libsodium binding) for AEAD + Argon2id; ``cryptography``'s HKDF
for subkey derivation. Both are already pinned in
``desktop/requirements.txt``. The PHP twin lives in
``server/src/Crypto/VaultCrypto.php`` and exercises the same JSON test
vectors (``tests/protocol/vault-v1/``) so a divergence in either runtime
breaks the build.

API stability + test injection (T2.5)
-------------------------------------

The module-level functions are the canonical surface — production code
imports them directly. To support tests that need to stub the crypto
without flushing the real CSPRNG / Argon2id every call, the module also
exposes:

- ``VaultCrypto`` — a ``typing.Protocol`` describing the public surface.
  Use it as a type hint when a caller wants to accept either the real
  implementation or a fake.

- ``DefaultVaultCrypto`` — an instance whose attributes bind to the
  module functions. Production code that prefers dependency injection
  passes ``DefaultVaultCrypto`` by default and tests pass a stub.

Adding new methods to the surface is fine; renames or signature
changes break the PHP twin and the JSON vectors in lockstep, so tread
carefully.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from typing import Protocol, runtime_checkable

import nacl.bindings
import nacl.pwhash.argon2id
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# ---------------------------------------------------------------- constants

# v1 Argon2id parameters per vault-v1-formats.md §12.2.
ARGON2ID_MEMORY_KIB = 131_072        # 128 MiB
ARGON2ID_ITERATIONS = 4
ARGON2ID_PARALLELISM = 1              # libsodium fixes parallelism at 1
ARGON2ID_SALT_BYTES = 16

XCHACHA20_KEY_BYTES = 32
XCHACHA20_NONCE_BYTES = 24
POLY1305_TAG_BYTES = 16

MASTER_KEY_BYTES = 32


# ---------------------------------------------------------------- HKDF

def derive_subkey(label: str, master_key: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 subkey derivation per formats §4.1.

    Construction:
        subkey = HKDF-SHA256(
            salt = 32 SHA-256 zero bytes (RFC 5869 §2.2 default),
            ikm  = master_key,
            info = label.encode("utf-8"),
            L    = length,
        )

    Args:
        label: UTF-8 string label, e.g. ``"dc-vault-v1/manifest"``.
        master_key: 32-byte vault master key.
        length: Output length in bytes. Default 32 (most subkeys).

    Returns:
        ``length`` bytes of derived key material.

    Raises:
        ValueError: master_key is not 32 bytes, or length <= 0.
    """
    if len(master_key) != MASTER_KEY_BYTES:
        raise ValueError(f"master_key must be {MASTER_KEY_BYTES} bytes; got {len(master_key)}")
    if length <= 0:
        raise ValueError(f"length must be positive; got {length}")
    # cryptography's HKDF treats salt=None as "HashLen zero bytes" which is
    # exactly RFC 5869 §2.2's default. We pass it explicitly as 32 zero
    # bytes so the construction is unambiguous across implementations and
    # the PHP twin can match without depending on its lib's defaulting.
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=b"\x00" * 32,
        info=label.encode("utf-8"),
    )
    return hkdf.derive(master_key)


# ---------------------------------------------------------------- AEAD

def aead_encrypt(plaintext: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes:
    """XChaCha20-Poly1305-IETF encrypt. Returns ``ciphertext || tag`` (Poly1305 tag is the trailing 16 bytes).

    Args:
        plaintext: Bytes to encrypt. May be empty.
        key: 32-byte AEAD key (typically a HKDF subkey).
        nonce: 24-byte nonce. CSPRNG-supplied per envelope; never reused
            with the same key.
        aad: Associated authenticated data per the formats §6 AAD layout
            for the relevant envelope type.

    Returns:
        ``len(plaintext) + 16`` bytes (ciphertext concatenated with the
        16-byte Poly1305 tag).

    Raises:
        ValueError: key is not 32 bytes or nonce is not 24 bytes.
    """
    if len(key) != XCHACHA20_KEY_BYTES:
        raise ValueError(f"key must be {XCHACHA20_KEY_BYTES} bytes; got {len(key)}")
    if len(nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(nonce)}")
    return nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        plaintext, aad, nonce, key
    )


def aead_decrypt(ciphertext_and_tag: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes:
    """XChaCha20-Poly1305-IETF decrypt. Raises on AEAD verification failure.

    Args:
        ciphertext_and_tag: ``ciphertext || 16-byte tag`` from a matching
            ``aead_encrypt`` call.
        key, nonce, aad: must exactly match the encrypt-side values.

    Returns:
        The original plaintext bytes.

    Raises:
        ValueError: key/nonce wrong length, or ciphertext_and_tag too short
            to contain a tag.
        nacl.exceptions.CryptoError: AEAD verification failed (wrong key,
            nonce, AAD, or tampered ciphertext / tag).
    """
    if len(key) != XCHACHA20_KEY_BYTES:
        raise ValueError(f"key must be {XCHACHA20_KEY_BYTES} bytes; got {len(key)}")
    if len(nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(nonce)}")
    if len(ciphertext_and_tag) < POLY1305_TAG_BYTES:
        raise ValueError(
            f"ciphertext_and_tag must be at least {POLY1305_TAG_BYTES} bytes (the tag); "
            f"got {len(ciphertext_and_tag)}"
        )
    return nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
        ciphertext_and_tag, aad, nonce, key
    )


# ---------------------------------------------------------------- Argon2id

def argon2id_kdf(
    passphrase: str,
    salt: bytes,
    output_length: int = 32,
    *,
    memory_kib: int = ARGON2ID_MEMORY_KIB,
    iterations: int = ARGON2ID_ITERATIONS,
) -> bytes:
    """Argon2id password-based KDF with the v1-locked parameters.

    Defaults to the formats §12.2 lock (m=128 MiB, t=4, p=1, output=32).
    Callers can override ``memory_kib`` / ``iterations`` for export
    bundles whose envelope carries its own param set; the recovery flow
    uses the defaults.

    Passphrase is **NFC-normalized** before encoding so users typing
    accents / emoji on different keyboards get the same key.

    libsodium fixes ``parallelism = 1`` for Argon2id; the formats spec
    locks p=1 to match. There's no ``parallelism`` argument here because
    no other value is reachable.

    Args:
        passphrase: User passphrase (recovery or export). Empty allowed
            for argument-checking tests, though weak passwords are a
            client-layer concern.
        salt: 16-byte salt. Stored alongside the wrapped envelope so the
            same passphrase reproduces the wrap key on recovery.
        output_length: Bytes of derived key material. 32 for AEAD subkeys.
        memory_kib: Argon2id memory cost in KiB. Defaults to 131072 (128 MiB).
        iterations: Argon2id time cost (passes). Defaults to 4.

    Returns:
        ``output_length`` bytes of derived key material.

    Raises:
        ValueError: salt is not 16 bytes, output_length / memory_kib /
            iterations is non-positive.
    """
    if len(salt) != ARGON2ID_SALT_BYTES:
        raise ValueError(f"salt must be {ARGON2ID_SALT_BYTES} bytes; got {len(salt)}")
    if output_length <= 0:
        raise ValueError(f"output_length must be positive; got {output_length}")
    if memory_kib <= 0:
        raise ValueError(f"memory_kib must be positive; got {memory_kib}")
    if iterations <= 0:
        raise ValueError(f"iterations must be positive; got {iterations}")

    pw_bytes = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    memlimit_bytes = memory_kib * 1024
    return nacl.pwhash.argon2id.kdf(
        size=output_length,
        password=pw_bytes,
        salt=salt,
        opslimit=iterations,
        memlimit=memlimit_bytes,
    )


# ---------------------------------------------------------------- ID normalization

def normalize_vault_id(vault_id: str) -> str:
    """Strip dashes, uppercase. Accepts ``"ABCD-2345-WXYZ"`` or ``"abcd2345wxyz"``;
    returns the 12-char canonical form. Does not validate the alphabet —
    callers that need a hard regex check (controllers) own that.

    AAD encodings always use the canonical form (12 UTF-8 bytes).
    """
    return vault_id.replace("-", "").upper()


# ---------------------------------------------------------------- Manifest AAD + envelope (formats §6.1, §10)

# Schema string locked in formats §6.1; never trim, never re-case.
_MANIFEST_AAD_SCHEMA = b"dc-vault-manifest-v1"   # 20 bytes


def build_manifest_aad(
    vault_id: str,
    revision: int,
    parent_revision: int,
    author_device_id: str,
) -> bytes:
    """Construct the 80-byte manifest AAD per formats §6.1.

    Layout (deterministic concatenation of fixed-length fields):
        utf8("dc-vault-manifest-v1")        # 20 bytes
        || vault_id_bytes                    # 12 bytes (canonical, undashed, uppercase)
        || revision_be64                     # 8 bytes
        || parent_revision_be64              # 8 bytes
        || author_device_id_bytes            # 32 bytes (UTF-8 hex)
                                              total: 80 bytes

    Args:
        vault_id: Canonical 12-char base32 form (dashes stripped, uppercase).
        revision: New manifest revision number (uint64).
        parent_revision: Predecessor revision; 0 for genesis.
        author_device_id: 32-char lowercase hex device id.

    Returns:
        Exactly 80 bytes of AAD, ready to pass to ``aead_encrypt``.

    Raises:
        ValueError: Field lengths off-spec (vault_id !12, author_device_id !32),
            or revisions don't fit in u64.
    """
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    if len(author_device_id) != 32:
        raise ValueError(f"author_device_id must be 32 hex chars; got {len(author_device_id)}")
    if revision < 0 or revision > 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError(f"revision out of u64 range: {revision}")
    if parent_revision < 0 or parent_revision > 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError(f"parent_revision out of u64 range: {parent_revision}")

    return (
        _MANIFEST_AAD_SCHEMA
        + canonical.encode("ascii")
        + revision.to_bytes(8, "big")
        + parent_revision.to_bytes(8, "big")
        + author_device_id.encode("ascii")
    )


def build_manifest_envelope(
    *,
    vault_id: str,
    revision: int,
    parent_revision: int,
    author_device_id: str,
    nonce: bytes,
    aead_ciphertext_and_tag: bytes,
    format_version: int = 1,
) -> bytes:
    """Assemble the manifest envelope wire bytes per formats §10.1.

    Layout:
        format_version_u8                    # 1 byte (0x01 in v1)
        || vault_id_bytes                    # 12 bytes
        || revision_be64                     # 8 bytes
        || parent_revision_be64              # 8 bytes
        || author_device_id_bytes            # 32 bytes
        || nonce                             # 24 bytes
        || aead_ciphertext_and_tag           # variable (len(plaintext) + 16)
                                              total: 85 + N bytes

    The plaintext header (85 bytes) is deterministic from the typed
    inputs. The relay parses the first 61 bytes for CAS checks and
    never decrypts the body.

    Note that ``format_version`` is plaintext-only (per T0 §A3); it is
    NOT in the AAD, so old clients can refuse a higher version before
    attempting decryption.
    """
    if not (0 <= format_version <= 0xFF):
        raise ValueError(f"format_version must fit in u8; got {format_version}")
    if len(nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(nonce)}")

    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    if len(author_device_id) != 32:
        raise ValueError(f"author_device_id must be 32 hex chars; got {len(author_device_id)}")

    return (
        bytes([format_version])
        + canonical.encode("ascii")
        + revision.to_bytes(8, "big")
        + parent_revision.to_bytes(8, "big")
        + author_device_id.encode("ascii")
        + nonce
        + aead_ciphertext_and_tag
    )


# ---------------------------------------------------------------- Chunk AAD + envelope (formats §6.2, §11)

_CHUNK_AAD_SCHEMA = b"dc-vault-chunk-v1"   # 17 bytes


def build_chunk_aad(
    vault_id: str,
    remote_folder_id: str,
    file_id: str,
    file_version_id: str,
    chunk_index: int,
    chunk_plaintext_size: int,
) -> bytes:
    """Construct the 135-byte chunk AAD per formats §6.2."""
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    for name, value in (
        ("remote_folder_id", remote_folder_id),
        ("file_id", file_id),
        ("file_version_id", file_version_id),
    ):
        if len(value) != 30:
            raise ValueError(f"{name} must be 30 bytes; got {len(value)}")
    return (
        _CHUNK_AAD_SCHEMA
        + canonical.encode("ascii")
        + remote_folder_id.encode("ascii")
        + file_id.encode("ascii")
        + file_version_id.encode("ascii")
        + chunk_index.to_bytes(8, "big")
        + chunk_plaintext_size.to_bytes(8, "big")
    )


def build_chunk_envelope(*, nonce: bytes, aead_ciphertext_and_tag: bytes) -> bytes:
    """Chunk envelope per formats §11.1: ``nonce(24) || aead_ciphertext_and_tag``.

    No plaintext header — the chunk's identifying metadata lives in AAD only.
    """
    if len(nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(nonce)}")
    return nonce + aead_ciphertext_and_tag


_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"


def _base32_lower_encode_15_bytes(raw: bytes) -> str:
    """Encode 15 bytes as 24 base32 lowercase chars (matching A19 / file/folder ids)."""
    if len(raw) != 15:
        raise ValueError(f"expected 15 bytes; got {len(raw)}")
    out = []
    bits = 0
    buf = 0
    for byte in raw:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32_LOWER[(buf >> bits) & 0x1f])
    return "".join(out[:24])


def derive_chunk_id_key(master_key: bytes) -> bytes:
    """Per-vault HMAC key for keyed chunk-id derivation."""
    return derive_subkey("dc-vault-v1/chunk-id", bytes(master_key))


def derive_content_fingerprint_key(master_key: bytes) -> bytes:
    """Per-vault HMAC key for keyed file content fingerprints (§04 §A7).

    Used to detect "this upload's bytes match an existing version" so the
    upload pipeline can short-circuit and PUT zero new chunks. Different
    vaults produce different fingerprints for identical bytes — the
    fingerprint never leaks plaintext-level identity outside the vault.
    """
    return derive_subkey("dc-vault-v1/content-fp", bytes(master_key))


def make_chunk_id(
    chunk_id_key: bytes,
    plaintext: bytes,
    file_version_id: str,
    chunk_index: int,
) -> str:
    """Per-(version, position) chunk id: ``ch_v1_<24 lowercase base32>``.

    Includes ``file_version_id`` and ``chunk_index`` in the HMAC input so
    the chunk_id uniquely identifies the (version, position) pair —
    chunks at different positions in the same file get distinct ids,
    and re-encryption under a fresh ``file_version_id`` produces fresh
    ids (matching what ``build_chunk_aad`` binds in the AEAD AAD).
    """
    if not isinstance(file_version_id, str) or len(file_version_id) != 30:
        raise ValueError("file_version_id must be a 30-char string")
    digest = hmac.new(
        bytes(chunk_id_key),
        hashlib.sha256(plaintext).digest()
        + file_version_id.encode("ascii")
        + int(chunk_index).to_bytes(8, "big"),
        hashlib.sha256,
    ).digest()
    return "ch_v1_" + _base32_lower_encode_15_bytes(digest[:15])


def derive_chunk_nonce_key(master_key: bytes) -> bytes:
    """Per-vault HMAC key for deterministic chunk-nonce derivation (T6.5)."""
    return derive_subkey("dc-vault-v1/chunk-nonce", bytes(master_key))


def make_chunk_nonce(
    nonce_key: bytes,
    plaintext: bytes,
    file_version_id: str,
    chunk_index: int,
) -> bytes:
    """24-byte deterministic XChaCha20-Poly1305 nonce.

    Re-encrypting the same chunk on the same vault produces a
    byte-identical envelope, which is what makes T6.5 resume "no chunk
    uploaded twice" land cleanly: the relay's per-chunk PUT is
    idempotent on hash match, so a re-encrypt + retry hits 200 OK.
    Different vaults derive different nonce keys via the master key,
    and different ``(file_version_id, chunk_index)`` tuples already
    produce different chunk_ids — so the deterministic nonce never
    collides within a single chunk_id slot.
    """
    if not isinstance(file_version_id, str) or len(file_version_id) != 30:
        raise ValueError("file_version_id must be a 30-char string")
    digest = hmac.new(
        bytes(nonce_key),
        hashlib.sha256(plaintext).digest()
        + file_version_id.encode("ascii")
        + int(chunk_index).to_bytes(8, "big"),
        hashlib.sha256,
    ).digest()
    return digest[:XCHACHA20_NONCE_BYTES]


def make_content_fingerprint(content_fp_key: bytes, plaintext_sha256: bytes) -> str:
    """Keyed file fingerprint, hex string. ``plaintext_sha256`` is the
    SHA-256 digest of the full file plaintext.

    Compared via ``==`` against the fingerprint stored on existing
    versions to short-circuit a redundant upload of identical bytes.
    """
    if len(plaintext_sha256) != 32:
        raise ValueError("plaintext_sha256 must be 32 bytes")
    digest = hmac.new(
        bytes(content_fp_key),
        plaintext_sha256,
        hashlib.sha256,
    ).digest()
    return digest.hex()


# ---------------------------------------------------------------- Header AAD + envelope (formats §6.3, §9)

_HEADER_AAD_SCHEMA = b"dc-vault-header-v1"   # 18 bytes


def build_header_aad(vault_id: str, header_revision: int) -> bytes:
    """Construct the 38-byte header AAD per formats §6.3."""
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    return (
        _HEADER_AAD_SCHEMA
        + canonical.encode("ascii")
        + header_revision.to_bytes(8, "big")
    )


def build_header_envelope(
    *,
    vault_id: str,
    header_revision: int,
    nonce: bytes,
    aead_ciphertext_and_tag: bytes,
    format_version: int = 1,
) -> bytes:
    """Vault header envelope per formats §9.1: 45 + N bytes."""
    if not (0 <= format_version <= 0xFF):
        raise ValueError(f"format_version must fit in u8; got {format_version}")
    if len(nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(nonce)}")
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    return (
        bytes([format_version])
        + canonical.encode("ascii")
        + header_revision.to_bytes(8, "big")
        + nonce
        + aead_ciphertext_and_tag
    )


# ---------------------------------------------------------------- Recovery envelope (formats §6.5, §12)

_RECOVERY_AAD_SCHEMA = b"dc-vault-recovery-v1"   # 20 bytes
_RECOVERY_WRAP_LABEL = "dc-vault-v1/recovery-wrap"


def build_recovery_aad(vault_id: str, envelope_id: str) -> bytes:
    """Construct the 62-byte recovery envelope AAD per formats §6.5."""
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    if len(envelope_id) != 30:
        raise ValueError(f"envelope_id must be 30 bytes; got {len(envelope_id)}")
    return (
        _RECOVERY_AAD_SCHEMA
        + canonical.encode("ascii")
        + envelope_id.encode("ascii")
    )


def derive_recovery_wrap_key(
    *,
    passphrase: str,
    recovery_secret: bytes,
    argon_salt: bytes,
    memory_kib: int = ARGON2ID_MEMORY_KIB,
    iterations: int = ARGON2ID_ITERATIONS,
) -> bytes:
    """Derive the recovery wrap key per formats §12.3.

    ``argon_output = argon2id(passphrase_NFC, argon_salt, params, 32)``
    ``wrap_key     = HKDF-SHA256(salt=argon_output, ikm=recovery_secret,
                                   info="dc-vault-v1/recovery-wrap", L=32)``

    Both passphrase AND recovery_secret are required; compromise of one
    without the other does not yield ``wrap_key``.
    """
    if len(recovery_secret) != 32:
        raise ValueError(f"recovery_secret must be 32 bytes; got {len(recovery_secret)}")
    argon_out = argon2id_kdf(
        passphrase, argon_salt, output_length=32,
        memory_kib=memory_kib, iterations=iterations,
    )
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=argon_out,
        info=_RECOVERY_WRAP_LABEL.encode("utf-8"),
    )
    return hkdf.derive(recovery_secret)


def build_recovery_envelope(
    *,
    vault_id: str,
    envelope_id: str,
    argon_salt: bytes,
    nonce: bytes,
    aead_ciphertext_and_tag: bytes,
    format_version: int = 1,
) -> bytes:
    """Recovery envelope per formats §12.4: 1 + 12 + 30 + 16 + 24 + 48 = 131 bytes."""
    if not (0 <= format_version <= 0xFF):
        raise ValueError(f"format_version must fit in u8; got {format_version}")
    if len(argon_salt) != ARGON2ID_SALT_BYTES:
        raise ValueError(f"argon_salt must be {ARGON2ID_SALT_BYTES} bytes; got {len(argon_salt)}")
    if len(nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(nonce)}")
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    if len(envelope_id) != 30:
        raise ValueError(f"envelope_id must be 30 bytes; got {len(envelope_id)}")
    return (
        bytes([format_version])
        + canonical.encode("ascii")
        + envelope_id.encode("ascii")
        + argon_salt
        + nonce
        + aead_ciphertext_and_tag
    )


# ---------------------------------------------------------------- Device grant (formats §6.6, §13, §14)

_DEVICE_GRANT_AAD_SCHEMA = b"dc-vault-device-grant-v1"   # 24 bytes
_DEVICE_GRANT_WRAP_LABEL = "dc-vault-v1/device-grant-wrap"


def build_device_grant_aad(
    vault_id: str,
    grant_id: str,
    claimant_device_id: str,
) -> bytes:
    """Construct the 98-byte device-grant AAD per formats §6.6."""
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    if len(grant_id) != 30:
        raise ValueError(f"grant_id must be 30 bytes; got {len(grant_id)}")
    if len(claimant_device_id) != 32:
        raise ValueError(f"claimant_device_id must be 32 hex chars; got {len(claimant_device_id)}")
    return (
        _DEVICE_GRANT_AAD_SCHEMA
        + canonical.encode("ascii")
        + grant_id.encode("ascii")
        + claimant_device_id.encode("ascii")
    )


def derive_device_grant_wrap_key(shared_secret: bytes) -> bytes:
    """k_device_grant_wrap per formats §13.5: ``HKDF(salt=b"\\x00"*32, ikm=shared_secret, info=label)``.

    ``shared_secret`` is the X25519 output between the admin's ephemeral
    private key and the claimant's ephemeral public key (or vice versa —
    both sides compute the same value).
    """
    if len(shared_secret) != 32:
        raise ValueError(f"shared_secret must be 32 bytes; got {len(shared_secret)}")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"\x00" * 32,
        info=_DEVICE_GRANT_WRAP_LABEL.encode("utf-8"),
    )
    return hkdf.derive(shared_secret)


def build_device_grant_envelope(
    *,
    vault_id: str,
    grant_id: str,
    claimant_pubkey: bytes,
    nonce: bytes,
    aead_ciphertext_and_tag: bytes,
    format_version: int = 1,
) -> bytes:
    """Device grant envelope per formats §14.1: 99 + N bytes."""
    if not (0 <= format_version <= 0xFF):
        raise ValueError(f"format_version must fit in u8; got {format_version}")
    if len(claimant_pubkey) != 32:
        raise ValueError(f"claimant_pubkey must be 32 bytes; got {len(claimant_pubkey)}")
    if len(nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(nonce)}")
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    if len(grant_id) != 30:
        raise ValueError(f"grant_id must be 30 bytes; got {len(grant_id)}")
    return (
        bytes([format_version])
        + canonical.encode("ascii")
        + grant_id.encode("ascii")
        + claimant_pubkey
        + nonce
        + aead_ciphertext_and_tag
    )


# ---------------------------------------------------------------- Export bundle outer envelope (formats §16)

_EXPORT_MAGIC = b"DCVE"   # 4 bytes
_EXPORT_WRAP_AAD_SCHEMA = b"dc-vault-export-wrap-v1"     # 23 bytes
_EXPORT_RECORD_AAD_SCHEMA = b"dc-vault-export-record-v1"  # 25 bytes


def build_export_outer_header(
    *,
    argon_memory_kib: int,
    argon_iterations: int,
    argon_parallelism: int,
    argon_salt: bytes,
    outer_nonce: bytes,
    format_version: int = 1,
) -> bytes:
    """Outer envelope header per formats §16.1: 4 + 1 + 4 + 4 + 4 + 16 + 24 = 57 bytes."""
    if not (0 <= format_version <= 0xFF):
        raise ValueError(f"format_version must fit in u8; got {format_version}")
    if len(argon_salt) != 16:
        raise ValueError(f"argon_salt must be 16 bytes; got {len(argon_salt)}")
    if len(outer_nonce) != XCHACHA20_NONCE_BYTES:
        raise ValueError(f"outer_nonce must be {XCHACHA20_NONCE_BYTES} bytes; got {len(outer_nonce)}")
    return (
        _EXPORT_MAGIC
        + bytes([format_version])
        + argon_memory_kib.to_bytes(4, "big")
        + argon_iterations.to_bytes(4, "big")
        + argon_parallelism.to_bytes(4, "big")
        + argon_salt
        + outer_nonce
    )


def build_export_wrap_aad(vault_id: str) -> bytes:
    """35-byte AAD for the wrapped-key envelope per formats §6.7."""
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    return _EXPORT_WRAP_AAD_SCHEMA + canonical.encode("ascii")


def build_export_record_aad(
    vault_id: str,
    record_index: int,
    record_type: int,
) -> bytes:
    """42-byte per-record AAD per formats §6.8."""
    canonical = normalize_vault_id(vault_id)
    if len(canonical) != 12:
        raise ValueError(f"vault_id must canonicalize to 12 bytes; got {len(canonical)}")
    if not (0 <= record_index <= 0xFFFFFFFF):
        raise ValueError(f"record_index out of u32 range: {record_index}")
    if not (0 <= record_type <= 0xFF):
        raise ValueError(f"record_type must fit in u8; got {record_type}")
    return (
        _EXPORT_RECORD_AAD_SCHEMA
        + canonical.encode("ascii")
        + record_index.to_bytes(4, "big")
        + bytes([record_type])
    )


def derive_export_wrap_key(
    *,
    passphrase: str,
    argon_salt: bytes,
    memory_kib: int = ARGON2ID_MEMORY_KIB,
    iterations: int = ARGON2ID_ITERATIONS,
) -> bytes:
    """k_export_wrap per formats §16.2: ``argon2id(passphrase_NFC, salt, params)`` directly.

    Unlike recovery, the export flow doesn't mix in a high-entropy kit
    secret — the export passphrase is the sole gate on the wrapped key.
    """
    return argon2id_kdf(
        passphrase, argon_salt, output_length=32,
        memory_kib=memory_kib, iterations=iterations,
    )


# ---------------------------------------------------------------- Protocol + default instance (T2.5)


@runtime_checkable
class VaultCrypto(Protocol):
    """Public surface of vault_crypto.py as a typing Protocol.

    Use as a type hint where a caller wants to accept either the real
    implementation (``DefaultVaultCrypto``) or a test fake. Methods
    map 1:1 onto the module-level functions; signatures must stay in
    lock-step or the cross-platform JSON vectors break.

    The protocol is ``@runtime_checkable`` so simple ``isinstance``
    checks work on duck-typed fakes — useful for assertions in service
    layers that want to refuse a half-implemented stub.
    """

    def derive_subkey(self, label: str, master_key: bytes, length: int = 32) -> bytes: ...
    def aead_encrypt(self, plaintext: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes: ...
    def aead_decrypt(self, ciphertext_and_tag: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes: ...
    def argon2id_kdf(
        self, passphrase: str, salt: bytes, output_length: int = 32,
    ) -> bytes: ...

    # Per-envelope builders.
    def build_manifest_aad(
        self, vault_id: str, revision: int, parent_revision: int, author_device_id: str,
    ) -> bytes: ...
    def build_chunk_aad(
        self, vault_id: str, remote_folder_id: str, file_id: str,
        file_version_id: str, chunk_index: int, chunk_plaintext_size: int,
    ) -> bytes: ...
    def build_header_aad(self, vault_id: str, header_revision: int) -> bytes: ...
    def build_recovery_aad(self, vault_id: str, envelope_id: str) -> bytes: ...
    def build_device_grant_aad(
        self, vault_id: str, grant_id: str, claimant_device_id: str,
    ) -> bytes: ...


class _DefaultVaultCrypto:
    """Production-default implementation of :class:`VaultCrypto`.

    Each attribute binds to the module-level function via ``staticmethod``;
    no per-instance state. Use ``DefaultVaultCrypto`` (the singleton
    instance below) rather than constructing this class directly.
    """

    derive_subkey      = staticmethod(derive_subkey)
    aead_encrypt       = staticmethod(aead_encrypt)
    aead_decrypt       = staticmethod(aead_decrypt)
    argon2id_kdf       = staticmethod(argon2id_kdf)
    build_manifest_aad     = staticmethod(build_manifest_aad)
    build_chunk_aad        = staticmethod(build_chunk_aad)
    build_header_aad       = staticmethod(build_header_aad)
    build_recovery_aad     = staticmethod(build_recovery_aad)
    build_device_grant_aad = staticmethod(build_device_grant_aad)


DefaultVaultCrypto: VaultCrypto = _DefaultVaultCrypto()
"""Singleton VaultCrypto implementation. Pass this where a service
takes a `VaultCrypto` argument; tests substitute a fake instead."""


__all__ = [
    "ARGON2ID_MEMORY_KIB",
    "ARGON2ID_ITERATIONS",
    "ARGON2ID_PARALLELISM",
    "ARGON2ID_SALT_BYTES",
    "DefaultVaultCrypto",
    "MASTER_KEY_BYTES",
    "POLY1305_TAG_BYTES",
    "VaultCrypto",
    "XCHACHA20_KEY_BYTES",
    "XCHACHA20_NONCE_BYTES",
    "aead_decrypt",
    "aead_encrypt",
    "argon2id_kdf",
    "build_chunk_aad",
    "build_chunk_envelope",
    "build_device_grant_aad",
    "build_device_grant_envelope",
    "build_export_outer_header",
    "build_export_record_aad",
    "build_export_wrap_aad",
    "build_header_aad",
    "build_header_envelope",
    "build_manifest_aad",
    "build_manifest_envelope",
    "build_recovery_aad",
    "build_recovery_envelope",
    "derive_device_grant_wrap_key",
    "derive_export_wrap_key",
    "derive_recovery_wrap_key",
    "derive_subkey",
    "normalize_vault_id",
]
