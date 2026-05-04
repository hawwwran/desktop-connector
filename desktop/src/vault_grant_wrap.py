"""Wrap / unwrap the vault grant for the QR exchange (T13.4 + T13.5).

When the admin approves a join request, the relay needs to carry the
*sensitive* vault material (master_key + access secret + role) from
the admin's device to the claimant's device. The relay must never
see the plaintext, so we wrap it with X25519 ECDH between the two
ephemeral keypairs already established during the QR + claim
exchange:

    shared = X25519(admin_priv, claimant_pub)        # admin side
    shared = X25519(claimant_priv, admin_pub)        # claimant side
    aead_key = HKDF-SHA256(shared, label="dc-vault-v1/grant-wrap")
    nonce = random 24 bytes
    aead = XChaCha20-Poly1305(aead_key, nonce, plaintext, AAD=join_request_id)
    wrapped = nonce ‖ aead

The AAD ties the wrap to the specific join_request_id so a recipient
can't be tricked into unwrapping a payload meant for a different
session. ``GrantPayload`` is JSON-encoded plaintext with the
fields the claimant needs to open the vault and start operating.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from .vault_crypto import (
    XCHACHA20_NONCE_BYTES,
    aead_decrypt, aead_encrypt, derive_subkey,
)


GRANT_WRAP_LABEL = b"dc-vault-v1/grant-wrap"


class GrantWrapError(ValueError):
    """Raised on malformed wrap inputs (wrong key length, bad ciphertext, …)."""


@dataclass(frozen=True)
class GrantPayload:
    vault_master_key_b64: str       # base64 of the 32-byte master key
    vault_access_secret: str        # raw secret string (the relay never gets this)
    role: str                       # read-only|browse-upload|sync|admin (D11)
    granted_by_device_id: str       # admin who approved
    granted_at: int                 # unix epoch seconds


def wrap_grant_for_claimant(
    *,
    payload: GrantPayload,
    admin_priv: bytes,
    claimant_pub: bytes,
    join_request_id: str,
) -> bytes:
    """Encrypt ``payload`` for the claimant; returns ``nonce ‖ aead`` bytes."""
    if not isinstance(admin_priv, (bytes, bytearray)) or len(admin_priv) != 32:
        raise GrantWrapError("admin_priv must be 32 bytes")
    if not isinstance(claimant_pub, (bytes, bytearray)) or len(claimant_pub) != 32:
        raise GrantWrapError("claimant_pub must be 32 bytes")
    if not isinstance(join_request_id, str) or not join_request_id:
        raise GrantWrapError("join_request_id is required (used as AAD)")

    shared = _x25519_shared(admin_priv, claimant_pub)
    aead_key = derive_subkey(GRANT_WRAP_LABEL.decode(), shared)
    nonce = secrets.token_bytes(XCHACHA20_NONCE_BYTES)
    plaintext = _encode_payload(payload)
    aad = join_request_id.encode("utf-8")
    ct = aead_encrypt(plaintext, aead_key, nonce, aad)
    return nonce + ct


def unwrap_grant_for_claimant(
    *,
    wrapped: bytes,
    claimant_priv: bytes,
    admin_pub: bytes,
    join_request_id: str,
) -> GrantPayload:
    """Decrypt the wrapped grant on the claimant side."""
    if not isinstance(claimant_priv, (bytes, bytearray)) or len(claimant_priv) != 32:
        raise GrantWrapError("claimant_priv must be 32 bytes")
    if not isinstance(admin_pub, (bytes, bytearray)) or len(admin_pub) != 32:
        raise GrantWrapError("admin_pub must be 32 bytes")
    if not isinstance(wrapped, (bytes, bytearray)) or len(wrapped) <= XCHACHA20_NONCE_BYTES:
        raise GrantWrapError("wrapped is shorter than nonce + AEAD tag")
    if not isinstance(join_request_id, str) or not join_request_id:
        raise GrantWrapError("join_request_id is required (used as AAD)")

    nonce = bytes(wrapped[:XCHACHA20_NONCE_BYTES])
    ct = bytes(wrapped[XCHACHA20_NONCE_BYTES:])
    shared = _x25519_shared(claimant_priv, admin_pub)
    aead_key = derive_subkey(GRANT_WRAP_LABEL.decode(), shared)
    aad = join_request_id.encode("utf-8")
    try:
        plaintext = aead_decrypt(ct, aead_key, nonce, aad)
    except Exception as exc:
        raise GrantWrapError(f"AEAD decrypt failed: {exc}") from exc
    return _decode_payload(plaintext)


def _encode_payload(payload: GrantPayload) -> bytes:
    return json.dumps(asdict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")


def _decode_payload(plaintext: bytes) -> GrantPayload:
    try:
        obj: dict[str, Any] = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise GrantWrapError(f"grant payload is not valid JSON: {exc}") from exc
    required = {
        "vault_master_key_b64", "vault_access_secret", "role",
        "granted_by_device_id", "granted_at",
    }
    missing = required - obj.keys()
    if missing:
        raise GrantWrapError(f"grant payload missing fields: {sorted(missing)}")
    return GrantPayload(
        vault_master_key_b64=str(obj["vault_master_key_b64"]),
        vault_access_secret=str(obj["vault_access_secret"]),
        role=str(obj["role"]),
        granted_by_device_id=str(obj["granted_by_device_id"]),
        granted_at=int(obj["granted_at"]),
    )


def _x25519_shared(priv: bytes, pub: bytes) -> bytes:
    """Return the 32-byte X25519 shared secret. Uses PyNaCl when available,
    falling back to ``cryptography`` so the function works in either
    environment.
    """
    try:
        from nacl.bindings import crypto_scalarmult
        return crypto_scalarmult(bytes(priv), bytes(pub))
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric import x25519
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv_obj = x25519.X25519PrivateKey.from_private_bytes(bytes(priv))
        pub_obj = x25519.X25519PublicKey.from_public_bytes(bytes(pub))
        return priv_obj.exchange(pub_obj)
    except Exception as exc:  # noqa: BLE001
        raise GrantWrapError(
            f"no X25519 backend available (PyNaCl + cryptography both failed): {exc}"
        ) from exc


__all__ = [
    "GRANT_WRAP_LABEL",
    "GrantPayload",
    "GrantWrapError",
    "unwrap_grant_for_claimant",
    "wrap_grant_for_claimant",
]
