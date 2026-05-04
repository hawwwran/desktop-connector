"""QR-encoded join URL for a vault join request (T13.2).

Wire format (per T13.2 spec):

    vault://<relay-host[:port]>/<vault_id>/<join_request_id>/<ephemeral_pubkey_b64>?expires=<unix-epoch-seconds>

- ``vault_id`` is rendered in the dashed 4-4-4 form. The parser accepts
  both dashed and undashed forms and re-normalises.
- ``join_request_id`` is the ``jr_v1_<24base32>`` opaque id minted by the
  relay.
- ``ephemeral_pubkey_b64`` is the 32-byte X25519 admin pubkey, urlsafe
  base64 (no padding), the same form the relay returns from
  ``POST /api/vaults/{id}/join-requests``.
- ``expires`` is a unix epoch (UTC seconds). The 15-minute default lives
  in ``DEFAULT_TTL_SECONDS``; the relay enforces ``expires_at`` server-side
  too — clients reject locally for snappy UX.

The URL is the only payload the QR carries; the claimant's desktop
parses it, derives the verification code from both ephemeral pubkeys
client-side, and POSTs the claim. No relay credentials cross the QR.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse


DEFAULT_TTL_SECONDS = 15 * 60  # T13.2 spec
SCHEME = "vault"


_VAULT_ID_DASHED_RE = re.compile(r"^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$")
_VAULT_ID_UNDASHED_RE = re.compile(r"^[A-Z2-7]{12}$")
_JOIN_REQUEST_ID_RE = re.compile(r"^jr_v1_[a-z2-7]{24}$")


class VaultGrantQRError(ValueError):
    """Raised on invalid join URL input (build- or parse-time)."""


@dataclass(frozen=True)
class VaultJoinUrl:
    relay_url: str           # e.g. "https://example.com" or "http://localhost:4441"
    vault_id_dashed: str     # canonical 4-4-4 form
    join_request_id: str
    ephemeral_pubkey: bytes  # 32 raw bytes
    expires_at: int          # unix epoch seconds (UTC)

    @property
    def vault_id_undashed(self) -> str:
        return self.vault_id_dashed.replace("-", "")

    def is_expired(self, *, now: float | None = None) -> bool:
        return float(self.expires_at) <= (time.time() if now is None else float(now))


def make_join_url(
    *,
    relay_url: str,
    vault_id: str,
    join_request_id: str,
    ephemeral_pubkey: bytes,
    expires_at: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Build the QR-encoded join URL from caller-supplied parts.

    ``vault_id`` is normalised to dashed form. If ``expires_at`` is
    ``None`` we default to ``now + ttl_seconds``. ``ephemeral_pubkey``
    must be exactly 32 bytes (X25519 pubkey).
    """
    relay_url = str(relay_url).rstrip("/")
    if not relay_url:
        raise VaultGrantQRError("relay_url is required")
    parsed = urlparse(relay_url)
    if parsed.scheme not in ("http", "https"):
        raise VaultGrantQRError(
            f"relay_url scheme must be http(s), got {parsed.scheme!r}"
        )
    if not parsed.netloc:
        raise VaultGrantQRError("relay_url is missing host")

    dashed = _normalize_vault_id_to_dashed(vault_id)

    if not _JOIN_REQUEST_ID_RE.match(join_request_id):
        raise VaultGrantQRError(
            f"join_request_id must match jr_v1_<24base32>, got {join_request_id!r}"
        )

    if not isinstance(ephemeral_pubkey, (bytes, bytearray)) or len(ephemeral_pubkey) != 32:
        raise VaultGrantQRError("ephemeral_pubkey must be exactly 32 bytes")
    pk_b64 = base64.urlsafe_b64encode(bytes(ephemeral_pubkey)).rstrip(b"=").decode("ascii")

    if expires_at is None:
        epoch = int((time.time() if now is None else float(now)) + int(ttl_seconds))
    else:
        epoch = int(expires_at)
    if epoch <= 0:
        raise VaultGrantQRError("expires_at must be a positive unix epoch")

    # Encode the host as the URL host; the path carries vault_id /
    # join_request_id / ephemeral_pubkey. We use SCHEME://host/... rather
    # than embedding the relay's https:// because the QR target is a
    # vault: URL the desktop registers as a handler.
    host = parsed.netloc
    return (
        f"{SCHEME}://{host}/{quote(dashed, safe='-')}"
        f"/{join_request_id}/{pk_b64}?expires={epoch}"
    )


def parse_join_url(raw: str) -> VaultJoinUrl:
    """Parse a vault://… join URL into its components."""
    if not isinstance(raw, str) or not raw.strip():
        raise VaultGrantQRError("join URL is empty")
    parsed = urlparse(raw.strip())
    if parsed.scheme != SCHEME:
        raise VaultGrantQRError(
            f"join URL scheme must be {SCHEME!r}, got {parsed.scheme!r}"
        )
    host = parsed.netloc
    if not host:
        raise VaultGrantQRError("join URL is missing host")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 3:
        raise VaultGrantQRError(
            f"join URL must encode /vault_id/join_request_id/pubkey (got {len(parts)} parts)"
        )
    vault_id_part, join_request_id, pk_b64 = parts

    dashed = _normalize_vault_id_to_dashed(vault_id_part)
    if not _JOIN_REQUEST_ID_RE.match(join_request_id):
        raise VaultGrantQRError(
            f"join_request_id must match jr_v1_<24base32>, got {join_request_id!r}"
        )

    pubkey = _decode_pubkey_b64(pk_b64)

    qs = parse_qs(parsed.query)
    expires_list = qs.get("expires", [])
    if not expires_list:
        raise VaultGrantQRError("join URL is missing ?expires=…")
    try:
        expires_at = int(expires_list[0])
    except ValueError as exc:
        raise VaultGrantQRError(f"expires is not an integer: {expires_list[0]!r}") from exc
    if expires_at <= 0:
        raise VaultGrantQRError("expires must be a positive unix epoch")

    relay_https = f"https://{host}"
    return VaultJoinUrl(
        relay_url=relay_https,
        vault_id_dashed=dashed,
        join_request_id=join_request_id,
        ephemeral_pubkey=pubkey,
        expires_at=expires_at,
    )


def _normalize_vault_id_to_dashed(vault_id: str) -> str:
    """Accept dashed or undashed input, return the canonical 4-4-4 form."""
    candidate = (vault_id or "").strip().upper()
    if not candidate:
        raise VaultGrantQRError("vault_id is empty")
    if _VAULT_ID_DASHED_RE.match(candidate):
        return candidate
    if _VAULT_ID_UNDASHED_RE.match(candidate):
        return f"{candidate[:4]}-{candidate[4:8]}-{candidate[8:12]}"
    raise VaultGrantQRError(
        "vault_id must be 12 base32 chars (dashed or undashed), "
        f"got {vault_id!r}"
    )


def _decode_pubkey_b64(b64: str) -> bytes:
    # Restore URL-safe base64 padding.
    padded = b64 + ("=" * (-len(b64) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise VaultGrantQRError(f"ephemeral_pubkey base64 decode failed: {exc}") from exc
    if len(raw) != 32:
        raise VaultGrantQRError(
            f"ephemeral_pubkey must decode to 32 bytes, got {len(raw)}"
        )
    return raw


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "SCHEME",
    "VaultGrantQRError",
    "VaultJoinUrl",
    "make_join_url",
    "parse_join_url",
]
