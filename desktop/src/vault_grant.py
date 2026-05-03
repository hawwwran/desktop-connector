"""Per-device vault grant storage (T3.2).

Once a device has unlocked a vault (via passphrase + recovery kit OR
via QR-grant approval from another paired device, T13), the resulting
``master_key`` is cached locally so the user doesn't have to re-enter
the passphrase every session.

Two backends, mirroring the existing ``secrets.SecretStore`` split:

- :class:`KeyringGrantStore` — system keyring (libsecret / KWallet via
  the ``keyring`` package). Production default.
- :class:`FileGrantStore` — AEAD-encrypted JSON file at
  ``<config_dir>/vault_grant_<vault_id>.json``. Used when the keyring
  is unreachable (headless boxes, disabled session bus). Wrap key is
  device-local — derived via HKDF from a caller-provided seed (in
  production, the device's X25519 private key bytes from
  ``crypto.KeyManager``).

Sensitive material is held in ``bytearray`` so :meth:`VaultGrant.zero`
can overwrite it in place after use.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .vault_crypto import (
    XCHACHA20_KEY_BYTES,
    XCHACHA20_NONCE_BYTES,
    aead_decrypt,
    aead_encrypt,
)

log = logging.getLogger(__name__)


_KEYRING_SERVICE = "desktop-connector"
_KEYRING_KEY_PREFIX = "vault_grant:"

_FILE_AAD_SCHEMA = b"dc-vault-grant-fallback-v1"
_FILE_HKDF_INFO = b"dc-vault-v1/grant-fallback-wrap"


# ---------------------------------------------------------------- VaultGrant


@dataclass
class VaultGrant:
    """Per-device unlock material for a single vault.

    Holds ``master_key`` as a ``bytearray`` so :meth:`zero` can overwrite
    in place. Direct access via :attr:`master_key` returns ``bytes``
    for callers (``bytearray`` round-trip is safe).
    """

    vault_id: str
    _master_key: bytearray
    vault_access_secret: str

    @classmethod
    def from_bytes(cls, vault_id: str, master_key: bytes, vault_access_secret: str) -> "VaultGrant":
        if len(master_key) != 32:
            raise ValueError(f"master_key must be 32 bytes; got {len(master_key)}")
        return cls(
            vault_id=vault_id,
            _master_key=bytearray(master_key),
            vault_access_secret=vault_access_secret,
        )

    @property
    def master_key(self) -> bytes:
        return bytes(self._master_key)

    def zero(self) -> None:
        for i in range(len(self._master_key)):
            self._master_key[i] = 0
        self._master_key = bytearray()
        self.vault_access_secret = ""

    def to_json(self) -> str:
        """Plaintext JSON — used by the keyring backend (which provides
        the at-rest encryption itself) and as the AEAD plaintext for
        the file backend."""
        return json.dumps({
            "vault_id": self.vault_id,
            "master_key_b64": base64.b64encode(bytes(self._master_key)).decode("ascii"),
            "vault_access_secret": self.vault_access_secret,
        }, separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> "VaultGrant":
        obj = json.loads(data)
        return cls.from_bytes(
            vault_id=obj["vault_id"],
            master_key=base64.b64decode(obj["master_key_b64"]),
            vault_access_secret=obj["vault_access_secret"],
        )


# ---------------------------------------------------------------- GrantStore protocol


class GrantStore(Protocol):
    def save(self, grant: VaultGrant) -> None: ...
    def load(self, vault_id: str) -> VaultGrant | None: ...
    def delete(self, vault_id: str) -> None: ...


# ---------------------------------------------------------------- keyring backend


class KeyringGrantStore:
    """Stores grants in the OS keyring via the ``keyring`` package.

    Construct via :meth:`open_default` to probe the runtime backend at
    startup; raises :class:`KeyringUnavailable` if no usable backend is
    reachable so the caller can fall back to :class:`FileGrantStore`.
    """

    def __init__(self, keyring_module) -> None:
        self._kr = keyring_module

    @classmethod
    def open_default(cls) -> "KeyringGrantStore":
        try:
            import keyring as keyring_module
            from keyring.errors import NoKeyringError  # noqa: F401
        except ImportError as exc:
            raise KeyringUnavailable("keyring package not installed") from exc

        # Probe: getting a known-absent key on a working backend returns
        # None; on a non-functional backend raises.
        try:
            keyring_module.get_password(_KEYRING_SERVICE, "_probe_no_such_key")
        except Exception as exc:
            raise KeyringUnavailable(f"keyring probe failed: {exc}") from exc

        return cls(keyring_module)

    @staticmethod
    def _key(vault_id: str) -> str:
        return _KEYRING_KEY_PREFIX + vault_id

    def save(self, grant: VaultGrant) -> None:
        self._kr.set_password(_KEYRING_SERVICE, self._key(grant.vault_id), grant.to_json())

    def load(self, vault_id: str) -> VaultGrant | None:
        raw = self._kr.get_password(_KEYRING_SERVICE, self._key(vault_id))
        if raw is None:
            return None
        return VaultGrant.from_json(raw)

    def delete(self, vault_id: str) -> None:
        try:
            self._kr.delete_password(_KEYRING_SERVICE, self._key(vault_id))
        except Exception as exc:
            # ``keyring`` raises PasswordDeleteError when the entry
            # doesn't exist; idempotent delete is more useful.
            log.debug("vault_grant.keyring.delete_noop %s: %s", vault_id, exc)


class KeyringUnavailable(Exception):
    """Raised by :meth:`KeyringGrantStore.open_default` when the
    keyring isn't reachable. Callers fall back to :class:`FileGrantStore`.
    """


# ---------------------------------------------------------------- file backend (AEAD-wrapped fallback)


class FileGrantStore:
    """AEAD-encrypted JSON file fallback when the keyring is offline.

    The wrap key is derived by HKDF-SHA256 from a caller-supplied
    ``device_seed`` (in production: the bytes of the device's X25519
    private key, fetched from ``crypto.KeyManager``). Tests pass a
    fixed seed so they can verify byte-shape behavior without
    touching the real key.

    File layout (per vault):
        <config_dir>/vault_grant_<vault_id>.json — JSON with
            schema_version, nonce_b64, ciphertext_b64.

    AAD: ``"dc-vault-grant-fallback-v1" || vault_id (12 ASCII bytes)``.
    """

    def __init__(self, config_dir: Path, device_seed: bytes) -> None:
        if len(device_seed) < 16:
            raise ValueError("device_seed must be at least 16 bytes")
        self._config_dir = Path(config_dir)
        self._config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Derive the wrap key once and hold in memory; size is small,
        # the KDF is fast (HKDF, not Argon2id), and re-deriving on
        # every operation buys nothing security-wise.
        self._wrap_key = HKDF(
            algorithm=hashes.SHA256(),
            length=XCHACHA20_KEY_BYTES,
            salt=b"\x00" * 32,
            info=_FILE_HKDF_INFO,
        ).derive(device_seed)

    def _path(self, vault_id: str) -> Path:
        return self._config_dir / f"vault_grant_{vault_id}.json"

    def _aad(self, vault_id: str) -> bytes:
        return _FILE_AAD_SCHEMA + vault_id.encode("ascii")

    def save(self, grant: VaultGrant) -> None:
        nonce = os.urandom(XCHACHA20_NONCE_BYTES)
        ct = aead_encrypt(
            grant.to_json().encode("utf-8"),
            self._wrap_key,
            nonce,
            self._aad(grant.vault_id),
        )
        envelope = {
            "schema": "vault-grant-fallback-v1",
            "nonce_b64": base64.b64encode(nonce).decode("ascii"),
            "ciphertext_b64": base64.b64encode(ct).decode("ascii"),
        }
        path = self._path(grant.vault_id)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(envelope, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def load(self, vault_id: str) -> VaultGrant | None:
        path = self._path(vault_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            envelope = json.load(f)
        if envelope.get("schema") != "vault-grant-fallback-v1":
            raise ValueError(f"unknown grant envelope schema: {envelope.get('schema')!r}")
        nonce = base64.b64decode(envelope["nonce_b64"])
        ct = base64.b64decode(envelope["ciphertext_b64"])
        plaintext = aead_decrypt(ct, self._wrap_key, nonce, self._aad(vault_id))
        return VaultGrant.from_json(plaintext.decode("utf-8"))

    def delete(self, vault_id: str) -> None:
        path = self._path(vault_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def zero_wrap_key(self) -> None:
        """Best-effort overwrite of the in-memory wrap key. Call when
        shutting down the process — the OS-level memory is the real
        boundary, but zeroing what we own is still right.
        """
        if isinstance(self._wrap_key, (bytes, bytearray)):
            buf = bytearray(self._wrap_key)
            for i in range(len(buf)):
                buf[i] = 0
            self._wrap_key = bytes(len(buf))


# ---------------------------------------------------------------- factory


def open_default_grant_store(
    *,
    config_dir: Path,
    device_seed_provider,
) -> GrantStore:
    """Pick a backend at startup. Tries keyring first; falls back to
    file-on-disk if the keyring is unreachable.

    ``device_seed_provider`` is a no-arg callable returning bytes — it's
    only invoked when the keyring is unavailable, so the file backend's
    expensive provider (in production: PEM read + parse) doesn't run
    on happy-path startup.
    """
    try:
        store = KeyringGrantStore.open_default()
        log.info("vault_grant.backend.keyring")
        return store
    except KeyringUnavailable as exc:
        log.warning("vault_grant.backend.fallback reason=%s", exc)
        seed = device_seed_provider()
        return FileGrantStore(config_dir=config_dir, device_seed=seed)
