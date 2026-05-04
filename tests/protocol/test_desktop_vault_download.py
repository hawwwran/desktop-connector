"""T5.3 — Vault browser single-file download helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
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
    VaultLocalDiskFullError,
    download_folder,
    download_latest_file,
    resolve_folder_destination,
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

    def test_download_folder_recursively_materializes_current_tree(self) -> None:
        files = {
            f"batch/file-{index}.txt": f"payload {index}".encode("utf-8")
            for index in range(10)
        }
        manifest, chunks = _folder_manifest_and_chunks(files)
        relay = FakeChunkRelay(chunks)
        vault = _vault()
        progress = []
        try:
            out = download_folder(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path="Documents",
                destination=self.tmpdir / "Documents",
                progress=progress.append,
            )
        finally:
            vault.close()

        self.assertEqual(out, self.tmpdir / "Documents")
        for relative_path, expected in files.items():
            self.assertEqual((out / relative_path).read_bytes(), expected)
        self.assertEqual(len(relay.downloaded), 10)
        self.assertEqual(progress[-1].phase, "done")
        self.assertFalse(list(out.rglob("*.dc-temp-*")))

    def test_download_nested_folder_uses_nested_path_as_destination_root(self) -> None:
        manifest, chunks = _folder_manifest_and_chunks({
            "batch/a.txt": b"a",
            "batch/nested/b.txt": b"b",
            "outside.txt": b"outside",
        })
        relay = FakeChunkRelay(chunks)
        vault = _vault()
        try:
            out = download_folder(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path="Documents/batch",
                destination=self.tmpdir / "Batch",
            )
        finally:
            vault.close()

        self.assertEqual((out / "a.txt").read_bytes(), b"a")
        self.assertEqual((out / "nested" / "b.txt").read_bytes(), b"b")
        self.assertFalse((out / "outside.txt").exists())

    def test_download_folder_preflight_aborts_before_writes(self) -> None:
        manifest, chunks = _folder_manifest_and_chunks({"large.bin": b"x" * 1024})
        relay = FakeChunkRelay(chunks)
        vault = _vault()
        destination = self.tmpdir / "Documents"
        try:
            with mock.patch(
                "src.vault_download.shutil.disk_usage",
                return_value=SimpleNamespace(free=10),
            ):
                with self.assertRaises(VaultLocalDiskFullError):
                    download_folder(
                        vault=vault,
                        relay=relay,
                        manifest=manifest,
                        path="Documents",
                        destination=destination,
                    )
        finally:
            vault.close()

        self.assertFalse(destination.exists())
        self.assertEqual(relay.batch_head_calls, [])
        self.assertEqual(relay.downloaded, [])

    def test_download_folder_missing_chunk_fails_before_creating_destination(self) -> None:
        manifest, chunks = _folder_manifest_and_chunks({"a.txt": b"a"})
        relay = FakeChunkRelay({})
        vault = _vault()
        destination = self.tmpdir / "Documents"
        try:
            with self.assertRaises(VaultChunkMissingError):
                download_folder(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    path="Documents",
                    destination=destination,
                )
        finally:
            vault.close()

        self.assertFalse(destination.exists())
        self.assertEqual(relay.downloaded, [])

    def test_download_folder_rejects_unsafe_manifest_paths(self) -> None:
        manifest, chunks = _folder_manifest_and_chunks({"../evil.txt": b"bad"})
        relay = FakeChunkRelay(chunks)
        vault = _vault()
        try:
            with self.assertRaisesRegex(ValueError, "unsafe vault path"):
                download_folder(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    path="Documents",
                    destination=self.tmpdir / "Documents",
                )
        finally:
            vault.close()

        self.assertFalse((self.tmpdir / "evil.txt").exists())

    def test_keep_both_folder_policy_preserves_existing_destination(self) -> None:
        destination = self.tmpdir / "Documents"
        destination.mkdir()
        out = resolve_folder_destination(destination, "keep_both")

        self.assertEqual(out.name, "Documents (downloaded 1)")


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
    return _encrypt_chunk_for(plaintext, index, FILE_ID, VERSION_ID)


def _folder_manifest_and_chunks(files: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    chunks = {}
    entries = []
    alphabet = "abcdefghijklmnopqrstuvwx"
    for index, (relative_path, plaintext) in enumerate(files.items()):
        letter = alphabet[index % len(alphabet)]
        file_id = f"fe_v1_{letter * 24}"
        version_id = f"fv_v1_{letter * 24}"
        chunk_id = f"ch_v1_{letter * 24}"
        chunk_entries = []
        if plaintext:
            encrypted = _encrypt_chunk_for(plaintext, 0, file_id, version_id)
            chunks[chunk_id] = encrypted
            chunk_entries.append({
                "chunk_id": chunk_id,
                "index": 0,
                "plaintext_size": len(plaintext),
                "ciphertext_size": len(encrypted),
            })
        version = {
            "version_id": version_id,
            "created_at": "2026-05-04T12:00:00.000Z",
            "modified_at": "2026-05-04T11:59:00.000Z",
            "logical_size": len(plaintext),
            "ciphertext_size": sum(chunk["ciphertext_size"] for chunk in chunk_entries),
            "content_fingerprint": "unused",
            "chunks": chunk_entries,
            "author_device_id": AUTHOR,
        }
        entries.append({
            "entry_id": file_id,
            "type": "file",
            "path": relative_path,
            "latest_version_id": version_id,
            "deleted": False,
            "versions": [version],
        })

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
                entries=entries,
            )
        ],
    )
    return manifest, chunks


def _encrypt_chunk_for(plaintext: bytes, index: int, file_id: str, version_id: str) -> bytes:
    nonce = bytes([index + 1]) * 24
    subkey = derive_subkey("dc-vault-v1/chunk", MASTER_KEY)
    aad = build_chunk_aad(
        VAULT_ID,
        DOCS_ID,
        file_id,
        version_id,
        index,
        len(plaintext),
    )
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    return build_chunk_envelope(nonce=nonce, aead_ciphertext_and_tag=ciphertext)


if __name__ == "__main__":
    unittest.main()
