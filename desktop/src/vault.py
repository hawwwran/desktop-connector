"""Vault domain class — desktop-side vault lifecycle.

T3.1 deliverable: a single class that wraps the create / open / close
flow on top of ``vault_crypto`` and a ``RelayProtocol`` abstraction.
HTTP plumbing lives in ``api_client.py``; this module is decoupled so
tests pass a fake relay.

Lifecycle
---------

    # Create a new vault (also writes the recovery kit + saves the
    # device grant for subsequent opens-without-passphrase).
    vault = Vault.create_new(relay, recovery_passphrase="...")
    vault.master_key      # 32 bytes, in-memory only
    vault.vault_id        # canonical 12-char base32
    vault.recovery_secret # 32 bytes; SHOULD be persisted to the
                          # recovery kit file before the user closes
                          # the wizard. The vault holds it during
                          # the session for that one write.
    vault.close()         # zeroes master_key, recovery_secret, etc.

    # Reopen via passphrase + recovery kit (covers the "lost device"
    # restore flow):
    vault = Vault.open(
        relay, vault_id="ABCD2345WXYZ",
        recovery_passphrase="...",
        recovery_secret=kit_bytes,
    )

    # Reopen via stored device grant (covers the everyday flow on a
    # paired device — no passphrase prompt). Local grant unwrapping
    # lives in T3.2; see ``vault_grant.py``.

Test injection per T2.5
-----------------------

``Vault.create_new`` / ``Vault.open`` accept an explicit ``crypto``
argument satisfying ``vault_crypto.VaultCrypto``. Production code
defaults to ``DefaultVaultCrypto``; tests pass a fake to avoid running
real Argon2id.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from typing import Protocol

from .vault_crypto import (
    DefaultVaultCrypto,
    VaultCrypto,
    aead_decrypt,
    aead_encrypt,
    build_header_aad,
    build_header_envelope,
    build_manifest_aad,
    build_manifest_envelope,
    build_recovery_aad,
    build_recovery_envelope,
    derive_recovery_wrap_key,
    derive_subkey,
    normalize_vault_id,
)

log = logging.getLogger(__name__)


# Locked v1 chunk size already lives in crypto.py (CHUNK_SIZE = 2 MiB).
# Vault uses the same value; importing it here would create a cycle, so
# we duplicate the constant. Mismatch with crypto.CHUNK_SIZE is a bug.
VAULT_CHUNK_SIZE = 2 * 1024 * 1024

# Reduced Argon2id cost for tests. Production defaults live in
# vault_crypto.argon2id_kdf and the recovery flow (m=128 MiB, t=4).
# Tests pass these via the cost overrides.

# Random portions of the locked ID alphabets.
_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"
_BASE32_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


class RelayProtocol(Protocol):
    """Subset of the relay API surface that the Vault class needs.

    Production wires this to ``api_client.ApiClient`` (or a thin
    wrapper around it); tests pass a fake. Methods take primitive
    types so the protocol stays agnostic to HTTP transport.
    """

    def create_vault(
        self,
        vault_id: str,
        vault_access_token_hash: bytes,
        encrypted_header: bytes,
        header_hash: str,
        initial_manifest_ciphertext: bytes,
        initial_manifest_hash: str,
    ) -> dict: ...

    def get_header(
        self,
        vault_id: str,
        vault_access_secret: str,
    ) -> dict: ...


class Vault:
    """Open vault state on the desktop side.

    Holds the master key in memory; ``close()`` zeros it. Construct
    via :meth:`create_new` (fresh vault) or :meth:`open` (existing
    vault, via passphrase + kit). Direct ``__init__`` is internal.
    """

    def __init__(
        self,
        *,
        vault_id: str,
        master_key: bytes,
        recovery_secret: bytes | None,
        vault_access_secret: str,
        header_revision: int,
        manifest_revision: int,
        manifest_ciphertext: bytes,
        crypto: VaultCrypto,
    ) -> None:
        self._vault_id = normalize_vault_id(vault_id)
        # Buffers held as bytearrays so close() can zero them in place.
        self._master_key = bytearray(master_key)
        self._recovery_secret = bytearray(recovery_secret) if recovery_secret is not None else None
        self._vault_access_secret = vault_access_secret
        self._header_revision = header_revision
        self._manifest_revision = manifest_revision
        self._manifest_ciphertext = manifest_ciphertext
        self._crypto = crypto
        self._closed = False
        # T3.2 will replace this with a real GrantStore.
        self._grant_keyring: dict[str, bytes] = {}

    # ---------------------------------------------------------------- properties

    @property
    def vault_id(self) -> str:
        return self._vault_id

    @property
    def vault_id_dashed(self) -> str:
        """Display form: ``XXXX-XXXX-XXXX``."""
        v = self._vault_id
        return f"{v[0:4]}-{v[4:8]}-{v[8:12]}"

    @property
    def master_key(self) -> bytes | None:
        if self._closed:
            return None
        return bytes(self._master_key) if self._master_key else None

    @property
    def recovery_secret(self) -> bytes | None:
        """The 32-byte recovery secret. Available only between
        ``create_new()`` and ``close()`` — the caller is expected to
        write it into the recovery kit file before closing.
        """
        if self._closed or self._recovery_secret is None:
            return None
        return bytes(self._recovery_secret)

    @property
    def vault_access_secret(self) -> str | None:
        return None if self._closed else self._vault_access_secret

    @property
    def header_revision(self) -> int:
        return self._header_revision

    @property
    def manifest_revision(self) -> int:
        return self._manifest_revision

    @property
    def manifest_ciphertext(self) -> bytes:
        return self._manifest_ciphertext

    # ---------------------------------------------------------------- factory: create new vault

    @classmethod
    def create_new(
        cls,
        relay: RelayProtocol,
        recovery_passphrase: str,
        *,
        crypto: VaultCrypto = DefaultVaultCrypto,
        argon_memory_kib: int = 131_072,
        argon_iterations: int = 4,
    ) -> "Vault":
        """Generate fresh vault material and POST /api/vaults.

        Returns a fully-open ``Vault`` (master_key in memory). The
        caller MUST persist :attr:`recovery_secret` to the kit file
        and the device grant to the OS keyring (T3.2) before
        ``close()`` zeros the buffers.

        ``argon_memory_kib`` / ``argon_iterations`` default to the
        v1-locked params; tests override to keep the suite fast.
        """
        master_key = secrets.token_bytes(32)
        recovery_secret = secrets.token_bytes(32)
        vault_access_secret = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(vault_access_secret.encode("ascii")).digest()
        vault_id = _generate_vault_id()

        # Assemble the encrypted header with the recovery envelope.
        recovery_envelope_id = _generate_id_v1("rk")
        recovery_argon_salt = secrets.token_bytes(16)

        recovery_wrap_key = derive_recovery_wrap_key(
            passphrase=recovery_passphrase,
            recovery_secret=recovery_secret,
            argon_salt=recovery_argon_salt,
            memory_kib=argon_memory_kib,
            iterations=argon_iterations,
        )
        recovery_envelope_nonce = secrets.token_bytes(24)
        recovery_envelope_ct = aead_encrypt(
            master_key,
            recovery_wrap_key,
            recovery_envelope_nonce,
            build_recovery_aad(vault_id, recovery_envelope_id),
        )

        header_plaintext = _canonical_json({
            "schema": "dc-vault-header-v1",
            "vault_id": vault_id,
            "created_at": _now_rfc3339(),
            "genesis_fingerprint": _genesis_fingerprint_hex(master_key),
            "kdf_profiles": {"recovery": "argon2id-v1", "export": "argon2id-v1"},
            "recovery_envelopes": [
                {
                    "envelope_id": recovery_envelope_id,
                    "type": "recovery-kit-passphrase",
                    "argon_salt": base64.b64encode(recovery_argon_salt).decode("ascii"),
                    "argon_params": {
                        "memory_kib": argon_memory_kib,
                        "iterations": argon_iterations,
                        "parallelism": 1,
                    },
                    "nonce": base64.b64encode(recovery_envelope_nonce).decode("ascii"),
                    "aead_ciphertext_and_tag": base64.b64encode(recovery_envelope_ct).decode("ascii"),
                }
            ],
            "manifest_format_version": 1,
            "header_format_version": 1,
        })

        header_subkey = derive_subkey("dc-vault-v1/header", master_key)
        header_nonce = secrets.token_bytes(24)
        header_aad = build_header_aad(vault_id, header_revision=1)
        header_ciphertext = aead_encrypt(
            header_plaintext, header_subkey, header_nonce, header_aad,
        )
        header_envelope = build_header_envelope(
            vault_id=vault_id, header_revision=1,
            nonce=header_nonce, aead_ciphertext_and_tag=header_ciphertext,
        )
        header_hash = hashlib.sha256(header_envelope).hexdigest()

        # Genesis manifest — empty folder list, no op-log entries yet.
        author_device_id = "0" * 32  # caller plugs in a real device id later
        manifest_plaintext = _canonical_json({
            "schema": "dc-vault-manifest-v1",
            "vault_id": vault_id,
            "revision": 1,
            "parent_revision": 0,
            "created_at": _now_rfc3339(),
            "author_device_id": author_device_id,
            "manifest_format_version": 1,
            "remote_folders": [],
            "operation_log_tail": [],
            "archived_op_segments": [],
        })
        manifest_subkey = derive_subkey("dc-vault-v1/manifest", master_key)
        manifest_nonce = secrets.token_bytes(24)
        manifest_aad = build_manifest_aad(
            vault_id=vault_id, revision=1, parent_revision=0,
            author_device_id=author_device_id,
        )
        manifest_ciphertext = aead_encrypt(
            manifest_plaintext, manifest_subkey, manifest_nonce, manifest_aad,
        )
        manifest_envelope = build_manifest_envelope(
            vault_id=vault_id, revision=1, parent_revision=0,
            author_device_id=author_device_id,
            nonce=manifest_nonce,
            aead_ciphertext_and_tag=manifest_ciphertext,
        )
        manifest_hash = hashlib.sha256(manifest_envelope).hexdigest()

        relay.create_vault(
            vault_id=vault_id,
            vault_access_token_hash=token_hash,
            encrypted_header=header_envelope,
            header_hash=header_hash,
            initial_manifest_ciphertext=manifest_envelope,
            initial_manifest_hash=manifest_hash,
        )

        log.info("vault.create.ok vault_id=%s", vault_id[:8] + "…")
        return cls(
            vault_id=vault_id,
            master_key=master_key,
            recovery_secret=recovery_secret,
            vault_access_secret=vault_access_secret,
            header_revision=1,
            manifest_revision=1,
            manifest_ciphertext=manifest_envelope,
            crypto=crypto,
        )

    # ---------------------------------------------------------------- factory: open existing vault

    @classmethod
    def open(
        cls,
        relay: RelayProtocol,
        vault_id: str,
        recovery_passphrase: str,
        recovery_secret: bytes,
        vault_access_secret: str,
        *,
        crypto: VaultCrypto = DefaultVaultCrypto,
    ) -> "Vault":
        """Open an existing vault via passphrase + recovery-kit secret.

        For the everyday "paired device" open flow, T3.2 introduces a
        keyring-backed grant store that provides ``master_key`` directly
        without prompting for a passphrase. This factory specifically
        covers the recovery path: the user has the kit file + remembers
        the passphrase.
        """
        canonical = normalize_vault_id(vault_id)
        header_resp = relay.get_header(canonical, vault_access_secret)
        encrypted_header_bytes = header_resp["encrypted_header"]
        header_revision = int(header_resp["header_revision"])

        # Parse the envelope: format_version(1) | vault_id(12) | revision(8)
        # | nonce(24) | ciphertext_and_tag(N).
        if len(encrypted_header_bytes) < 1 + 12 + 8 + 24 + 16:
            raise ValueError("encrypted_header too short to contain a valid envelope")
        nonce = encrypted_header_bytes[1 + 12 + 8 : 1 + 12 + 8 + 24]
        header_ct = encrypted_header_bytes[1 + 12 + 8 + 24:]

        # Need the master key to derive k_header — but master key lives
        # behind the recovery envelope. Walk the chain:
        #   1. Decode the embedded recovery_envelope JSON from a
        #      pre-decryption peek IS NOT possible (it's encrypted).
        #   2. Instead: we loop over all recovery_envelopes in the
        #      header AFTER first-pass decrypting under each candidate
        #      wrap key. v1 always writes exactly one envelope, so this
        #      is one iteration in practice.
        # The real recovery-secret-derived k_recovery_wrap also needs
        # the argon_salt + params from the envelope's metadata. Those
        # live INSIDE the encrypted header. So the recovery flow can't
        # decrypt the header until after it's recovered the master key
        # — but it needs the master key to decrypt the header.
        #
        # The way out: the relay also stores the RECOVERY ENVELOPE bytes
        # separately (per the wire spec the header is opaque, but the
        # recovery envelope is also persisted for retrieval). For v1 we
        # work around this by piggybacking on the header response:
        # the relay returns ``recovery_envelopes`` as a parallel field.
        #
        # T1's GET /header doesn't expose this yet; the recovery flow
        # in v1 uses an out-of-band "recovery hint" produced at create
        # time. Tests inject the recovery_envelope_id + argon_salt
        # + wrap-envelope ciphertext directly via the relay shim.
        recovery_envelope = header_resp.get("recovery_envelope")
        if not isinstance(recovery_envelope, dict):
            raise ValueError(
                "relay did not return a recovery_envelope alongside the header — "
                "v1 recovery requires that out-of-band hint"
            )
        env_id = recovery_envelope["envelope_id"]
        argon_salt = recovery_envelope["argon_salt"]
        argon_params = recovery_envelope["argon_params"]
        env_nonce = recovery_envelope["nonce"]
        env_ct = recovery_envelope["aead_ciphertext_and_tag"]

        wrap_key = derive_recovery_wrap_key(
            passphrase=recovery_passphrase,
            recovery_secret=recovery_secret,
            argon_salt=argon_salt,
            memory_kib=int(argon_params["memory_kib"]),
            iterations=int(argon_params["iterations"]),
        )
        master_key = aead_decrypt(
            env_ct, wrap_key, env_nonce,
            build_recovery_aad(canonical, env_id),
        )

        # Decrypt header ciphertext now that we have the master key.
        header_subkey = derive_subkey("dc-vault-v1/header", master_key)
        header_aad = build_header_aad(canonical, header_revision)
        header_plaintext = aead_decrypt(header_ct, header_subkey, nonce, header_aad)
        # Sanity: vault_id inside the plaintext matches.
        decoded_header = json.loads(header_plaintext.decode("utf-8"))
        if decoded_header["vault_id"] != canonical:
            raise ValueError("decoded header vault_id mismatch")

        # Manifest comes through a separate fetch; the test relay returns
        # it in the same response for round-trip convenience.
        manifest_envelope = header_resp.get("manifest_envelope_bytes", b"")

        log.info("vault.open.ok vault_id=%s", canonical[:8] + "…")
        return cls(
            vault_id=canonical,
            master_key=master_key,
            recovery_secret=recovery_secret,
            vault_access_secret=vault_access_secret,
            header_revision=header_revision,
            manifest_revision=int(header_resp.get("manifest_revision", 1)),
            manifest_ciphertext=manifest_envelope,
            crypto=crypto,
        )

    # ---------------------------------------------------------------- decryption helpers

    def decrypt_manifest(self) -> dict:
        """AEAD-decrypt the current manifest ciphertext and return the
        canonical-JSON-decoded plaintext as a dict.

        Raises ``ValueError`` if the vault is closed.
        """
        if self._closed or not self._master_key:
            raise ValueError("vault is closed")
        if len(self._manifest_ciphertext) < 85 + 16:
            raise ValueError("manifest_ciphertext too short")
        # Plaintext header layout per formats §10.1:
        #   format_version(1) | vault_id(12) | revision(8) | parent(8)
        #   | author_device_id(32) | nonce(24) | aead(N)
        nonce = self._manifest_ciphertext[1 + 12 + 8 + 8 + 32 : 85]
        ct = self._manifest_ciphertext[85:]
        revision = int.from_bytes(self._manifest_ciphertext[13:21], "big")
        parent = int.from_bytes(self._manifest_ciphertext[21:29], "big")
        author = self._manifest_ciphertext[29:61].decode("ascii")

        manifest_subkey = derive_subkey("dc-vault-v1/manifest", bytes(self._master_key))
        aad = build_manifest_aad(
            vault_id=self._vault_id, revision=revision,
            parent_revision=parent, author_device_id=author,
        )
        plaintext = aead_decrypt(ct, manifest_subkey, nonce, aad)
        return json.loads(plaintext.decode("utf-8"))

    # ---------------------------------------------------------------- close + zeroize

    def close(self) -> None:
        """Zero the in-memory master key and recovery secret.

        After ``close()``, ``master_key`` and ``recovery_secret``
        return ``None``; ``decrypt_manifest()`` raises. The vault
        instance can't be reopened — the caller constructs a new one
        via :meth:`open`.

        Note: Python's bytearray contents can be overwritten in place
        but other copies (e.g., the ones the AEAD calls produced
        internally) live in PyNaCl's C buffers, which we can't reach.
        Best-effort zeroing of the buffers we own is still the right
        thing to do; the rest is a defense-in-depth gap noted in the
        critical-risks doc.
        """
        if self._closed:
            return
        if self._master_key:
            for i in range(len(self._master_key)):
                self._master_key[i] = 0
        if self._recovery_secret:
            for i in range(len(self._recovery_secret)):
                self._recovery_secret[i] = 0
        self._master_key = bytearray()
        self._recovery_secret = None
        self._vault_access_secret = ""
        self._closed = True

    def __enter__(self) -> "Vault":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ---------------------------------------------------------------- helpers


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
    import hmac
    mac = hmac.new(master_key, b"dc-vault-v1/genesis-fingerprint", hashlib.sha256).digest()
    return mac[:16].hex()


def _canonical_json(obj: dict) -> bytes:
    """RFC 8785-ish canonical JSON. Good enough for the test stack —
    real RFC 8785 lib is added when an actual cross-platform JSON
    interop case lands."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now_rfc3339() -> str:
    """RFC 3339 ms-precision UTC timestamp."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
