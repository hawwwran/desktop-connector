"""T7 — Vault soft-delete, restore, and tombstone retention helpers."""

from __future__ import annotations

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
from src.vault_browser_model import list_folder, list_versions  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_delete import (  # noqa: E402
    delete_file,
    delete_folder_contents,
    restore_version_to_current,
)
from src.vault_manifest import (  # noqa: E402
    compute_recoverable_until,
    find_file_entry,
    make_manifest,
    make_remote_folder,
    restore_file_entry,
    tombstone_file_entry,
    tombstone_files_under,
)
from src.vault_upload import upload_file

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import (  # noqa: E402
    FakeUploadRelay,
)


VAULT_ACCESS_SECRET = "vault-secret"


class VaultManifestTombstoneTests(unittest.TestCase):
    def test_tombstone_file_entry_marks_deleted_and_keeps_versions(self) -> None:
        manifest = _seeded_manifest([
            ("Invoices/2026/report.pdf", "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa"),
            ("Photos/IMG_001.jpg", "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb"),
        ])

        out = tombstone_file_entry(
            manifest,
            remote_folder_id=DOCS_ID,
            path="Invoices/2026/report.pdf",
            deleted_at="2026-05-04T18:00:00.000Z",
            author_device_id=AUTHOR,
        )

        entry = find_file_entry(out, DOCS_ID, "Invoices/2026/report.pdf")
        self.assertTrue(entry["deleted"])
        self.assertEqual(entry["deleted_at"], "2026-05-04T18:00:00.000Z")
        self.assertEqual(entry["deleted_by_device_id"], AUTHOR)
        # Chunk references retained for restore (T7.4).
        self.assertEqual(len(entry["versions"]), 1)
        self.assertEqual(entry["latest_version_id"], "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa")
        # Other entries untouched.
        peer = find_file_entry(out, DOCS_ID, "Photos/IMG_001.jpg")
        self.assertFalse(peer["deleted"])

    def test_tombstone_file_entry_missing_raises(self) -> None:
        manifest = _seeded_manifest([("a.txt", "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa")])
        with self.assertRaises(KeyError):
            tombstone_file_entry(
                manifest, remote_folder_id=DOCS_ID, path="missing.txt",
                deleted_at="2026-05-04T18:00:00.000Z", author_device_id=AUTHOR,
            )

    def test_tombstone_files_under_bulk_only_touches_subtree(self) -> None:
        manifest = _seeded_manifest([
            ("Invoices/2026/a.pdf", "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa"),
            ("Invoices/2026/b.pdf", "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb"),
            ("Invoices/2025/old.pdf", "fv_v1_cccccccccccccccccccccccc"),
            ("Photos/wedding.jpg", "fv_v1_dddddddddddddddddddddddd"),
        ])

        out, tombstoned = tombstone_files_under(
            manifest,
            remote_folder_id=DOCS_ID,
            path_prefix="Invoices/2026",
            deleted_at="2026-05-04T18:00:00.000Z",
            author_device_id=AUTHOR,
        )

        self.assertCountEqual(
            tombstoned,
            ["Invoices/2026/a.pdf", "Invoices/2026/b.pdf"],
        )
        for path in tombstoned:
            self.assertTrue(find_file_entry(out, DOCS_ID, path)["deleted"])
        for survivor in ("Invoices/2025/old.pdf", "Photos/wedding.jpg"):
            self.assertFalse(find_file_entry(out, DOCS_ID, survivor)["deleted"])

    def test_tombstone_files_under_root_drops_every_live_entry(self) -> None:
        manifest = _seeded_manifest([
            ("a.txt", "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa"),
            ("nested/b.txt", "fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb"),
        ])
        out, tombstoned = tombstone_files_under(
            manifest, remote_folder_id=DOCS_ID, path_prefix="",
            deleted_at="2026-05-04T18:00:00.000Z", author_device_id=AUTHOR,
        )
        self.assertCountEqual(tombstoned, ["a.txt", "nested/b.txt"])

    def test_restore_file_entry_clears_tombstone_and_promotes_version(self) -> None:
        manifest = _seeded_manifest([("a.txt", "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa")])
        manifest = tombstone_file_entry(
            manifest, remote_folder_id=DOCS_ID, path="a.txt",
            deleted_at="2026-05-04T18:00:00.000Z", author_device_id=AUTHOR,
        )

        new_version = {
            "version_id": "fv_v1_zzzzzzzzzzzzzzzzzzzzzzzz",
            "created_at": "2026-05-05T10:00:00.000Z",
            "modified_at": "2026-05-05T10:00:00.000Z",
            "logical_size": 32,
            "ciphertext_size": 56,
            "content_fingerprint": "deadbeef",
            "author_device_id": AUTHOR,
            "chunks": [{
                "chunk_id": "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                "index": 0,
                "plaintext_size": 32,
                "ciphertext_size": 56,
            }],
        }
        out = restore_file_entry(
            manifest, remote_folder_id=DOCS_ID, path="a.txt",
            new_version=new_version, author_device_id=AUTHOR,
        )

        entry = find_file_entry(out, DOCS_ID, "a.txt")
        self.assertFalse(entry["deleted"])
        self.assertNotIn("deleted_at", entry)
        self.assertEqual(entry["latest_version_id"], "fv_v1_zzzzzzzzzzzzzzzzzzzzzzzz")
        self.assertEqual(len(entry["versions"]), 2)
        self.assertEqual(entry["restored_by_device_id"], AUTHOR)


