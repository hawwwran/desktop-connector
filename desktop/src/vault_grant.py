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

from .vault.atomic import atomic_write_file

from .vault.crypto import (
    XCHACHA20_KEY_BYTES,
    XCHACHA20_NONCE_BYTES,
    aead_decrypt,
    aead_encrypt,
    normalize_vault_id,
)

log = logging.getLogger(__name__)


_DEFAULT_KEYRING_SERVICE = "desktop-connector"
_KEYRING_KEY_PREFIX = "vault_grant:"


def _resolve_keyring_service(config_dir: Path | None) -> str:
    """Derive the keyring service name from ``config_dir``.

    Mirrors the per-``config_dir`` derivation that ``Config.__init__``
    does for ``auth_token`` / ``device_id``: a non-default
    ``--config-dir`` (e.g. the vault automation harness's
    ``~/.config/desktop-connector-dev``) lands in keyring service
    ``desktop-connector-dev``, fully isolated from the canonical
    install at ``desktop-connector``. Without this, a dev twin
    saving a vault grant clobbers (or aliases) the user's real
    keyring under the canonical service name.

    The ``DC_KEYRING_SERVICE`` env var is still honoured as a global
    override; otherwise the basename of the config dir wins, falling
    back to the canonical name when no config dir is supplied.
    """
    override = os.environ.get("DC_KEYRING_SERVICE")
    if override:
        return override
    if config_dir is None:
        return _DEFAULT_KEYRING_SERVICE
    name = Path(config_dir).name.strip()
    return name or _DEFAULT_KEYRING_SERVICE

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
        """Best-effort overwrite of the in-memory master key + access secret.

        Limitation (F-C16): ``to_json()`` and ``from_json()`` allocate
        immutable ``bytes`` and ``str`` copies of the same material that
        Python doesn't expose deterministic zeroing for. Calling
        ``zero()`` after a serialization round-trip leaves those copies
        live until garbage-collected. The OS-level memory boundary is
        the real defence; zeroing what we own is still right but isn't
        a complete scrub. A future ``with_master_key`` context-manager
        API could avoid the round-trips entirely.
        """
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

    The ``service_name`` is the libsecret / Secret Service collection
    attribute the entries land under. Production threads it from the
    caller's ``config_dir.name`` (via :func:`_resolve_keyring_service`)
    so a dev twin running with ``--config-dir=~/.config/desktop-connector-dev``
    doesn't write into the canonical install's namespace.
    """

    def __init__(
        self,
        keyring_module,
        service_name: str = _DEFAULT_KEYRING_SERVICE,
    ) -> None:
        self._kr = keyring_module
        self._service = service_name

    @classmethod
    def open_default(
        cls,
        service_name: str = _DEFAULT_KEYRING_SERVICE,
    ) -> "KeyringGrantStore":
        try:
            import keyring as keyring_module
            from keyring.errors import NoKeyringError  # noqa: F401
        except ImportError as exc:
            raise KeyringUnavailable("keyring package not installed") from exc

        # Probe: getting a known-absent key on a working backend returns
        # None; on a non-functional backend raises.
        try:
            keyring_module.get_password(service_name, "_probe_no_such_key")
        except Exception as exc:
            raise KeyringUnavailable(f"keyring probe failed: {exc}") from exc

        return cls(keyring_module, service_name)

    @staticmethod
    def _key(vault_id: str) -> str:
        # F-C18: normalize so save/load symmetry is independent of dashing.
        return _KEYRING_KEY_PREFIX + normalize_vault_id(vault_id)

    def save(self, grant: VaultGrant) -> None:
        self._kr.set_password(self._service, self._key(grant.vault_id), grant.to_json())

    def load(self, vault_id: str) -> VaultGrant | None:
        raw = self._kr.get_password(self._service, self._key(vault_id))
        if raw is None:
            return None
        return VaultGrant.from_json(raw)

    def delete(self, vault_id: str) -> None:
        try:
            self._kr.delete_password(self._service, self._key(vault_id))
        except Exception as exc:
            # ``keyring`` raises PasswordDeleteError when the entry
            # doesn't exist; idempotent delete is more useful.
            log.debug("vault_grant.keyring.delete_noop %s: %s", vault_id, exc)

    def has_grant(self, vault_id: str) -> bool:
        """F-U15: cheap probe — does an entry exist for ``vault_id``?

        Avoids the JSON parse + ``VaultGrant.from_json`` allocation that
        ``load`` performs; the tray calls this on every menu refresh
        so it shouldn't pay decoding cost just to know "is there
        anything here at all".
        """
        try:
            return self._kr.get_password(
                self._service, self._key(vault_id),
            ) is not None
        except Exception:
            # Any keyring backend hiccup → "no grant we can prove" is
            # the safer answer; the file-fallback probe will still run.
            return False


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
        return fallback_grant_path(self._config_dir, vault_id)

    def _aad(self, vault_id: str) -> bytes:
        # F-C18: bind AAD to the canonical vault id so save/load
        # symmetry doesn't depend on whether the caller passed dashed
        # or undashed input.
        return _FILE_AAD_SCHEMA + normalize_vault_id(vault_id).encode("ascii")

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
        payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        atomic_write_file(path, payload, mode=0o600)

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

    def has_grant(self, vault_id: str) -> bool:
        """F-U15: cheap probe — does the on-disk grant file exist?

        File-existence is enough; we don't need to decrypt to know
        "is there anything here". The wrap key probe still happens
        on actual ``load``.
        """
        return self._path(vault_id).exists()

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

    The keyring service name is derived from ``config_dir.name`` so a
    non-default ``--config-dir`` cannot leak its grant into the
    canonical install's keyring namespace (suite 0002 test 04 spotted
    this — the dev twin's vault grant landed under service
    ``desktop-connector`` instead of ``desktop-connector-dev``).
    """
    service = _resolve_keyring_service(config_dir)
    try:
        store = KeyringGrantStore.open_default(service_name=service)
        log.info("vault_grant.backend.keyring service=%s", service)
        return store
    except KeyringUnavailable as exc:
        log.warning("vault_grant.backend.fallback reason=%s", exc)
        seed = device_seed_provider()
        return FileGrantStore(config_dir=config_dir, device_seed=seed)


