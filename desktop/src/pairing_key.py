"""Pairing-key codec + validation for desktop-to-desktop pairing (M.11).

A *pairing key* is the desktop-friendly form of the same data the QR
code carries: the inviter's relay URL, device id, public key, and
display name. The joiner pastes (or imports a file containing) this
key, derives its half of the shared secret via ECDH, and posts an
ordinary ``POST /api/pairing/request`` to the relay — the rest of the
flow is identical to the QR-scan path.

Wire shape (D9 in the multi-device support plan):

```
dc-pair:<url-safe-base64-of-json-blob>
```

The JSON blob holds ``{server, device_id, pubkey, name}``; the same
keys the existing QR generator emits. The encoded form is one line so
users can paste it through chat / email / password manager without
line-wrap corruption. The file form (``.dcpair``) is the same string
written to disk.

Privacy: the key is paste-secret material in the same threat-model
bucket as the QR image (anyone holding it can request to pair with
the inviter while the inviter's pairing window is open). It carries
**no symmetric key**; the AES-256-GCM key is derived per-pair via
ECDH on each side from its own X25519 private key plus the other
side's pubkey. The verification-code step on both sides catches MITM.

Never log the encoded key, decoded contents, or the verification
code — call sites use short-id correlation instead.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .crypto import KeyManager
from .devices import (
    ConnectedDeviceRegistry,
    DeviceRegistryError,
)

log = logging.getLogger("desktop-connector.pairing-key")

PAIRING_KEY_PREFIX = "dc-pair:"
PAIRING_KEY_FILE_EXT = ".dcpair"

_REQUIRED_FIELDS = ("server", "device_id", "pubkey", "name")
_FILENAME_BLOCKED = re.compile(r"[^A-Za-z0-9._\- ]+")


# --- typed errors ---------------------------------------------------


class PairingKeyError(Exception):
    """Base class for any pairing-key codec or validation failure."""


class PairingKeyParseError(PairingKeyError):
    """Bytes / base64 / JSON couldn't be parsed."""


class PairingKeySchemaError(PairingKeyError):
    """Decoded JSON is missing required fields or has the wrong types."""


class SelfPairError(PairingKeyError):
    """The pairing key's device_id is this desktop's own device_id."""


class RelayMismatchError(PairingKeyError):
    """Pairing key's relay URL does not match the local config."""

    def __init__(self, *, local: str, remote: str) -> None:
        super().__init__(
            f"pairing key targets relay {remote!r}; "
            f"this desktop is configured for {local!r}",
        )
        self.local = local
        self.remote = remote


class AlreadyPairedError(PairingKeyError):
    """The pairing key's device_id is already in paired_devices."""

    def __init__(self, *, device_id: str, name: str) -> None:
        super().__init__(
            f"already paired with {name!r} ({device_id[:12]}…)",
        )
        self.device_id = device_id
        self.name = name


# --- dataclass ------------------------------------------------------


@dataclass(frozen=True)
class PairingKey:
    server: str
    device_id: str
    pubkey: str
    name: str


# --- encode / decode -----------------------------------------------


def encode(key: PairingKey) -> str:
    """Encode a PairingKey into the canonical ``dc-pair:<b64>`` string."""
    payload = {
        "server": key.server,
        "device_id": key.device_id,
        "pubkey": key.pubkey,
        "name": key.name,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{PAIRING_KEY_PREFIX}{b64}"


def decode(text: str) -> PairingKey:
    """Decode a pairing-key string into a :class:`PairingKey`.

    Forgiving parsing — strips whitespace + embedded newlines (chat
    soft-wrap), strips an optional leading ``dc-pair:`` prefix, accepts
    base64 with or without padding. Raises :class:`PairingKeyParseError`
    on any byte-level failure and :class:`PairingKeySchemaError` on a
    type / required-field mismatch in the decoded JSON.
    """
    if not isinstance(text, str):
        raise PairingKeyParseError("pairing key must be a string")

    cleaned = "".join(text.split())  # strip all whitespace incl. newlines
    if not cleaned:
        raise PairingKeyParseError("pairing key is empty")

    if cleaned.startswith(PAIRING_KEY_PREFIX):
        cleaned = cleaned[len(PAIRING_KEY_PREFIX):]
    elif cleaned.lower().startswith(PAIRING_KEY_PREFIX):
        cleaned = cleaned[len(PAIRING_KEY_PREFIX):]

    # Restore base64 padding so urlsafe_b64decode accepts the value
    # whether or not the encoder emitted padding.
    pad = (-len(cleaned)) % 4
    if pad:
        cleaned = cleaned + ("=" * pad)

    try:
        raw = base64.urlsafe_b64decode(cleaned.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise PairingKeyParseError(
            f"pairing key is not valid base64: {type(exc).__name__}",
        ) from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PairingKeyParseError(
            f"pairing key is not valid JSON: {type(exc).__name__}",
        ) from exc

    if not isinstance(payload, dict):
        raise PairingKeySchemaError("pairing key JSON must be an object")

    for field in _REQUIRED_FIELDS:
        if field not in payload:
            raise PairingKeySchemaError(f"pairing key missing field: {field}")
        if not isinstance(payload[field], str):
            raise PairingKeySchemaError(
                f"pairing key field {field!r} must be a string"
            )

    server = payload["server"].strip()
    device_id = payload["device_id"].strip()
    pubkey = payload["pubkey"].strip()
    name = payload["name"].strip()

    if not server or not device_id or not pubkey or not name:
        raise PairingKeySchemaError("pairing key has empty required field")

    return PairingKey(
        server=server,
        device_id=device_id,
        pubkey=pubkey,
        name=name,
    )


# --- validate -------------------------------------------------------


def validate_for_join(
    key: PairingKey,
    *,
    config: Config,
    crypto: KeyManager,
    registry: ConnectedDeviceRegistry | None = None,
) -> None:
    """Enforce the join-side rules from D8 + D10 + already-paired refusal.

    Raises one of the typed ``*Error`` subclasses on failure. Returns
    ``None`` on success — call sites then derive the symkey and proceed
    to the verification-code step.
    """
    if key.device_id == crypto.get_device_id():
        raise SelfPairError("pairing key targets this same desktop")

    local_norm = _normalize_server_url(config.server_url)
    remote_norm = _normalize_server_url(key.server)
    if local_norm != remote_norm:
        raise RelayMismatchError(local=local_norm, remote=remote_norm)

    if registry is None:
        registry = ConnectedDeviceRegistry(config)
    existing = registry.get(key.device_id)
    if existing is not None:
        raise AlreadyPairedError(
            device_id=existing.device_id,
            name=existing.name or existing.short_id,
        )


def _normalize_server_url(url: str) -> str:
    """Normalize for D8 comparison: scheme/host case-fold, trailing slash trim."""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))


