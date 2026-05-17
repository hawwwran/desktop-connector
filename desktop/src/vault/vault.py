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
    build_root_aad,
    build_root_envelope,
    build_shard_aad,
    build_shard_envelope,
    derive_recovery_wrap_key,
    derive_subkey,
    normalize_vault_id,
)
from .manifest import (
    assemble_unified_manifest,
    assert_publishable_revision,
    assert_publishable_root_revision,
    assert_publishable_shard_revision,
    bump_root_revision,
    bump_shard_revision,
    canonical_manifest_json,
    canonical_root_json,
    canonical_shard_json,
    make_folder_shard,
    make_manifest,
    make_root_folder_pointer,
    make_root_manifest,
    normalize_manifest_plaintext,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
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
        failures (see ``temp/finished-plans/desktop-connector-vault-plan-md/VAULT-progress.md``
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

        # Genesis manifest — empty folder list, no op-log entries
        # yet. Phase H transition: the production code paths still use
        # the legacy single-manifest shape, so we publish a legacy
        # manifest envelope on create. Once the Phase H mechanical port
        # flips every caller to the sharded surface, this will switch
        # back to a root envelope (the sharded code in ``vault.py``
        # already supports both via the dual decrypt path).
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
        # after a successful POST. Field names mirror the legacy wire
        # contract (``initial_manifest_*``).
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

    def fetch_root_manifest(self, relay: RelayProtocol, *, local_index=None) -> dict:
        """Fetch + decrypt the current root manifest envelope.

        Returns the canonical-JSON-decoded plaintext as a dict. The
        envelope bytes are cached on the instance under
        ``_root_envelope`` so the legacy ``fetch_unified_manifest``
        path can hash-verify each shard against the trusted root
        without a second fetch.
        """
        if self._closed:
            raise ValueError("vault is closed")
        resp = relay.get_root(self._vault_id, self._vault_access_secret)
        envelope = resp.get("root_ciphertext")
        if not isinstance(envelope, (bytes, bytearray)):
            raise ValueError("relay returned an invalid root ciphertext")
        envelope_bytes = bytes(envelope)
        root = self.decrypt_root_envelope(envelope_bytes)
        self._root_envelope = envelope_bytes
        self._root_revision = int(root["root_revision"])
        # The manifest_ciphertext slot historically held the legacy
        # vault-wide manifest envelope. After Phase D the root is the
        # authoritative top-level envelope; keep the alias populated so
        # decrypt_manifest's local_index path still has a revision to
        # check against the rollback floor.
        self._manifest_ciphertext = envelope_bytes
        self._manifest_revision = self._root_revision
        if local_index is not None:
            self._verify_root_floor_or_raise(root, local_index)
        return root

    def decrypt_root_envelope(self, envelope_bytes: bytes) -> dict:
        """Decrypt a root envelope's bytes to its plaintext dict.

        Standalone path used by CAS conflict handlers that receive the
        server-current root envelope inline in a 409 response (so a
        re-fetch round-trip is avoided). Does NOT touch
        ``_root_envelope`` / ``_root_revision`` — that's the caller's
        choice (most CAS retries don't want the side effect).
        """
        if self._closed:
            raise ValueError("vault is closed")
        if len(envelope_bytes) < 85 + 16:
            raise ValueError("root envelope too short")
        if envelope_bytes[0] != 1:
            raise ValueError(
                f"vault_format_version_unsupported: root format_version={envelope_bytes[0]}"
            )
        revision = int.from_bytes(envelope_bytes[13:21], "big")
        parent_revision = int.from_bytes(envelope_bytes[21:29], "big")
        author_device_id = envelope_bytes[29:61].decode("ascii")
        plaintext = aead_decrypt(
            envelope_bytes[85:],
            derive_subkey("dc-vault-v1/root", bytes(self._master_key)),
            envelope_bytes[61:85],
            build_root_aad(
                vault_id=self._vault_id,
                root_revision=revision,
                parent_root_revision=parent_revision,
                author_device_id=author_device_id,
            ),
        )
        root = json.loads(plaintext.decode("utf-8"))
        return normalize_root_manifest_plaintext(root)

    def publish_root_manifest(
        self,
        relay: RelayProtocol,
        root: dict,
        *,
        local_index=None,
    ) -> dict:
        """Encrypt + CAS-publish a new root revision.

        F-Y21 invariant: ``root_revision == parent_root_revision + 1``
        is enforced before any AEAD encryption.
        """
        if self._closed or not self._master_key:
            raise ValueError("vault is closed")

        normalized = normalize_root_manifest_plaintext(root)
        assert_publishable_root_revision(normalized)
        revision = int(normalized["root_revision"])
        parent_revision = int(normalized["parent_root_revision"])
        author_device_id = str(normalized["author_device_id"])

        envelope_bytes, envelope_hash = self._encrypt_root_envelope(
            normalized, revision, parent_revision, author_device_id,
        )

        relay.put_root(
            self._vault_id,
            self._vault_access_secret,
            expected_current_root_revision=parent_revision,
            new_root_revision=revision,
            parent_root_revision=parent_revision,
            root_hash=envelope_hash,
            root_ciphertext=envelope_bytes,
        )
        self._root_envelope = envelope_bytes
        self._root_revision = revision
        self._manifest_ciphertext = envelope_bytes
        self._manifest_revision = revision
        return normalized

    def fetch_folder_shard(
        self,
        relay: RelayProtocol,
        remote_folder_id: str,
        *,
        expected_shard_hash: str | None = None,
    ) -> dict:
        """Fetch + decrypt a folder shard envelope.

        Per §10.C, a fetched shard's ``sha256(envelope_bytes)`` must equal
        the trusted root pointer's ``shard_hash`` for the same
        ``remote_folder_id``. AEAD alone is not sufficient: a malicious
        or rolled-back relay can serve an authentic *prior* shard
        envelope whose AAD-bound revision pair still matches its own
        embedded bytes, so AEAD decrypt succeeds but the data is stale.
        Pass ``expected_shard_hash=root.remote_folders[i].shard_hash``
        (extracted from a freshly-fetched, AEAD-verified root) to run
        the §10.C compare before decrypt and raise
        :class:`VaultShardHashMismatchError` on drift.

        Callers without a trusted root pointer in hand (e.g. a probe
        test that's setting up state) can omit the kwarg, in which
        case AEAD remains the only integrity gate.
        ``fetch_unified_manifest`` always supplies the hash.
        """
        if self._closed:
            raise ValueError("vault is closed")
        resp = relay.get_shard(self._vault_id, self._vault_access_secret, remote_folder_id)
        envelope = resp.get("shard_ciphertext")
        if not isinstance(envelope, (bytes, bytearray)):
            raise ValueError("relay returned an invalid shard ciphertext")
        envelope_bytes = bytes(envelope)
        if expected_shard_hash is not None and expected_shard_hash != "":
            actual = hashlib.sha256(envelope_bytes).hexdigest()
            if actual != expected_shard_hash:
                from .relay_errors import VaultShardHashMismatchError
                raise VaultShardHashMismatchError(
                    vault_id=self._vault_id,
                    remote_folder_id=remote_folder_id,
                    expected_shard_hash=expected_shard_hash,
                    actual_shard_hash=actual,
                )
        return self.decrypt_shard_envelope(envelope_bytes, remote_folder_id)

    def decrypt_shard_envelope(self, envelope_bytes: bytes, remote_folder_id: str) -> dict:
        """Decrypt a shard envelope's bytes to its plaintext dict.

        Standalone path used by CAS conflict handlers that receive the
        server-current shard envelope inline in a 409 response. The
        caller already knows ``remote_folder_id`` (it's in the
        conflict payload + needed for AAD); the revision pair is
        recovered from the envelope's deterministic prefix.
        """
        if self._closed:
            raise ValueError("vault is closed")
        if len(envelope_bytes) < 115 + 16:
            raise ValueError("shard envelope too short")
        if envelope_bytes[0] != 1:
            raise ValueError(
                f"vault_format_version_unsupported: shard format_version={envelope_bytes[0]}"
            )
        envelope_rf_id = envelope_bytes[13:43].decode("ascii")
        if envelope_rf_id != remote_folder_id:
            raise ValueError(
                f"shard envelope remote_folder_id {envelope_rf_id!r} "
                f"does not match expected {remote_folder_id!r}"
            )
        shard_revision = int.from_bytes(envelope_bytes[43:51], "big")
        parent_shard_revision = int.from_bytes(envelope_bytes[51:59], "big")
        plaintext = aead_decrypt(
            envelope_bytes[115:],
            derive_subkey("dc-vault-v1/shard", bytes(self._master_key)),
            envelope_bytes[91:115],
            build_shard_aad(
                vault_id=self._vault_id,
                remote_folder_id=remote_folder_id,
                shard_revision=shard_revision,
                parent_shard_revision=parent_shard_revision,
                author_device_id=envelope_bytes[59:91].decode("ascii"),
            ),
        )
        shard = json.loads(plaintext.decode("utf-8"))
        return normalize_shard_plaintext(shard)

    def publish_folder_shard(
        self,
        relay: RelayProtocol,
        remote_folder_id: str,
        shard: dict,
    ) -> tuple[dict, str]:
        """Encrypt + CAS-publish a new shard revision for one folder.

        Strongly discouraged for normal edits — use
        :meth:`publish_shard_with_root` so the root's shard_hash stays
        in sync atomically. The standalone publish is for retention-purge
        passes and the one-shot migration script, both of which patch
        the root pointer in a separate publish.

        Returns ``(normalized_shard_plaintext, envelope_hash)``. The
        ``envelope_hash`` is ``sha256(published_envelope_bytes)`` — the
        value the caller MUST store in
        ``root.remote_folders[i].shard_hash`` before the next root
        publish, so §10.C readers can verify this shard. Shard envelope
        encryption is non-deterministic (random nonce), so the caller
        cannot recompute this hash later; it is only knowable at
        publish time, which is why the method returns it.
        """
        if self._closed or not self._master_key:
            raise ValueError("vault is closed")
        normalized = normalize_shard_plaintext(shard)
        assert_publishable_shard_revision(normalized)
        revision = int(normalized["shard_revision"])
        parent_revision = int(normalized["parent_shard_revision"])
        author_device_id = str(normalized["author_device_id"])

        envelope_bytes, envelope_hash = self._encrypt_shard_envelope(
            normalized, remote_folder_id, revision, parent_revision, author_device_id,
        )
        relay.put_shard(
            self._vault_id,
            self._vault_access_secret,
            remote_folder_id,
            expected_current_shard_revision=parent_revision,
            new_shard_revision=revision,
            parent_shard_revision=parent_revision,
            shard_hash=envelope_hash,
            shard_ciphertext=envelope_bytes,
        )
        return normalized, envelope_hash

    def publish_shard_with_root(
        self,
        relay: RelayProtocol,
        remote_folder_id: str,
        shard: dict,
        root: dict,
    ) -> dict:
        """Atomic per-folder shard + root publish (§6.8).

        The **primary** publish path for sync engines. Encrypts the
        shard first, then patches the matching folder pointer in the
        supplied root with ``shard_hash =
        sha256(shard_envelope_bytes)`` and ``shard_revision =
        new_shard_revision`` so the §10.C hash chain holds at the
        wire boundary. Callers only need to set ``shard_hash`` to any
        placeholder (or omit it on a freshly-added pointer); the Vault
        owns the real value because shard envelope encryption is
        non-deterministic and the hash is only knowable post-encrypt.

        The pointer for ``remote_folder_id`` MUST already exist in
        ``root.remote_folders`` — call ``publish_root_manifest`` to
        add a brand-new folder pointer before its first shard publish.
        Returns the normalized shard + root dicts as a tuple of two
        dicts (the root reflects the patched pointer).
        """
        if self._closed or not self._master_key:
            raise ValueError("vault is closed")

        shard_n = normalize_shard_plaintext(shard)
        assert_publishable_shard_revision(shard_n)
        s_rev = int(shard_n["shard_revision"])
        s_parent = int(shard_n["parent_shard_revision"])
        s_author = str(shard_n["author_device_id"])
        shard_env_bytes, shard_env_hash = self._encrypt_shard_envelope(
            shard_n, remote_folder_id, s_rev, s_parent, s_author,
        )

        root_n = normalize_root_manifest_plaintext(root)
        # §10.C: the root pointer for this folder MUST carry the
        # just-computed shard envelope hash. The shard nonce was
        # generated above and never leaves this method, so callers
        # cannot know the right hash; we own it here and patch the
        # pointer before sealing the root.
        pointer_patched = False
        for pointer in root_n.get("remote_folders", []):
            if str(pointer.get("remote_folder_id", "")) == remote_folder_id:
                pointer["shard_hash"] = shard_env_hash
                pointer["shard_revision"] = s_rev
                pointer_patched = True
                break
        if not pointer_patched:
            raise ValueError(
                f"root.remote_folders has no pointer for {remote_folder_id!r}; "
                "publish a root with the new folder pointer first"
            )

        assert_publishable_root_revision(root_n)
        r_rev = int(root_n["root_revision"])
        r_parent = int(root_n["parent_root_revision"])
        r_author = str(root_n["author_device_id"])
        root_env_bytes, root_env_hash = self._encrypt_root_envelope(
            root_n, r_rev, r_parent, r_author,
        )

        relay.put_shard_with_root(
            self._vault_id,
            self._vault_access_secret,
            remote_folder_id,
            shard={
                "expected_current_shard_revision": s_parent,
                "new_shard_revision": s_rev,
                "parent_shard_revision": s_parent,
                "shard_hash": shard_env_hash,
                "shard_ciphertext": shard_env_bytes,
            },
            root={
                "expected_current_root_revision": r_parent,
                "new_root_revision": r_rev,
                "parent_root_revision": r_parent,
                "root_hash": root_env_hash,
                "root_ciphertext": root_env_bytes,
            },
        )
        self._root_envelope = root_env_bytes
        self._root_revision = r_rev
        self._manifest_ciphertext = root_env_bytes
        self._manifest_revision = r_rev
        return shard_n, root_n

    def fetch_unified_manifest(
        self,
        relay: RelayProtocol,
        *,
        local_index=None,
    ) -> dict:
        """Compat surface: fetch the root, then every listed shard, and
        assemble into the legacy unified manifest shape that
        ``Vault.fetch_manifest`` historically returned.

        Used by callers that haven't been ported to the shard-aware
        fetch path yet (Phase E + F migrate them; Phase H removes this
        method). Each shard is fetched with the root pointer's
        ``shard_hash`` as ``expected_shard_hash`` so the §10.C check
        fires per shard — a relay-side per-folder rollback raises
        :class:`VaultShardHashMismatchError` before any plaintext
        entries are consumed.

        Pointers whose ``shard_hash`` is empty (``""``) denote a freshly
        added folder that has not yet had a shard published; the
        ``get_shard`` call would 404 anyway, so we skip the fetch and
        let the assembled view show the pointer with ``entries: []``.
        """
        root = self.fetch_root_manifest(relay, local_index=local_index)
        shards_by_id: dict[str, dict] = {}
        for pointer in root.get("remote_folders", []):
            rf_id = str(pointer.get("remote_folder_id", ""))
            expected_hash = str(pointer.get("shard_hash", ""))
            if expected_hash == "":
                # Pointer exists in the root but no shard has been
                # published yet — skip the fetch.
                continue
            shard = self.fetch_folder_shard(
                relay, rf_id, expected_shard_hash=expected_hash,
            )
            shards_by_id[rf_id] = shard
        return assemble_unified_manifest(root, shards_by_id)

    # -- Legacy fetch_manifest / publish_manifest (Phase D back-compat) --
    #
    # During Phase D these continue to talk to the legacy relay methods
    # (``get_manifest`` / ``put_manifest``). The production
    # ``VaultHttpRelay`` keeps those methods on its surface as raise-
    # NotImplementedError stubs because the matching server endpoints
    # were removed in Phase B — production callers that haven't been
    # migrated yet will crash at runtime. Tests use fake relays that
    # implement the legacy interface byte-identically to the
    # pre-sharding code, so the protocol suite stays green. Phase E
    # ports the sync engine; Phase F ports cross-shard ops; Phase H
    # removes these legacy methods + the unified-manifest assembler.

    def fetch_manifest(self, relay: RelayProtocol, *, local_index=None) -> dict:
        """Fetch, store, decrypt, and optionally cache the current manifest.

        Legacy compat surface (see method-block comment above). New code
        should call ``fetch_root_manifest`` + ``fetch_folder_shard``
        directly so only the relevant folder's shard ships over the
        wire.
        """
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
        """Encrypt and CAS-publish a new (legacy unified) manifest revision.

        Legacy compat surface (see method-block comment above). New code
        should call ``publish_shard_with_root`` for normal edits;
        ``publish_root_manifest`` for folder-set changes;
        ``publish_folder_shard`` for retention-only edits.
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

    # -- Internal envelope-builders --

    def _encrypt_root_envelope(
        self,
        plaintext_dict: dict,
        revision: int,
        parent_revision: int,
        author_device_id: str,
    ) -> tuple[bytes, str]:
        plaintext = canonical_root_json(plaintext_dict)
        key = derive_subkey("dc-vault-v1/root", bytes(self._master_key))
        nonce = secrets.token_bytes(24)
        aad = build_root_aad(
            vault_id=self._vault_id,
            root_revision=revision,
            parent_root_revision=parent_revision,
            author_device_id=author_device_id,
        )
        ct = aead_encrypt(plaintext, key, nonce, aad)
        env = build_root_envelope(
            vault_id=self._vault_id,
            root_revision=revision,
            parent_root_revision=parent_revision,
            author_device_id=author_device_id,
            nonce=nonce,
            aead_ciphertext_and_tag=ct,
        )
        return env, hashlib.sha256(env).hexdigest()

    def _encrypt_shard_envelope(
        self,
        plaintext_dict: dict,
        remote_folder_id: str,
        revision: int,
        parent_revision: int,
        author_device_id: str,
    ) -> tuple[bytes, str]:
        plaintext = canonical_shard_json(plaintext_dict)
        key = derive_subkey("dc-vault-v1/shard", bytes(self._master_key))
        nonce = secrets.token_bytes(24)
        aad = build_shard_aad(
            vault_id=self._vault_id,
            remote_folder_id=remote_folder_id,
            shard_revision=revision,
            parent_shard_revision=parent_revision,
            author_device_id=author_device_id,
        )
        ct = aead_encrypt(plaintext, key, nonce, aad)
        env = build_shard_envelope(
            vault_id=self._vault_id,
            remote_folder_id=remote_folder_id,
            shard_revision=revision,
            parent_shard_revision=parent_revision,
            author_device_id=author_device_id,
            nonce=nonce,
            aead_ciphertext_and_tag=ct,
        )
        return env, hashlib.sha256(env).hexdigest()

    def _verify_root_floor_or_raise(self, root: dict, local_index) -> None:
        """Run the §3.7 rollback floor check against a freshly-fetched root."""
        from .relay_errors import VaultManifestRollbackError
        revision = int(root.get("root_revision", 0))
        floor = local_index.get_manifest_revision_floor(self._vault_id)
        if revision < floor:
            log.warning(
                "vault.manifest.rollback_detected vault_id=%s served=%d floor=%d",
                self._vault_id, revision, floor,
            )
            local_index.record_manifest_rollback(
                self._vault_id,
                served_revision=revision,
                floor_revision=floor,
            )
            raise VaultManifestRollbackError(
                vault_id=self._vault_id,
                served_revision=revision,
                floor_revision=floor,
            )
        local_index.bump_manifest_revision_floor(self._vault_id, revision)
        local_index.clear_manifest_rollback(self._vault_id)
        # NOTE: refresh_remote_folders_cache uses the legacy unified
        # shape (one ``remote_folders`` array with ``entries[]``). It is
        # invoked by the unified-manifest assembly path, not here, so
        # the cache stays consistent with the legacy view consumed by
        # Phase F-era browsers.

    # ---------------------------------------------------------------- decryption helpers

    def decrypt_manifest(self, *, local_index=None) -> dict:
        """AEAD-decrypt the current manifest ciphertext and return the
        canonical-JSON-decoded plaintext as a dict.

        Raises ``ValueError`` if the vault is closed. When
        ``local_index`` is provided, the AEAD-verified revision is
        compared against the per-vault floor; a strict downgrade
        raises :class:`VaultManifestRollbackError` **before** the
        local folder cache is refreshed, so a relay-served older
        state cannot quietly overwrite trusted local state. The
        floor itself only advances on success — this is the trust
        anchor for §3.7 rollback detection.
        """
        from .ui.browser_model import decrypt_manifest
        from .relay_errors import VaultManifestRollbackError

        manifest = decrypt_manifest(self, self._manifest_ciphertext)
        if local_index is not None:
            revision = int(manifest.get("revision", 0))
            floor = local_index.get_manifest_revision_floor(self._vault_id)
            if revision < floor:
                log.warning(
                    "vault.manifest.rollback_detected vault_id=%s served=%d floor=%d",
                    self._vault_id, revision, floor,
                )
                local_index.record_manifest_rollback(
                    self._vault_id,
                    served_revision=revision,
                    floor_revision=floor,
                )
                raise VaultManifestRollbackError(
                    vault_id=self._vault_id,
                    served_revision=revision,
                    floor_revision=floor,
                )
            local_index.refresh_remote_folders_cache(manifest)
            local_index.bump_manifest_revision_floor(self._vault_id, revision)
            # Self-heal: relay has resumed serving fresh state, drop
            # any prior latched warning so the banner clears.
            local_index.clear_manifest_rollback(self._vault_id)
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
