"""
End-to-end encryption for Desktop Connector.
X25519 key exchange + HKDF key derivation + AES-256-GCM symmetric encryption.
"""

import base64
import hashlib
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization

from .secrets import (
    SECRET_KEY_PRIVATE_KEY_PEM,
    SecretServiceUnavailable,
    SecretStore,
)


log = logging.getLogger(__name__)


HKDF_SALT = b"desktop-connector"
HKDF_INFO = b"aes-256-gcm-key"
CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB

PRIVATE_KEY_FILENAME = "private_key.pem"
PRIVATE_KEY_FILE_MODE = 0o600
KEYS_DIR_MODE = 0o700


class KeyManager:
    """Manages device identity keys and encryption operations.

    Persistence (H.7): the long-term X25519 private key lives in the
    OS keyring (libsecret / KWallet) when one is reachable, with the
    on-disk PEM file as the fallback for headless deployments and
    for callers that don't pass a secret store. The two storage
    backends are mutually exclusive at any moment — at most one
    holds the live key — and migration in either direction is
    handled lazily on init.

    Backwards compatibility: pre-H.7 callers (and tests that pass no
    secret store) get the legacy PEM-only behaviour. Post-H.7
    callers pass the same store ``Config`` selected via
    ``open_default_store`` — see :attr:`Config.secret_store`.
    """

    def __init__(
        self,
        config_dir: Path,
        secret_store: SecretStore | None = None,
    ):
        self.config_dir = config_dir
        self.keys_dir = config_dir / "keys"
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.keys_dir, KEYS_DIR_MODE)

        self._secret_store = secret_store
        # H.7 diagnostic surface: True iff this init moved a PEM file
        # into the secret store. Consumed by main.py's --scrub-secrets
        # CLI handler and the Settings Verify flow to roll up a
        # combined "what was scrubbed this call" message.
        self._migrated_from_pem: bool = False

        self._private_key: X25519PrivateKey | None = None
        self._load_or_generate()

    # --- properties ---------------------------------------------------

    @property
    def was_pem_migrated(self) -> bool:
        """True iff a PEM file was migrated into the secret store
        during the most recent init or :meth:`scrub_private_key` call.

        Read-only signal — the underlying flag flips off each time
        a fresh load runs that finds nothing to migrate."""
        return self._migrated_from_pem

    @property
    def private_key(self) -> X25519PrivateKey:
        return self._private_key

    @property
    def public_key(self) -> X25519PublicKey:
        return self._private_key.public_key()

    # --- key lifecycle ------------------------------------------------

    def reset_keys(self) -> None:
        """Wipe the keypair from EVERY backend and regenerate a fresh
        one. Because :meth:`get_device_id` is a hash of the public key,
        the next call yields a new device_id — used by the
        AUTH_INVALID recovery when the server's record of this device
        has drifted beyond repair.

        H.7 mandate: the wipe must touch the secret store too,
        otherwise the next init would find the old key still present
        and ignore the regeneration entirely. ``reset_keys`` is the
        one method that has to know about both backends explicitly.
        """
        if self._secret_store is not None:
            try:
                self._secret_store.delete(SECRET_KEY_PRIVATE_KEY_PEM)
            except SecretServiceUnavailable as exc:
                log.warning(
                    "config.secrets.private_key.reset_store_failed reason=%s",
                    exc,
                )
        pem_path = self.keys_dir / PRIVATE_KEY_FILENAME
        pem_path.unlink(missing_ok=True)
        self._private_key = None
        self._migrated_from_pem = False
        self._load_or_generate()

    def scrub_private_key(self) -> bool:
        """Re-scan for a stray ``private_key.pem`` and migrate it into
        the secret store. Returns ``True`` iff a migration occurred.

        Companion to :meth:`Config.scrub_secrets` — covers the
        scenario where the PEM file was put back manually (e.g.
        restored from a KeePassXC backup) after the original
        migration had moved it into the keyring. ``__init__`` already
        runs the same check on every construction, so under normal
        operation this method is a no-op; it exists for the
        long-running-process / Settings-Verify flow.

        No-op when:
          - No secret store was provided (PEM-only mode by design)
          - The active store is insecure (JSON fallback — nowhere
            secure to migrate to)
          - The PEM file isn't present
          - The store already holds a value (caller probably wants
            :meth:`reset_keys` if they're trying to swap in the
            disk PEM as the new identity)
        """
        if self._secret_store is None:
            return False
        if not self._secret_store.is_secure():
            return False
        pem_path = self.keys_dir / PRIVATE_KEY_FILENAME
        if not pem_path.exists():
            return False
        try:
            existing = self._secret_store.get(SECRET_KEY_PRIVATE_KEY_PEM)
        except SecretServiceUnavailable as exc:
            log.warning(
                "config.secrets.private_key.scrub_read_failed reason=%s",
                exc,
            )
            return False
        if existing:
            # Store already authoritative. Stale PEM — defensively
            # remove so future loads see a single source of truth.
            self._delete_pem_file(pem_path)
            log.info("config.secrets.private_key.stale_pem_removed")
            return False
        return self._migrate_pem_to_store(pem_path)

    # --- private: load/generate orchestration -------------------------

    def _load_or_generate(self) -> None:
        """Resolve the private key from whichever backend is live."""
        self._migrated_from_pem = False
        pem_path = self.keys_dir / PRIVATE_KEY_FILENAME

        if self._secret_store is not None and self._secret_store.is_secure():
            if self._load_via_store(pem_path):
                return
            # Store-side fully failed (read AND write threw). Drop
            # through to the PEM path so we degrade gracefully rather
            # than crashing the desktop.

        self._load_from_pem(pem_path)

    def _load_via_store(self, pem_path: Path) -> bool:
        """Attempt the store-backed load path. Returns True iff
        ``self._private_key`` is populated by the time we return.

        Three sub-paths:
          (a) store has a key — load it; clean up any stale PEM.
          (b) store is empty AND a PEM exists — migrate.
          (c) store is empty AND no PEM — generate fresh into store.

        Any :class:`SecretServiceUnavailable` from a probe-time
        call returns False so the outer flow can retry from PEM.
        Mid-flow failures (e.g. ``set`` raises after a successful
        ``get``) prefer to keep the loaded private key in memory
        and log; we don't lose state we already have just because
        the store stopped cooperating.
        """
        try:
            existing = self._secret_store.get(SECRET_KEY_PRIVATE_KEY_PEM)
        except SecretServiceUnavailable as exc:
            log.warning(
                "config.secrets.private_key.store_read_failed reason=%s "
                "(falling back to PEM)",
                exc,
            )
            return False

        # (a) Authoritative key already in the store.
        if existing:
            try:
                self._private_key = serialization.load_pem_private_key(
                    existing.encode("ascii"), password=None,
                )
            except (ValueError, TypeError) as exc:
                # Corrupt entry. Refuse to silently overwrite; the
                # operator should triage with seahorse.
                log.error(
                    "config.secrets.private_key.store_corrupt reason=%s "
                    "(refusing to regenerate; remove the keyring entry "
                    "manually if you want a fresh identity)", exc,
                )
                raise
            if pem_path.exists():
                self._delete_pem_file(pem_path)
                log.info("config.secrets.private_key.stale_pem_removed")
            return True

        # (b) Migrate an existing PEM into the store.
        if pem_path.exists():
            return self._migrate_pem_to_store(pem_path)

        # (c) Fresh install on a secure store — generate into the keyring.
        return self._generate_into_store(pem_path)

    def _migrate_pem_to_store(self, pem_path: Path) -> bool:
        """Copy ``pem_path`` into the store, then delete the file.

        On a partial failure (key parsed OK but ``store.set`` raises),
        leaves the PEM in place — next boot retries. On total failure
        (PEM unreadable / unparseable), leaves it for human triage.
        """
        try:
            pem_data = pem_path.read_bytes()
        except OSError as exc:
            log.error(
                "config.secrets.private_key.pem_read_failed reason=%s",
                exc,
            )
            raise
        try:
            self._private_key = serialization.load_pem_private_key(
                pem_data, password=None,
            )
        except (ValueError, TypeError) as exc:
            log.error(
                "config.secrets.private_key.pem_parse_failed reason=%s "
                "(leaving file in place for triage)", exc,
            )
            raise

        try:
            self._secret_store.set(
                SECRET_KEY_PRIVATE_KEY_PEM, pem_data.decode("ascii"),
            )
        except SecretServiceUnavailable as exc:
            log.warning(
                "config.secrets.private_key.migration_failed reason=%s "
                "(plaintext PEM left in place; retrying next boot)",
                exc,
            )
            return True  # we have a key (from PEM); skip PEM-fallback path

        self._delete_pem_file(pem_path)
        self._migrated_from_pem = True
        log.info(
            "config.secrets.private_key.migrated bytes=%d",
            len(pem_data),
        )
        return True

    def _generate_into_store(self, pem_path: Path) -> bool:
        """Generate a fresh keypair and persist it directly into the
        secret store. If the ``set`` call fails, fall back to writing
        a PEM file so the desktop doesn't lose its identity over a
        transient keyring outage."""
        self._private_key = X25519PrivateKey.generate()
        pem_data = self._private_pem_bytes()
        try:
            self._secret_store.set(
                SECRET_KEY_PRIVATE_KEY_PEM, pem_data.decode("ascii"),
            )
        except SecretServiceUnavailable as exc:
            log.warning(
                "config.secrets.private_key.generate_to_keyring_failed "
                "reason=%s (writing PEM as fallback)", exc,
            )
            self._write_pem(pem_path, pem_data)
            return True
        log.info("config.secrets.private_key.generated_to_keyring")
        return True

    def _load_from_pem(self, pem_path: Path) -> None:
        """Pre-H.7 path: PEM file is the source of truth. Used when
        no secret store was provided (tests, AppImage subprocesses
        that don't need encryption metadata) or when the store probe
        is failing this boot."""
        if pem_path.exists():
            pem_data = pem_path.read_bytes()
            self._private_key = serialization.load_pem_private_key(
                pem_data, password=None,
            )
            return
        self._private_key = X25519PrivateKey.generate()
        self._write_pem(pem_path, self._private_pem_bytes())

    # --- private: file IO helpers -------------------------------------

    def _private_pem_bytes(self) -> bytes:
        """Serialize the in-memory private key to PEM bytes (PKCS8,
        no encryption — the at-rest protection comes from the secret
        store or filesystem perms, not from the PEM itself)."""
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def _write_pem(self, pem_path: Path, pem_data: bytes) -> None:
        """Write the PEM with strict permissions. Atomic enough for
        our purposes — the keyring is the authoritative target on
        most installs; the PEM is just a fallback artifact."""
        pem_path.write_bytes(pem_data)
        os.chmod(pem_path, PRIVATE_KEY_FILE_MODE)

    def _delete_pem_file(self, pem_path: Path) -> None:
        """Remove the PEM file. Logs but doesn't raise — leaving an
        orphan PEM after a successful keyring write is annoying but
        not security-critical (the keyring is now authoritative; the
        next boot's ``_load_via_store`` cleans up via the
        ``stale_pem_removed`` path)."""
        try:
            pem_path.unlink()
        except OSError as exc:
            log.warning(
                "config.secrets.private_key.pem_unlink_failed reason=%s",
                exc,
            )

    # --- public-key derivations + cryptographic operations ------------

    def get_public_key_bytes(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def get_public_key_b64(self) -> str:
        return base64.b64encode(self.get_public_key_bytes()).decode()

    def get_device_id(self) -> str:
        raw = self.get_public_key_bytes()
        return hashlib.sha256(raw).hexdigest()[:32]

    def derive_shared_key(self, their_public_key_b64: str) -> bytes:
        """X25519 ECDH + HKDF-SHA256 → 32-byte AES key."""
        their_raw = base64.b64decode(their_public_key_b64)
        their_pubkey = X25519PublicKey.from_public_bytes(their_raw)
        shared_secret = self._private_key.exchange(their_pubkey)

        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=HKDF_SALT,
            info=HKDF_INFO,
        ).derive(shared_secret)
        return derived

    @staticmethod
    def get_verification_code(shared_key: bytes) -> str:
        """First 3 bytes of SHA-256(shared_key) displayed as XXX-XXX."""
        digest = hashlib.sha256(shared_key).digest()
        num = int.from_bytes(digest[:3], "big") % 1000000
        code = f"{num:06d}"
        return f"{code[:3]}-{code[3:]}"

    @staticmethod
    def encrypt_blob(plaintext: bytes, key: bytes, nonce: bytes | None = None) -> bytes:
        """AES-256-GCM encrypt. Returns nonce(12) + ciphertext + tag(16)."""
        if nonce is None:
            nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ciphertext

    @staticmethod
    def decrypt_blob(blob: bytes, key: bytes) -> bytes:
        """AES-256-GCM decrypt. Expects nonce(12) + ciphertext + tag(16)."""
        nonce = blob[:12]
        ciphertext = blob[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)

    @staticmethod
    def make_chunk_nonce(base_nonce: bytes, chunk_index: int) -> bytes:
        """Derive per-chunk nonce: base_nonce XOR chunk_index (little-endian padded to 12 bytes)."""
        index_bytes = chunk_index.to_bytes(12, "little")
        return bytes(a ^ b for a, b in zip(base_nonce, index_bytes))

    @staticmethod
    def encrypt_metadata(metadata: dict, key: bytes) -> str:
        """Encrypt metadata dict → base64 blob."""
        plaintext = json.dumps(metadata).encode()
        blob = KeyManager.encrypt_blob(plaintext, key)
        return base64.b64encode(blob).decode()

    @staticmethod
    def decrypt_metadata(blob_b64: str, key: bytes) -> dict:
        """Decrypt base64 blob → metadata dict."""
        blob = base64.b64decode(blob_b64)
        plaintext = KeyManager.decrypt_blob(blob, key)
        return json.loads(plaintext)

    # --- Streaming file encryption / decryption ---

    @staticmethod
    def generate_base_nonce() -> bytes:
        """Generate a random 12-byte base nonce for a transfer."""
        return os.urandom(12)

    @classmethod
    def build_encrypted_metadata(cls, filename: str, mime_type: str, size: int,
                                 chunk_count: int, base_nonce: bytes,
                                 key: bytes) -> str:
        """Build and encrypt the per-transfer metadata blob."""
        metadata = {
            "filename": filename,
            "mime_type": mime_type,
            "size": size,
            "chunk_count": chunk_count,
            "chunk_size": CHUNK_SIZE,
            "base_nonce": base64.b64encode(base_nonce).decode(),
        }
        return cls.encrypt_metadata(metadata, key)

    @classmethod
    def encrypt_chunk(cls, plaintext: bytes, base_nonce: bytes,
                      index: int, key: bytes) -> bytes:
        """Encrypt a single plaintext chunk using the per-chunk derived nonce."""
        nonce = cls.make_chunk_nonce(base_nonce, index)
        return cls.encrypt_blob(plaintext, key, nonce)

    @classmethod
    def decrypt_chunk(cls, blob: bytes, key: bytes) -> bytes:
        """Decrypt a single encrypted chunk. The blob carries its nonce prefix."""
        return cls.decrypt_blob(blob, key)

    @staticmethod
    def guess_mime(filename: str) -> str:
        import mimetypes
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"
