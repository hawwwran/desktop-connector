"""Vault ID generation + dashed display form + genesis fingerprint."""

import hashlib
import hmac
import secrets


# Random portions of the locked ID alphabets.
_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"
_BASE32_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _generate_vault_id() -> str:
    """Random 12-char base32 (uppercase) vault id, undashed."""
    raw = secrets.token_bytes(15)
    out = []
    bits = 0
    buf = 0
    for byte in raw:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32_UPPER[(buf >> bits) & 0x1f])
    return "".join(out[:12])


def _generate_id_v1(prefix: str) -> str:
    """``<prefix>_v1_<24 base32 lowercase>`` per formats §3.3."""
    raw = secrets.token_bytes(15)
    out = []
    bits = 0
    buf = 0
    for byte in raw:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32_LOWER[(buf >> bits) & 0x1f])
    return f"{prefix}_v1_" + "".join(out[:24])


def _genesis_fingerprint_hex(master_key: bytes) -> str:
    """``HMAC-SHA256(master_key, "dc-vault-v1/genesis-fingerprint")[0:16]`` per formats §8.1."""
    mac = hmac.new(master_key, b"dc-vault-v1/genesis-fingerprint", hashlib.sha256).digest()
    return mac[:16].hex()


def vault_id_dashed(vault_id_undashed: str) -> str:
    """``ABCD2345WXYZ`` → ``ABCD-2345-WXYZ`` for display + filenames."""
    v = vault_id_undashed
    if len(v) == 12:
        return f"{v[0:4]}-{v[4:8]}-{v[8:12]}"
    return v
