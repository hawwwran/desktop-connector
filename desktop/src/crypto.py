"""
End-to-end encryption for Desktop Connector.
X25519 key exchange + HKDF key derivation + AES-256-GCM symmetric encryption.
"""

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization


HKDF_SALT = b"desktop-connector"
HKDF_INFO = b"aes-256-gcm-key"
CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB


class KeyManager:
    """Manages device identity keys and encryption operations."""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.keys_dir = config_dir / "keys"
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.keys_dir, 0o700)

        self._private_key: X25519PrivateKey | None = None
        self._load_or_generate()

    def _load_or_generate(self) -> None:
        key_file = self.keys_dir / "private_key.pem"
        if key_file.exists():
            pem_data = key_file.read_bytes()
            self._private_key = serialization.load_pem_private_key(pem_data, password=None)
        else:
            self._private_key = X25519PrivateKey.generate()
            pem_data = self._private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            key_file.write_bytes(pem_data)
            os.chmod(key_file, 0o600)

    @property
    def private_key(self) -> X25519PrivateKey:
        return self._private_key

    @property
    def public_key(self) -> X25519PublicKey:
        return self._private_key.public_key()

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

    def encrypt_file_to_chunks(self, filepath: Path, key: bytes,
                               filename_override: str | None = None) -> tuple[str, bytes, list[bytes]]:
        """
        Encrypt a file into chunks.
        Returns: (encrypted_meta_b64, base_nonce, list_of_encrypted_chunks)
        """
        data = filepath.read_bytes()
        base_nonce = os.urandom(12)

        chunks = []
        offset = 0
        chunk_index = 0
        while offset < len(data):
            chunk_data = data[offset:offset + CHUNK_SIZE]
            nonce = self.make_chunk_nonce(base_nonce, chunk_index)
            encrypted_chunk = self.encrypt_blob(chunk_data, key, nonce)
            chunks.append(encrypted_chunk)
            offset += CHUNK_SIZE
            chunk_index += 1

        if not chunks:
            nonce = self.make_chunk_nonce(base_nonce, 0)
            chunks.append(self.encrypt_blob(b"", key, nonce))

        actual_name = filename_override or filepath.name
        metadata = {
            "filename": actual_name,
            "mime_type": self._guess_mime(Path(actual_name)),
            "size": len(data),
            "chunk_count": len(chunks),
            "chunk_size": CHUNK_SIZE,
            "base_nonce": base64.b64encode(base_nonce).decode(),
        }
        encrypted_meta = self.encrypt_metadata(metadata, key)

        return encrypted_meta, base_nonce, chunks

    def decrypt_chunks_to_file(self, encrypted_meta_b64: str, encrypted_chunks: list[bytes],
                                key: bytes, save_dir: Path) -> Path:
        """
        Decrypt chunks and save to file.
        Returns the path of the saved file.
        """
        metadata = self.decrypt_metadata(encrypted_meta_b64, key)
        base_nonce = base64.b64decode(metadata["base_nonce"])
        filename = metadata["filename"]

        # Decrypt and reassemble
        plaintext_parts = []
        for i, chunk_blob in enumerate(encrypted_chunks):
            nonce = self.make_chunk_nonce(base_nonce, i)
            # The blob from server includes the nonce prefix, but when we encrypted,
            # we prepended the nonce. When downloading from server, the blob IS the
            # full nonce+ciphertext+tag as stored. So decrypt_blob handles it.
            # However, during encryption we used a specific nonce and encrypt_blob
            # prepended it. For chunks, we used a deterministic nonce, so the blob
            # has that nonce prepended. decrypt_blob will extract and use it.
            plaintext_parts.append(self.decrypt_blob(chunk_blob, key))

        data = b"".join(plaintext_parts)

        # Avoid overwriting existing files
        save_path = save_dir / filename
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            counter = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        save_path.write_bytes(data)
        return save_path

    @staticmethod
    def _guess_mime(filepath: Path) -> str:
        import mimetypes
        mime, _ = mimetypes.guess_type(filepath.name)
        return mime or "application/octet-stream"
