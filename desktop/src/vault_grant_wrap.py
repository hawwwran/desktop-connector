"""Wrap / unwrap the vault grant for the QR exchange (T13.4 + T13.5).

When the admin approves a join request, the relay needs to carry the
*sensitive* vault material (master_key + access secret + role) from
the admin's device to the claimant's device. The relay must never see
the plaintext, so we wrap it with X25519 ECDH between the two ephemeral
keypairs already established during the QR + claim exchange:

    shared = X25519(admin_priv, claimant_pub)        # admin side
    shared = X25519(claimant_priv, admin_pub)        # claimant side
    aead_key = HKDF-SHA256(shared, info="dc-vault-v1/device-grant-wrap")

The envelope and AAD follow ``docs/protocol/vault-v1-formats.md`` §13.5
+ §14: a 99-byte deterministic prefix
(format_version + vault_id + grant_id + claimant_pubkey + nonce) plus
the variable AEAD ciphertext+tag. The 98-byte AAD binds vault_id +
grant_id + claimant_device_id so a wrapped grant exfiltrated en route
cannot be replayed onto a different device or vault. The plaintext is
the canonical-JSON ``dc-vault-device-grant-v1`` schema, including the
v1 ``vault_access_secret`` extension.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

import nacl.bindings

from .vault_crypto import (
    XCHACHA20_NONCE_BYTES,
    aead_decrypt, aead_encrypt,
    build_device_grant_aad,
    build_device_grant_envelope,
    derive_device_grant_wrap_key,
    normalize_vault_id,
)


# Envelope deterministic prefix (formats §14.1):
#   format_version (1) + vault_id (12) + grant_id (30)
#   + claimant_pubkey (32) + nonce (24) = 99 bytes.
_ENVELOPE_PREFIX_BYTES = 1 + 12 + 30 + 32 + XCHACHA20_NONCE_BYTES


class GrantWrapError(ValueError):
    """Raised on malformed wrap inputs (wrong key length, bad ciphertext, …)."""


@dataclass(frozen=True)
class GrantPayload:
    """Plaintext schema for the device grant.

    Mirrors formats §14.2 plus the v1 ``vault_access_secret`` extension —
    recovery on a fresh device needs both the master key (for AEAD) and
    the access secret (for relay auth), so the spec was extended to ship
    both in one wrap. The relay never sees this plaintext; it's AEAD
    -sealed end-to-end between the admin and the claimant.
    """

    vault_id: str                    # 12 base32 chars (canonical / wire form)
    grant_id: str                    # gr_v1_<24base32>, 30 chars total
    claimant_device_id: str          # 32 hex chars
    approved_role: str               # read-only|browse-upload|sync|admin (D11)
    granted_by_device_id: str        # 32 hex chars
    granted_at: str                  # RFC3339 ms-precision UTC
    vault_master_key_b64: str        # base64 of 32 bytes
    vault_access_secret: str         # v1 extension; URL-safe base64

    def to_canonical_dict(self) -> dict[str, Any]:
        """Wire-shape JSON dict (formats §14.2). The base64-encoded master
        key serializes under the spec's ``vault_master_key`` field name;
        ``vault_master_key_b64`` is just the local Python attribute label."""
        return {
            "schema":                "dc-vault-device-grant-v1",
            "grant_id":              self.grant_id,
            "vault_id":              normalize_vault_id(self.vault_id),
            "claimant_device_id":    self.claimant_device_id,
            "approved_role":         self.approved_role,
            "granted_by_device_id":  self.granted_by_device_id,
            "granted_at":            self.granted_at,
            "vault_master_key":      self.vault_master_key_b64,
            "vault_access_secret":   self.vault_access_secret,
        }


def wrap_grant_for_claimant(
    *,
    payload: GrantPayload,
    admin_priv: bytes,
    claimant_pub: bytes,
) -> bytes:
    """Encrypt ``payload`` for the claimant; returns the full envelope.

    Envelope shape per formats §14.1: 99-byte prefix + AEAD ct+tag.
    """
    if not isinstance(admin_priv, (bytes, bytearray)) or len(admin_priv) != 32:
        raise GrantWrapError("admin_priv must be 32 bytes")
    if not isinstance(claimant_pub, (bytes, bytearray)) or len(claimant_pub) != 32:
        raise GrantWrapError("claimant_pub must be 32 bytes")
    _validate_payload_ids(payload)

    canonical_vault_id = normalize_vault_id(payload.vault_id)
    shared = _x25519_shared(bytes(admin_priv), bytes(claimant_pub))
    aead_key = derive_device_grant_wrap_key(shared)
    aad = build_device_grant_aad(
        canonical_vault_id, payload.grant_id, payload.claimant_device_id,
    )
    nonce = _random_nonce()
    plaintext = _encode_payload(payload)
    ciphertext = aead_encrypt(plaintext, aead_key, nonce, aad)
    return build_device_grant_envelope(
        vault_id=canonical_vault_id,
        grant_id=payload.grant_id,
        claimant_pubkey=bytes(claimant_pub),
        nonce=nonce,
        aead_ciphertext_and_tag=ciphertext,
    )


def unwrap_grant_for_claimant(
    *,
    envelope: bytes,
    claimant_priv: bytes,
    admin_pub: bytes,
    expected_vault_id: str,
    expected_claimant_device_id: str,
) -> GrantPayload:
    """Decrypt the envelope on the claimant side.

    Verifies the envelope-internal vault_id matches ``expected_vault_id``
    (the QR-encoded vault) so a malicious admin can't re-target a wrap
    onto a different vault. ``expected_claimant_device_id`` is used to
    rebuild the AAD; AEAD failure surfaces as ``GrantWrapError``.
    """
    if not isinstance(claimant_priv, (bytes, bytearray)) or len(claimant_priv) != 32:
        raise GrantWrapError("claimant_priv must be 32 bytes")
    if not isinstance(admin_pub, (bytes, bytearray)) or len(admin_pub) != 32:
        raise GrantWrapError("admin_pub must be 32 bytes")
    if not isinstance(envelope, (bytes, bytearray)) or len(envelope) <= _ENVELOPE_PREFIX_BYTES:
        raise GrantWrapError("envelope is shorter than the 99-byte prefix + AEAD tag")

    envelope = bytes(envelope)
    canonical_expected = normalize_vault_id(expected_vault_id)
    if len(canonical_expected) != 12:
        raise GrantWrapError("expected_vault_id must canonicalize to 12 bytes")
    if len(expected_claimant_device_id) != 32:
        raise GrantWrapError("expected_claimant_device_id must be 32 hex chars")

    format_version = envelope[0]
    if format_version != 1:
        raise GrantWrapError(f"unknown grant envelope format_version: {format_version}")
    vault_id_bytes = envelope[1:13].decode("ascii")
    if vault_id_bytes != canonical_expected:
        raise GrantWrapError(
            "envelope vault_id does not match expected_vault_id"
        )
    grant_id_bytes = envelope[13:43].decode("ascii")
    claimant_pubkey_bytes = envelope[43:75]
    nonce = envelope[75:75 + XCHACHA20_NONCE_BYTES]
    ciphertext = envelope[_ENVELOPE_PREFIX_BYTES:]

    shared = _x25519_shared(bytes(claimant_priv), bytes(admin_pub))
    aead_key = derive_device_grant_wrap_key(shared)
    aad = build_device_grant_aad(
        canonical_expected, grant_id_bytes, expected_claimant_device_id,
    )
    try:
        plaintext = aead_decrypt(ciphertext, aead_key, nonce, aad)
    except Exception as exc:
        raise GrantWrapError(f"AEAD decrypt failed: {exc}") from exc

    payload = _decode_payload(plaintext)
    # Defense in depth: payload's self-attested ids must match what the
    # AAD already cryptographically pinned. AEAD success implies they
    # agree; this catches a malformed admin client whose payload diverges
    # from its own AAD.
    if normalize_vault_id(payload.vault_id) != canonical_expected:
        raise GrantWrapError("payload vault_id mismatch")
    if payload.grant_id != grant_id_bytes:
        raise GrantWrapError("payload grant_id mismatch")
    if payload.claimant_device_id != expected_claimant_device_id:
        raise GrantWrapError("payload claimant_device_id mismatch")
    return payload


def _validate_payload_ids(payload: GrantPayload) -> None:
    canonical = normalize_vault_id(payload.vault_id)
    if len(canonical) != 12:
        raise GrantWrapError("payload.vault_id must canonicalize to 12 bytes")
    if len(payload.grant_id) != 30:
        raise GrantWrapError("payload.grant_id must be 30 bytes (gr_v1_<24base32>)")
    if len(payload.claimant_device_id) != 32:
        raise GrantWrapError("payload.claimant_device_id must be 32 hex chars")
    if len(payload.granted_by_device_id) != 32:
        raise GrantWrapError("payload.granted_by_device_id must be 32 hex chars")
    if payload.approved_role not in (
        "read-only", "browse-upload", "sync", "admin",
    ):
        raise GrantWrapError(f"payload.approved_role unknown: {payload.approved_role}")


def _encode_payload(payload: GrantPayload) -> bytes:
    return json.dumps(
        payload.to_canonical_dict(),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _decode_payload(plaintext: bytes) -> GrantPayload:
    try:
        obj: dict[str, Any] = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise GrantWrapError(f"grant payload is not valid JSON: {exc}") from exc
    if obj.get("schema") != "dc-vault-device-grant-v1":
        raise GrantWrapError(
            f"grant payload schema mismatch: {obj.get('schema')!r}"
        )
    required = {
        "grant_id", "vault_id", "claimant_device_id", "approved_role",
        "granted_by_device_id", "granted_at",
        "vault_master_key", "vault_access_secret",
    }
    missing = required - obj.keys()
    if missing:
        raise GrantWrapError(f"grant payload missing fields: {sorted(missing)}")
    return GrantPayload(
        vault_id=str(obj["vault_id"]),
        grant_id=str(obj["grant_id"]),
        claimant_device_id=str(obj["claimant_device_id"]),
        approved_role=str(obj["approved_role"]),
        granted_by_device_id=str(obj["granted_by_device_id"]),
        granted_at=str(obj["granted_at"]),
        vault_master_key_b64=str(obj["vault_master_key"]),
        vault_access_secret=str(obj["vault_access_secret"]),
    )


def _random_nonce() -> bytes:
    import secrets
    return secrets.token_bytes(XCHACHA20_NONCE_BYTES)


def _x25519_shared(priv: bytes, pub: bytes) -> bytes:
    """Return the 32-byte X25519 shared secret. PyNaCl is the primary
    backend; falls back to ``cryptography`` so the function works in
    minimal environments.
    """
    try:
        return bytes(nacl.bindings.crypto_scalarmult(priv, pub))
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric import x25519
        priv_obj = x25519.X25519PrivateKey.from_private_bytes(priv)
        pub_obj = x25519.X25519PublicKey.from_public_bytes(pub)
        return priv_obj.exchange(pub_obj)
    except Exception as exc:  # noqa: BLE001
        raise GrantWrapError(
            f"no X25519 backend available (PyNaCl + cryptography both failed): {exc}"
        ) from exc


__all__ = [
    "GrantPayload",
    "GrantWrapError",
    "unwrap_grant_for_claimant",
    "wrap_grant_for_claimant",
]