# --- helpers --------------------------------------------------------


def default_filename(key: PairingKey) -> str:
    """Sanitised default filename for the export-to-file dialog."""
    base = _FILENAME_BLOCKED.sub("-", key.name).strip().strip("-.")
    return f"{base or 'device'}{PAIRING_KEY_FILE_EXT}"


def build_local_key(config: Config, crypto: KeyManager) -> PairingKey:
    """Build the inviter's pairing key from local Config + KeyManager."""
    return PairingKey(
        server=config.server_url,
        device_id=crypto.get_device_id(),
        pubkey=crypto.get_public_key_b64(),
        name=config.device_name,
    )


# --- joiner-side helpers --------------------------------------------


class JoinRequestError(PairingKeyError):
    """The relay refused the pairing request from the joiner."""


@dataclass(frozen=True)
class PairingHandshake:
    """Materials in hand on the joiner side after the request lands.

    ``shared_key`` is the 32-byte AES key derived via X25519+HKDF from
    the joiner's private key plus the inviter's pubkey; the verification
    code is the standard 6-digit display the user compares with the
    inviter's screen.
    """

    key: PairingKey
    shared_key: bytes
    verification_code: str


def begin_join(
    key: PairingKey,
    *,
    crypto: KeyManager,
    send_pairing_request: Callable[[str, str], bool],
) -> PairingHandshake:
    """Send the pairing request and derive the shared symkey.

    Validation must have already passed via :func:`validate_for_join`.
    The ``send_pairing_request`` injection lets call sites pass a bound
    ``api.send_pairing_request`` (or a fake in tests). Raises
    :class:`JoinRequestError` if the relay refuses the request.
    """
    ok = send_pairing_request(
        key.device_id,
        crypto.get_public_key_b64(),
    )
    if not ok:
        raise JoinRequestError(
            "relay refused the pairing request — the inviter's pairing "
            "window may be closed or the relay is unreachable",
        )
    shared = crypto.derive_shared_key(key.pubkey)
    code = KeyManager.get_verification_code(shared)
    return PairingHandshake(
        key=key,
        shared_key=shared,
        verification_code=code,
    )


def complete_join(
    handshake: PairingHandshake,
    *,
    config: Config,
    name: str,
    on_synced: Callable[[], None] | None = None,
) -> None:
    """Persist the pair locally after the user confirms the verification.

    Saves via ``config.add_paired_device`` (which also writes the symkey
    through the secret store), marks the new pair as the active device,
    and invokes ``on_synced`` so callers can run file-manager target
    sync without coupling this module to that subsystem.

    The caller is responsible for normalising / uniquing ``name``
    upstream (Settings rename uses the registry validator; the GTK
    naming page does the same on first save).
    """
    key = handshake.key
    config.add_paired_device(
        device_id=key.device_id,
        pubkey=key.pubkey,
        symmetric_key_b64=base64.b64encode(handshake.shared_key).decode(),
        name=name,
    )
    registry = ConnectedDeviceRegistry(config)
    try:
        registry.mark_active(key.device_id, reason="paired")
    except DeviceRegistryError:
        log.debug(
            "pairing.key.mark_active_failed peer=%s",
            key.device_id[:12],
        )
    if on_synced is not None:
        try:
            on_synced()
        except Exception:
            log.exception(
                "pairing.key.on_synced_failed peer=%s",
                key.device_id[:12],
            )
