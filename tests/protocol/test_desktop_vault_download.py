"""T5.3 — Vault browser single-file download helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault_crypto import (  # noqa: E402
    DefaultVaultCrypto,
    aead_encrypt,
    build_chunk_aad,
    build_chunk_envelope,
    derive_subkey,
)
from src.vault_download import (  # noqa: E402
    DownloadCancelled,
    VaultChunkMissingError,
    download_latest_file,
    resolve_download_destination,
    vault_chunk_cache_path,
)
from src.vault_manifest import make_manifest, make_remote_folder  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)


FILE_ID = "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
VERSION_ID = "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
CHUNK_A = "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
CHUNK_B = "ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb"


class VaultDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_download_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_download_latest_file_decrypts_chunks_and_writes_atomically(self) -> None:
        expected = b"hello " + b"vault"
        manifest, chunks = _manifest_and_chunks([b"hello ", b"vault"])
        relay = FakeChunkRelay(chunks)
        vault = _vault()
        progress = []
        try:
            out = download_latest_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path="Documents/report.txt",
                destination=self.tmpdir / "report.txt",
                progress=progress.append,
            )
        finally:
            vault.close()

        self.assertEqual(out.read_bytes(), expected)
        self.assertEqual(relay.downloaded, [CHUNK_A, CHUNK_B])
        self.assertFalse(list(self.tmpdir.glob("*.dc-temp-*")))
        self.assertEqual(progress[-1].phase, "done")

    def test_cached_encrypted_chunk_skips_get_after_batch_head(self) -> None:
        manifest, chunks = _manifest_and_chunks([b"cached"])
        relay = FakeChunkRelay(chunks)
        cache_dir = self.tmpdir / "cache"
        cache_path = vault_chunk_cache_path(cache_dir, VAULT_ID, CHUNK_A)
        cache_path.parent.mkdir(parents=True)
        cache_path.write_bytes(chunks[CHUNK_A])
        vault = _vault()
        try:
            out = download_latest_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path="Documents/report.txt",
                destination=self.tmpdir / "report.txt",
                chunk_cache_dir=cache_dir,
            )
        finally:
            vault.close()

        self.assertEqual(out.read_bytes(), b"cached")
        self.assertEqual(relay.batch_head_calls, [[CHUNK_A]])
        self.assertEqual(relay.downloaded, [])

    def test_keep_both_policy_preserves_existing_destination(self) -> None:
        manifest, chunks = _manifest_and_chunks([b"new"])
        relay = FakeChunkRelay(chunks)
        destination = self.tmpdir / "report.txt"
        destination.write_bytes(b"old")
        vault = _vault()
        try:
            out = download_latest_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path="Documents/report.txt",
                destination=destination,
                existing_policy="keep_both",
            )
        finally:
            vault.close()

        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(out.name, "report (downloaded 1).txt")
        self.assertEqual(out.read_bytes(), b"new")

    def test_cancel_policy_raises_before_download(self) -> None:
        destination = self.tmpdir / "report.txt"
        destination.write_bytes(b"old")

        with self.assertRaises(DownloadCancelled):
            resolve_download_destination(destination, "cancel")

    def test_missing_chunk_fails_before_destination_write(self) -> None:
        manifest, chunks = _manifest_and_chunks([b"hello ", b"vault"])
        relay = FakeChunkRelay({CHUNK_A: chunks[CHUNK_A]})
        destination = self.tmpdir / "report.txt"
        vault = _vault()
        try:
            with self.assertRaises(VaultChunkMissingError):
                download_latest_file(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    path="Documents/report.txt",
                    destination=destination,
                )
        finally:
            vault.close()

        self.assertFalse(destination.exists())
        self.assertEqual(relay.downloaded, [])

    def test_tampered_chunk_fails_closed(self) -> None:
        manifest, chunks = _manifest_and_chunks([b"secret"])
        corrupted = bytearray(chunks[CHUNK_A])
        corrupted[-1] ^= 0x01
        relay = FakeChunkRelay({CHUNK_A: bytes(corrupted)})
        cache_dir = self.tmpdir / "cache"
        vault = _vault()
        try:
            with self.assertRaises(Exception):
                download_latest_file(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    path="Documents/report.txt",
                    destination=self.tmpdir / "report.txt",
                    chunk_cache_dir=cache_dir,
                )
        finally:
            vault.close()

        self.assertFalse((self.tmpdir / "report.txt").exists())
        self.assertFalse(vault_chunk_cache_path(cache_dir, VAULT_ID, CHUNK_A).exists())


class FakeChunkRelay:
    def __init__(self, chunks: dict[str, bytes]) -> None:
        self.chunks = dict(chunks)
        self.downloaded: list[str] = []
        self.batch_head_calls: list[list[str]] = []

    def batch_head_chunks(self, vault_id, vault_access_secret, chunk_ids):
        self.batch_head_calls.append(list(chunk_ids))
        return {
            chunk_id: (
                {
                    "present": True,
                    "size": len(self.chunks[chunk_id]),
                    "hash": hashlib.sha256(self.chunks[chunk_id]).hexdigest(),
                }
                if chunk_id in self.chunks
                else {"present": False}
            )
            for chunk_id in chunk_ids
        }

    def get_chunk(self, vault_id, vault_access_secret, chunk_id):
        self.downloaded.append(chunk_id)
        return self.chunks[chunk_id]


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID,
        master_key=MASTER_KEY,
        recovery_secret=None,
        vault_access_secret="vault-secret",
        header_revision=1,
        manifest_revision=1,
        manifest_ciphertext=b"",
        crypto=DefaultVaultCrypto,
    )


def _manifest_and_chunks(parts: list[bytes]) -> tuple[dict, dict[str, bytes]]:
    chunk_ids = [CHUNK_A, CHUNK_B]
    chunks = {}
    chunk_entries = []
    for index, plaintext in enumerate(parts):
        chunk_id = chunk_ids[index]
        encrypted = _encrypt_chunk(plaintext, index)
        chunks[chunk_id] = encrypted
        chunk_entries.append({
            "chunk_id": chunk_id,
            "index": index,
            "plaintext_size": len(plaintext),
            "ciphertext_size": len(encrypted),
        })

    version = {
        "version_id": VERSION_ID,
        "created_at": "2026-05-04T12:00:00.000Z",
        "modified_at": "2026-05-04T11:59:00.000Z",
        "logical_size": sum(len(part) for part in parts),
        "ciphertext_size": sum(len(chunk) for chunk in chunks.values()),
        "content_fingerprint": "unused",
        "chunks": chunk_entries,
        "author_device_id": AUTHOR,
    }
    entry = {
        "entry_id": FILE_ID,
        "type": "file",
        "path": "report.txt",
        "latest_version_id": VERSION_ID,
        "deleted": False,
        "versions": [version],
    }
    manifest = make_manifest(
        vault_id=VAULT_ID,
        revision=20,
        parent_revision=19,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[entry],
            )
        ],
    )
    return manifest, chunks


def _encrypt_chunk(plaintext: bytes, index: int) -> bytes:
    nonce = bytes([index + 1]) * 24
    subkey = derive_subkey("dc-vault-v1/chunk", MASTER_KEY)
    aad = build_chunk_aad(
        VAULT_ID,
        DOCS_ID,
        FILE_ID,
        VERSION_ID,
        index,
        len(plaintext),
    )
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    return build_chunk_envelope(nonce=nonce, aead_ciphertext_and_tag=ciphertext)


if __name__ == "__main__":
    unittest.main()
