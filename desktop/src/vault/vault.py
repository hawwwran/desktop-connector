"""Vault domain class — desktop-side vault lifecycle.

Wraps create / open / close on top of ``vault_crypto`` and a
``RelayProtocol`` abstraction. HTTP plumbing lives in ``api_client``;
this module is decoupled so tests pass a fake relay.

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
import secrets

from .crypto import (
    DefaultVaultCrypto,
    VaultCrypto,
    aead_decrypt,
    aead_encrypt,
    build_header_aad,
    build_header_envelope,
    build_manifest_aad,
    build_manifest_envelope,
    build_recovery_aad,
    derive_recovery_wrap_key,
    derive_subkey,
    normalize_vault_id,
)
from .manifest import (
    assert_publishable_revision,
    canonical_manifest_json,
    make_manifest,
    normalize_manifest_plaintext,
)
from .canonical import _canonical_json, _now_rfc3339
from .ids import _generate_id_v1, _generate_vault_id, _genesis_fingerprint_hex
from .protocols import RelayProtocol
from .remote_folders import RemoteFoldersMixin

log = logging.getLogger(__name__)


# Locked v1 chunk size already lives in crypto.py (CHUNK_SIZE = 2 MiB).
# Vault uses the same value; importing it here would create a cycle, so
# we duplicate the constant. Mismatch with crypto.CHUNK_SIZE is a bug.
VAULT_CHUNK_SIZE = 2 * 1024 * 1024


class Vault(RemoteFoldersMixin):
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
        recovery_envelope_meta: dict | None = None,
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
        # Recovery-envelope plaintext metadata — non-secret values
        # (envelope_id, argon_salt, argon_params, nonce, the AEAD-wrapped
        # master_key ciphertext) needed to re-run the unwrap during a
        # recovery verify. The ciphertext alone is harmless; without
        # both ``passphrase`` and the kit file's ``recovery_secret`` it
        # can't be decrypted. Kept readable after ``close()`` so the
        # wizard can still verify after zeroing the master key.
        self._recovery_envelope_meta = dict(recovery_envelope_meta or {})

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

    @property
    def recovery_envelope_meta(self) -> dict:
        """Non-secret recovery-envelope plaintext fields (envelope_id,
        argon_salt + params, nonce, the wrapped-master-key ciphertext).
        Used by the wizard's verify-recovery step. Survives ``close()``.
        """
        return dict(self._recovery_envelope_meta)

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
        """Generate fresh vault material and POST ``/api/vaults`` in
        one shot. Equivalent to ``prepare_new(...).publish_initial(relay)``.

        The wizard prefers the two-step :meth:`prepare_new` /
        :meth:`publish_initial` pair so it can save the local grant
        BEFORE creating the vault on the relay — that ordering means a
        failed local-persistence step never leaves an orphaned vault row
        on the server. Tests and headless code paths still use this
        one-call form.
        """
        vault = cls.prepare_new(
            recovery_passphrase,
            crypto=crypto,
            argon_memory_kib=argon_memory_kib,
            argon_iterations=argon_iterations,
        )
        vault.publish_initial(relay)
        return vault

    @classmethod
    def prepare_new(
        cls,
        recovery_passphrase: str,
        *,
        crypto: VaultCrypto = DefaultVaultCrypto,
        argon_memory_kib: int = 131_072,
        argon_iterations: int = 4,
    ) -> "Vault":
        """Generate fresh vault material in memory **without touching the relay**.

        Returns a Vault holding the master key, recovery envelope, encrypted
        header, and genesis manifest. The relay POST is deferred until
        :meth:`publish_initial` is called, so the caller can save the local
        device grant first and avoid orphaned relay rows on local-persistence
        failures (see ``docs/plans/desktop-connector-vault-plan-md/VAULT-progress.md``
        on the wizard ordering).

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
        manifest_plaintext = canonical_manifest_json(make_manifest(
            vault_id=vault_id,
            revision=1,
            parent_revision=0,
            created_at=_now_rfc3339(),
            author_device_id=author_device_id,
            remote_folders=[],
            operation_log_tail=[],
            archived_op_segments=[],
        ))
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

        log.info("vault.prepare.ok vault_id=%s", vault_id[:8] + "…")
        instance = cls(
            vault_id=vault_id,
            master_key=master_key,
            recovery_secret=recovery_secret,
            vault_access_secret=vault_access_secret,
            header_revision=1,
            manifest_revision=1,
            manifest_ciphertext=manifest_envelope,
            crypto=crypto,
            recovery_envelope_meta={
                "envelope_id": recovery_envelope_id,
                "argon_salt": recovery_argon_salt,
                "argon_memory_kib": argon_memory_kib,
                "argon_iterations": argon_iterations,
                "nonce": recovery_envelope_nonce,
                "aead_ciphertext_and_tag": recovery_envelope_ct,
            },
        )
        # Publish payload is held on the instance so a later
        # publish_initial() retry uses byte-identical bundles. Cleared
        # after a successful POST.
        instance._pending_publish = {
            "vault_id": vault_id,
            "vault_access_token_hash": token_hash,
            "encrypted_header": header_envelope,
            "header_hash": header_hash,
            "initial_manifest_ciphertext": manifest_envelope,
            "initial_manifest_hash": manifest_hash,
        }
        return instance

    def publish_initial(self, relay: RelayProtocol) -> None:
        """POST the prepared bundle to ``/api/vaults``.

        Idempotent under retry: keeps the publish payload on the instance
        until the relay accepts it. After success the payload is cleared
        and a second call raises (the vault is already on the relay).
        """
        if self._closed:
            raise ValueError("vault is closed")
        payload = getattr(self, "_pending_publish", None)
        if not payload:
            raise ValueError("vault has no pending publish (already published or opened from grant)")
        relay.create_vault(**payload)
        self._pending_publish = None
        log.info("vault.publish.ok vault_id=%s", self._vault_id[:8] + "…")

    @property
    def has_pending_publish(self) -> bool:
        """True between prepare_new() and a successful publish_initial()."""
        return bool(getattr(self, "_pending_publish", None))

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

    @classmethod
    def from_grant(
        cls,
        grant,
        *,
        crypto: VaultCrypto = DefaultVaultCrypto,
    ) -> "Vault":
        """Open in-memory vault state from a local per-device grant."""
        return cls(
            vault_id=grant.vault_id,
            master_key=grant.master_key,
            recovery_secret=None,
            vault_access_secret=grant.vault_access_secret,
            header_revision=0,
            manifest_revision=0,
            manifest_ciphertext=b"",
            crypto=crypto,
        )

    # ---------------------------------------------------------------- manifest helpers

    def fetch_manifest(self, relay: RelayProtocol, *, local_index=None) -> dict:
        """Fetch, store, decrypt, and optionally cache the current manifest."""
        if self._closed:
            raise ValueError("vault is closed")
        resp = relay.get_manifest(self._vault_id, self._vault_access_secret)
        envelope = resp.get("manifest_ciphertext", resp.get("manifest_envelope_bytes"))
        if not isinstance(envelope, (bytes, bytearray)):
            raise ValueError("relay returned an invalid manifest ciphertext")
        self._manifest_ciphertext = bytes(envelope)
        self._manifest_revision = int(resp.get("revision", self._manifest_revision or 0))
        return self.decrypt_manifest(local_index=local_index)

    def publish_manifest(
        self,
        relay: RelayProtocol,
        manifest: dict,
        *,
        local_index=None,
    ) -> dict:
        """Encrypt and CAS-publish a new manifest revision.

        F-Y21: enforces the §A8 revision invariant
        (``revision == parent_revision + 1``) at the publish boundary.
        Callers that mutate the manifest body (tombstone / restore /
        folder add/remove / merge) own the revision bump; passing in a
        manifest whose revision pair is inconsistent with the bump
        raises :class:`ManifestRevisionInvariantError` *before* any
        AEAD encryption or relay POST. The enforcement is here rather
        than scattered across every helper because every code path
        eventually reaches this method to publish.
        """
        if self._closed or not self._master_key:
            raise ValueError("vault is closed")

        normalized = normalize_manifest_plaintext(manifest)
        assert_publishable_revision(normalized)
        revision = int(normalized["revision"])
        parent_revision = int(normalized["parent_revision"])
        author_device_id = str(normalized["author_device_id"])

        manifest_plaintext = canonical_manifest_json(normalized)
        manifest_subkey = derive_subkey("dc-vault-v1/manifest", bytes(self._master_key))
        manifest_nonce = secrets.token_bytes(24)
        manifest_aad = build_manifest_aad(
            vault_id=self._vault_id,
            revision=revision,
            parent_revision=parent_revision,
            author_device_id=author_device_id,
        )
        manifest_ciphertext = aead_encrypt(
            manifest_plaintext, manifest_subkey, manifest_nonce, manifest_aad,
        )
        manifest_envelope = build_manifest_envelope(
            vault_id=self._vault_id,
            revision=revision,
            parent_revision=parent_revision,
            author_device_id=author_device_id,
            nonce=manifest_nonce,
            aead_ciphertext_and_tag=manifest_ciphertext,
        )
        manifest_hash = hashlib.sha256(manifest_envelope).hexdigest()

        relay.put_manifest(
            self._vault_id,
            self._vault_access_secret,
            expected_current_revision=parent_revision,
            new_revision=revision,
            parent_revision=parent_revision,
            manifest_hash=manifest_hash,
            manifest_ciphertext=manifest_envelope,
        )
        self._manifest_revision = revision
        self._manifest_ciphertext = manifest_envelope
        if local_index is not None:
            local_index.refresh_remote_folders_cache(normalized)
        return normalized

    # ---------------------------------------------------------------- decryption helpers

    def decrypt_manifest(self, *, local_index=None) -> dict:
        """AEAD-decrypt the current manifest ciphertext and return the
        canonical-JSON-decoded plaintext as a dict.

        Raises ``ValueError`` if the vault is closed.
        """
        from ..vault_browser_model import decrypt_manifest

        manifest = decrypt_manifest(self, self._manifest_ciphertext)
        if local_index is not None:
            local_index.refresh_remote_folders_cache(manifest)
        return manifest

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