class VaultRetentionDisplayTests(unittest.TestCase):
    """T7.6 — display-only retention math; server clock is still authoritative."""

    def test_compute_recoverable_until_adds_keep_days(self) -> None:
        out = compute_recoverable_until("2026-05-04T18:00:00.000Z", 30)
        self.assertEqual(out, "2026-06-03T18:00:00.000Z")

    def test_compute_recoverable_until_zero_days(self) -> None:
        out = compute_recoverable_until("2026-05-04T18:00:00.000Z", 0)
        self.assertEqual(out, "2026-05-04T18:00:00.000Z")

    def test_compute_recoverable_until_handles_offset_timezone(self) -> None:
        out = compute_recoverable_until("2026-05-04T20:00:00+02:00", 30)
        self.assertEqual(out, "2026-06-03T18:00:00.000Z")

    def test_compute_recoverable_until_unparseable_returns_blank(self) -> None:
        self.assertEqual(compute_recoverable_until("", 30), "")
        self.assertEqual(compute_recoverable_until("not a date", 30), "")

    def test_tombstone_stamps_recoverable_until_using_folder_retention(self) -> None:
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=2,
            parent_revision=1,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    retention_policy={"keep_deleted_days": 14, "keep_versions": 10},
                    entries=[{
                        "entry_id": "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                        "type": "file",
                        "path": "doomed.txt",
                        "deleted": False,
                        "latest_version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                        "versions": [{
                            "version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                            "created_at": "2026-05-01T10:00:00.000Z",
                            "modified_at": "2026-05-01T10:00:00.000Z",
                            "logical_size": 1,
                            "ciphertext_size": 25,
                            "content_fingerprint": "x",
                            "chunks": [],
                            "author_device_id": AUTHOR,
                        }],
                    }],
                )
            ],
        )

        out = tombstone_file_entry(
            manifest, remote_folder_id=DOCS_ID, path="doomed.txt",
            deleted_at="2026-05-04T18:00:00.000Z", author_device_id=AUTHOR,
        )
        entry = find_file_entry(out, DOCS_ID, "doomed.txt")
        self.assertEqual(entry["recoverable_until"], "2026-05-18T18:00:00.000Z")


class VaultDeleteOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_delete_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_delete_file_publishes_one_revision_and_hides_in_browser(self) -> None:
        local = self.tmpdir / "doomed.txt"
        local.write_bytes(b"payload that will be soft-deleted")
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="doomed.txt",
                author_device_id=AUTHOR,
            )
            published = delete_file(
                vault=vault, relay=relay, manifest=uploaded.manifest,
                remote_folder_id=DOCS_ID, remote_path="doomed.txt",
                author_device_id=AUTHOR,
                deleted_at="2026-05-04T18:00:00.000Z",
            )
        finally:
            vault.close()

        self.assertEqual(relay.current_revision, 3)  # bootstrap upload + delete
        entry = find_file_entry(published, DOCS_ID, "doomed.txt")
        self.assertTrue(entry["deleted"])
        # Chunks remain on the relay — soft delete only touches the manifest.
        self.assertGreater(len(relay.chunks), 0)
        # Browser model hides deleted by default (T7.1 / T5.2 contract).
        _folders, files = list_folder(published, "Documents")
        self.assertEqual([f["name"] for f in files], [])
        # …but include_deleted=True surfaces them with the tombstone fields.
        _folders, files = list_folder(published, "Documents", include_deleted=True)
        self.assertEqual([f["name"] for f in files], ["doomed.txt"])
        self.assertEqual(files[0]["status"], "Deleted")

    def test_delete_folder_contents_bulk_tombstones(self) -> None:
        relay = FakeUploadRelay(manifest=_empty_manifest())
        vault = _vault()
        try:
            for sub in ("Invoices/2026/a.pdf", "Invoices/2026/b.pdf", "Photos/p.jpg"):
                local = self.tmpdir / sub.replace("/", "_")
                local.write_bytes(f"content for {sub}".encode("utf-8"))
                upload_file(
                    vault=vault, relay=relay,
                    manifest=_decrypt_current_manifest(vault, relay),
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path=sub, author_device_id=AUTHOR,
                )
            head = _decrypt_current_manifest(vault, relay)
            published, tombstoned = delete_folder_contents(
                vault=vault, relay=relay, manifest=head,
                remote_folder_id=DOCS_ID, path_prefix="Invoices/2026",
                author_device_id=AUTHOR,
                deleted_at="2026-05-04T18:00:00.000Z",
            )
        finally:
            vault.close()

        self.assertCountEqual(
            tombstoned,
            ["Invoices/2026/a.pdf", "Invoices/2026/b.pdf"],
        )
        for path in tombstoned:
            self.assertTrue(find_file_entry(published, DOCS_ID, path)["deleted"])
        # Photo survived.
        self.assertFalse(
            find_file_entry(published, DOCS_ID, "Photos/p.jpg")["deleted"]
        )

    def test_restore_version_promotes_chosen_version_without_uploading(self) -> None:
        local = self.tmpdir / "report.txt"
        local.write_bytes(b"version 1 content")
        relay = FakeUploadRelay(manifest=_empty_manifest())
        vault = _vault()
        try:
            v1 = upload_file(
                vault=vault, relay=relay, manifest=_empty_manifest(),
                local_path=local, remote_folder_id=DOCS_ID,
                remote_path="report.txt", author_device_id=AUTHOR,
                created_at="2026-05-01T10:00:00.000Z",
            )
            local.write_bytes(b"version 2 content - different bytes")
            v2 = upload_file(
                vault=vault, relay=relay, manifest=v1.manifest,
                local_path=local, remote_folder_id=DOCS_ID,
                remote_path="report.txt", author_device_id=AUTHOR,
                created_at="2026-05-02T10:00:00.000Z",
            )
            puts_before = list(relay.put_calls)
            restored = restore_version_to_current(
                vault=vault, relay=relay, manifest=v2.manifest,
                remote_folder_id=DOCS_ID, remote_path="report.txt",
                source_version_id=v1.version_id,
                author_device_id=AUTHOR,
                created_at="2026-05-03T10:00:00.000Z",
            )
        finally:
            vault.close()

        # No new chunks were PUT for the restore.
        self.assertEqual(relay.put_calls, puts_before)

        entry = find_file_entry(restored, DOCS_ID, "report.txt")
        self.assertEqual(len(entry["versions"]), 3)  # v1 + v2 + restored
        self.assertNotEqual(entry["latest_version_id"], v1.version_id)
        self.assertNotEqual(entry["latest_version_id"], v2.version_id)
        latest = next(
            v for v in entry["versions"]
            if v["version_id"] == entry["latest_version_id"]
        )
        # The restored version references v1's chunk_ids.
        v1_chunk_ids = {
            c["chunk_id"]
            for ver in entry["versions"]
            if ver["version_id"] == v1.version_id
            for c in ver["chunks"]
        }
        restored_chunk_ids = {c["chunk_id"] for c in latest["chunks"]}
        self.assertEqual(restored_chunk_ids, v1_chunk_ids)
        self.assertEqual(latest["restored_from_version_id"], v1.version_id)

    def test_restore_tombstoned_file_clears_deleted(self) -> None:
        local = self.tmpdir / "ghost.txt"
        local.write_bytes(b"will be tombstoned then restored")
        relay = FakeUploadRelay(manifest=_empty_manifest())
        vault = _vault()
        try:
            uploaded = upload_file(
                vault=vault, relay=relay, manifest=_empty_manifest(),
                local_path=local, remote_folder_id=DOCS_ID,
                remote_path="ghost.txt", author_device_id=AUTHOR,
            )
            after_delete = delete_file(
                vault=vault, relay=relay, manifest=uploaded.manifest,
                remote_folder_id=DOCS_ID, remote_path="ghost.txt",
                author_device_id=AUTHOR,
            )
            self.assertTrue(
                find_file_entry(after_delete, DOCS_ID, "ghost.txt")["deleted"]
            )
            after_restore = restore_version_to_current(
                vault=vault, relay=relay, manifest=after_delete,
                remote_folder_id=DOCS_ID, remote_path="ghost.txt",
                source_version_id=uploaded.version_id,
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        entry = find_file_entry(after_restore, DOCS_ID, "ghost.txt")
        self.assertFalse(entry["deleted"])
        self.assertNotIn("deleted_at", entry)


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


def _seeded_manifest(files: list[tuple[str, str]]) -> dict:
    """Build a manifest with N files, each having a single dummy version."""
    entries = []
    for path, version_id in files:
        entries.append({
            "entry_id": "fe_v1_" + path.replace("/", "_").ljust(24, "x")[:24],
            "type": "file",
            "path": path,
            "deleted": False,
            "latest_version_id": version_id,
            "versions": [{
                "version_id": version_id,
                "created_at": "2026-05-01T10:00:00.000Z",
                "modified_at": "2026-05-01T10:00:00.000Z",
                "logical_size": 100,
                "ciphertext_size": 124,
                "content_fingerprint": "abc",
                "chunks": [],
                "author_device_id": AUTHOR,
            }],
        })
    return make_manifest(
        vault_id=VAULT_ID,
        revision=2,
        parent_revision=1,
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


def _decrypt_current_manifest(vault, relay) -> dict:
    """Decrypt whatever envelope the fake relay last accepted."""
    from src.vault_browser_model import decrypt_manifest as _decrypt
    if not relay.current_envelope:
        from src.vault_manifest import normalize_manifest_plaintext
        return normalize_manifest_plaintext(relay.current_manifest)
    return _decrypt(vault, relay.current_envelope)


if __name__ == "__main__":
    unittest.main()