def fallback_grant_path(config_dir: Path, vault_id: str) -> Path:
    """Fallback grant file path for a vault id.

    Kept as a free function so disconnect flows can remove the local
    file without needing the device seed required to decrypt it.
    Normalizes the vault id so dashed / undashed callers all reach the
    same on-disk file (F-C18).
    """
    return Path(config_dir) / f"vault_grant_{normalize_vault_id(vault_id)}.json"


def local_vault_grant_exists(config_dir: Path, vault_id: str) -> bool:
    """F-U15: authoritative answer for "does this device have an unlock
    grant for ``vault_id``?".

    Used by the tray to decide whether the Vault submenu should show
    Create / Import (no grant) or Open / Sync / Settings (grant
    present). The previous heuristic checked only
    ``config['vault']['last_known_id']`` — that admits a stale-config
    race where the id stays in config but the grant artifact has been
    deleted out from under it (manual keyring purge, OS-keyring
    switch, half-published wizard run that left config rewritten but
    the grant gone).

    Probes the keyring first, falls back to the on-disk file's
    presence — neither requires the device seed (so callers don't
    need to plumb crypto.KeyManager through). Returns ``False`` on
    any backend error: surfacing Create / Import on a "we can't
    prove a grant exists" machine is the safer recovery affordance.
    """
    if not vault_id:
        return False
    canonical = normalize_vault_id(vault_id)
    service = _resolve_keyring_service(config_dir)
    try:
        store = KeyringGrantStore.open_default(service_name=service)
    except KeyringUnavailable:
        store = None
    except Exception:
        store = None
    if store is not None and store.has_grant(canonical):
        return True
    return fallback_grant_path(config_dir, canonical).exists()


def delete_local_grant_artifacts(config_dir: Path, vault_id: str) -> None:
    """Best-effort removal of this machine's grant for ``vault_id``.

    Disconnecting a vault is a local operation: it must remove the
    machine's unlock material but must not call the relay or delete the
    vault itself. Try both storage locations because a machine may have
    switched between keyring and file fallback over time.

    F-D20: failures hit ``log.warning`` *and* are aggregated into a
    final ``RuntimeError`` raised after the loop. Without the raise, a
    read-only config dir (stale Docker volume, mis-mounted ZFS) lets
    disconnect "succeed" while leaving sensitive grant material on
    disk. The aggregation lets the caller surface a single
    user-visible "partial disconnect" message instead of multiple.
    Missing files (the normal case) are not errors.
    """
    errors: list[tuple[str, BaseException]] = []
    service = _resolve_keyring_service(config_dir)
    try:
        KeyringGrantStore.open_default(service_name=service).delete(vault_id)
    except KeyringUnavailable:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault_grant.keyring.disconnect_delete_failed vault=%s error=%s",
            vault_id, exc,
        )
        errors.append(("keyring", exc))

    try:
        fallback_grant_path(config_dir, vault_id).unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault_grant.file.disconnect_delete_failed vault=%s error=%s",
            vault_id, exc,
        )
        errors.append(("file", exc))

    if errors:
        details = "; ".join(f"{src}: {exc}" for src, exc in errors)
        raise RuntimeError(
            f"partial vault disconnect for {vault_id}: {details}"
        )
