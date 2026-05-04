"""T6.1 — Vault browser single-file upload helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_download import download_latest_file  # noqa: E402
from src.vault_manifest import find_file_entry, make_manifest, make_remote_folder  # noqa: E402
from src.vault_relay_errors import VaultQuotaExceededError  # noqa: E402
from src.vault_upload import (  # noqa: E402
    UploadConflictError,
    detect_path_conflict,
    make_conflict_renamed_path,
    upload_file,
)

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)


VAULT_ACCESS_SECRET = "vault-secret"


class VaultUploadRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_upload_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_upload_then_download_roundtrips_bytes(self) -> None:
        payload = (b"hello vault " * 4096) + b"!! end"
        local = self.tmpdir / "report.txt"
        local.write_bytes(payload)

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            result = upload_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path="report.txt",
                author_device_id=AUTHOR,
                chunk_size=8 * 1024,
            )
            new_manifest = result.manifest
            entry = find_file_entry(new_manifest, DOCS_ID, "report.txt")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["latest_version_id"], result.version_id)

            destination = self.tmpdir / "downloaded.txt"
            download_latest_file(
                vault=vault,
                relay=relay,
                manifest=new_manifest,
                path="Documents/report.txt",
                destination=destination,
            )
        finally:
            vault.close()

        self.assertEqual(destination.read_bytes(), payload)
        latest_chunks = _latest_chunk_list(result.manifest, DOCS_ID, "report.txt")
        self.assertEqual(result.chunks_uploaded, len(latest_chunks))
        self.assertEqual(result.chunks_skipped, 0)

    def test_uploading_same_file_twice_uploads_zero_new_chunks(self) -> None:
        local = self.tmpdir / "twice.bin"
        local.write_bytes(b"deterministic content " * 1024)

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            first = upload_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path="twice.bin",
                author_device_id=AUTHOR,
                chunk_size=4 * 1024,
            )
            self.assertGreater(first.chunks_uploaded, 0)
            self.assertEqual(first.chunks_skipped, 0)
            chunks_after_first = dict(relay.chunks)

            second = upload_file(
                vault=vault,
                relay=relay,
                manifest=first.manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path="twice.bin",
                author_device_id=AUTHOR,
                chunk_size=4 * 1024,
            )
        finally:
            vault.close()

        self.assertEqual(second.chunks_uploaded, 0)
        self.assertEqual(second.chunks_skipped, first.chunks_uploaded)
        self.assertTrue(second.skipped_identical)
        self.assertEqual(second.version_id, first.version_id)
        # No PUTs after the first upload finished: identical content
        # short-circuited at the file-fingerprint level, before chunk
        # encrypt/PUT and before any manifest mutation.
        self.assertEqual(relay.chunks, chunks_after_first)
        self.assertEqual(len(relay.published_manifests), 1)
        entry = find_file_entry(second.manifest, DOCS_ID, "twice.bin")
        self.assertEqual(len(entry["versions"]), 1)
        self.assertEqual(entry["latest_version_id"], first.version_id)

    def test_quota_exceeded_mid_upload_raises_typed_error(self) -> None:
        local = self.tmpdir / "big.bin"
        local.write_bytes(b"q" * (16 * 1024))

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest, quota_after_n_chunks=1)
        vault = _vault()
        try:
            with self.assertRaises(VaultQuotaExceededError) as ctx:
                upload_file(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    local_path=local,
                    remote_folder_id=DOCS_ID,
                    remote_path="big.bin",
                    author_device_id=AUTHOR,
                    chunk_size=4 * 1024,
                )
        finally:
            vault.close()

        self.assertEqual(ctx.exception.status_code, 507)
        # Manifest must NOT have been published when an upload aborted.
        self.assertEqual(len(relay.published_manifests), 0)
        # Chunks accepted before the 507 are kept (idempotent retries are
        # supposed to skip them) but no new file entry exists yet.
        self.assertGreaterEqual(len(relay.chunks), 1)

    def test_keep_both_rename_path_uploads_as_independent_entry(self) -> None:
        local = self.tmpdir / "report.docx"
        local.write_bytes(b"first content")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            first = upload_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path="report.docx",
                author_device_id=AUTHOR,
            )
            # Different bytes for the second upload so the fingerprint
            # short-circuit does not fire.
            local.write_bytes(b"second content, totally different")
            renamed_path = make_conflict_renamed_path(
                "report.docx",
                "Laptop",
                now=datetime(2026, 5, 4, 17, 30, tzinfo=timezone.utc),
            )
            second = upload_file(
                vault=vault,
                relay=relay,
                manifest=first.manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path=renamed_path,
                author_device_id=AUTHOR,
                mode="new_file_only",
            )
        finally:
            vault.close()

        self.assertEqual(
            renamed_path,
            "report (conflict uploaded Laptop 2026-05-04 17-30).docx",
        )
        self.assertNotEqual(first.entry_id, second.entry_id)
        original = find_file_entry(second.manifest, DOCS_ID, "report.docx")
        renamed = find_file_entry(second.manifest, DOCS_ID, renamed_path)
        self.assertIsNotNone(original)
        self.assertIsNotNone(renamed)
        self.assertEqual(original["entry_id"], first.entry_id)
        self.assertEqual(renamed["entry_id"], second.entry_id)

    def test_detect_path_conflict_skips_tombstoned_entries(self) -> None:
        manifest = _empty_manifest()
        # Inject a tombstoned entry directly so we don't depend on T7.1 yet.
        manifest["remote_folders"][0]["entries"].append({
            "entry_id": "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
            "type": "file",
            "path": "ghost.txt",
            "deleted": True,
            "latest_version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
            "versions": [{
                "version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                "created_at": "2026-04-01T10:00:00.000Z",
                "modified_at": "2026-04-01T10:00:00.000Z",
                "logical_size": 1,
                "ciphertext_size": 25,
                "content_fingerprint": "deadbeef",
                "chunks": [],
                "author_device_id": AUTHOR,
            }],
        })

        self.assertFalse(detect_path_conflict(manifest, DOCS_ID, "ghost.txt"))
        self.assertFalse(detect_path_conflict(manifest, DOCS_ID, "absent.txt"))

    def test_detect_path_conflict_finds_live_entry(self) -> None:
        local = self.tmpdir / "live.txt"
        local.write_bytes(b"live")
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            first = upload_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path="live.txt",
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        self.assertTrue(detect_path_conflict(first.manifest, DOCS_ID, "live.txt"))

    def test_make_conflict_renamed_path_handles_directory_and_recursion(self) -> None:
        first = make_conflict_renamed_path(
            "Invoices/2026/report.pdf",
            "Workstation 7",
            now=datetime(2026, 5, 4, 17, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            first,
            "Invoices/2026/report (conflict uploaded Workstation 7 2026-05-04 17-30).pdf",
        )
        # A20 example: chained conflicts append a second suffix instead of
        # double-renaming.
        second = make_conflict_renamed_path(
            first,
            "Laptop",
            now=datetime(2026, 5, 4, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            second,
            "Invoices/2026/report (conflict uploaded Workstation 7 2026-05-04 17-30) "
            "(conflict uploaded Laptop 2026-05-04 18-00).pdf",
        )

    def test_make_conflict_renamed_path_extensionless_file(self) -> None:
        out = make_conflict_renamed_path(
            "README",
            "Laptop",
            now=datetime(2026, 5, 4, 17, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(out, "README (conflict uploaded Laptop 2026-05-04 17-30)")

    def test_make_conflict_renamed_path_sanitizes_device_name(self) -> None:
        out = make_conflict_renamed_path(
            "report.docx",
            "Bad/Name:With*Chars",
            now=datetime(2026, 5, 4, 17, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            out,
            "report (conflict uploaded Bad_Name_With_Chars 2026-05-04 17-30).docx",
        )

    def test_new_file_only_refuses_existing_path(self) -> None:
        local = self.tmpdir / "first.txt"
        local.write_bytes(b"first")
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            first = upload_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path="first.txt",
                author_device_id=AUTHOR,
            )
            with self.assertRaises(UploadConflictError):
                upload_file(
                    vault=vault,
                    relay=relay,
                    manifest=first.manifest,
                    local_path=local,
                    remote_folder_id=DOCS_ID,
                    remote_path="first.txt",
                    author_device_id=AUTHOR,
                    mode="new_file_only",
                )
        finally:
            vault.close()


class FakeUploadRelay:
    """In-memory fake of the chunk + manifest relay surface."""

    def __init__(self, *, manifest: dict, quota_after_n_chunks: int | None = None) -> None:
        self.chunks: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.batch_head_calls: list[list[str]] = []
        self.published_manifests: list[dict] = []
        self.current_manifest = manifest
        self._quota_remaining = quota_after_n_chunks

    # --- chunk relay ---------------------------------------------------
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
        return self.chunks[chunk_id]

    def put_chunk(self, vault_id, vault_access_secret, chunk_id, body):
        self.put_calls.append(chunk_id)
        if chunk_id in self.chunks:
            return {"created": False, "stored_size": len(body)}
        if self._quota_remaining is not None:
            if self._quota_remaining <= 0:
                raise VaultQuotaExceededError({
                    "code": "vault_quota_exceeded",
                    "message": "fake quota exceeded",
                    "details": {
                        "used_ciphertext_bytes": sum(len(v) for v in self.chunks.values()),
                        "quota_ciphertext_bytes": sum(len(v) for v in self.chunks.values())
                            + len(body) - 1,
                        "eviction_available": False,
                    },
                })
            self._quota_remaining -= 1
        self.chunks[chunk_id] = bytes(body)
        return {"created": True, "stored_size": len(body)}

    # --- manifest relay ------------------------------------------------
    def get_manifest(self, vault_id, vault_access_secret):
        return {
            "manifest_revision": int(self.current_manifest["revision"]),
            "manifest_ciphertext": b"",
            "manifest_hash": "",
        }

    def put_manifest(
        self,
        vault_id,
        vault_access_secret,
        *,
        expected_current_revision,
        new_revision,
        parent_revision,
        manifest_hash,
        manifest_ciphertext,
    ):
        # No CAS check in the unit-test fake: T6.3 tests that flow.
        self.published_manifests.append({
            "expected_current_revision": expected_current_revision,
            "new_revision": new_revision,
            "parent_revision": parent_revision,
            "manifest_hash": manifest_hash,
            "ciphertext": manifest_ciphertext,
        })
        return {"new_revision": new_revision}


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID,
        master_key=MASTER_KEY,
        recovery_secret=None,
        vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1,
        manifest_revision=1,
        manifest_ciphertext=b"",
        crypto=DefaultVaultCrypto,
    )


def _empty_manifest() -> dict:
    return make_manifest(
        vault_id=VAULT_ID,
        revision=1,
        parent_revision=0,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            )
        ],
    )


def _latest_chunk_list(manifest: dict, remote_folder_id: str, path: str) -> list[dict]:
    entry = find_file_entry(manifest, remote_folder_id, path)
    if entry is None:
        return []
    latest = next(
        (v for v in entry["versions"] if v["version_id"] == entry["latest_version_id"]),
        None,
    )
    return latest["chunks"] if latest else []


if __name__ == "__main__":
    unittest.main()
