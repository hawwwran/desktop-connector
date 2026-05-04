"""T8.3 + T8.4 — Vault import preview, decision gate, and §D9 merge."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_import import (  # noqa: E402
    DEFAULT_CONFLICT_MODE,
    ImportMergeResolution,
    decide_import_action,
    find_conflict_batches,
    merge_import_into,
    preview_import,
)
from src.vault_manifest import (  # noqa: E402
    find_file_entry,
    make_manifest,
    make_remote_folder,
)


VAULT_ID = "ABCD2345WXYZ"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
PHOTOS_ID = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


# ---------------------------------------------------------------------------
# T8.3 — preview + decision gate
# ---------------------------------------------------------------------------


class VaultImportDecisionTests(unittest.TestCase):
    def test_no_active_vault_yields_new_vault_path(self) -> None:
        action = decide_import_action(
            active_manifest=None,
            active_genesis_fingerprint=None,
            bundle_vault_id=VAULT_ID,
            bundle_genesis_fingerprint="aabbccddeeff0011",
        )
        self.assertEqual(action, "new_vault")

    def test_matching_vault_id_and_fingerprint_yields_merge(self) -> None:
        active = _manifest_with_files(VAULT_ID, [(DOCS_ID, "Documents", [])])
        action = decide_import_action(
            active_manifest=active,
            active_genesis_fingerprint="aabbccddeeff0011",
            bundle_vault_id=VAULT_ID,
            bundle_genesis_fingerprint="aabbccddeeff0011",
        )
        self.assertEqual(action, "merge")

    def test_different_vault_id_refuses(self) -> None:
        active = _manifest_with_files(VAULT_ID, [(DOCS_ID, "Documents", [])])
        action = decide_import_action(
            active_manifest=active,
            active_genesis_fingerprint="aabbccddeeff0011",
            bundle_vault_id="DIFFERENTVAULT",
            bundle_genesis_fingerprint="ffffffffffffffff",
        )
        self.assertEqual(action, "refuse")

    def test_same_vault_id_but_fingerprint_mismatch_refuses(self) -> None:
        """§D9: vault_id alone isn't sufficient; cryptographic anchor must match."""
        active = _manifest_with_files(VAULT_ID, [(DOCS_ID, "Documents", [])])
        action = decide_import_action(
            active_manifest=active,
            active_genesis_fingerprint="aabbccddeeff0011",
            bundle_vault_id=VAULT_ID,
            bundle_genesis_fingerprint="00000000000000ff",
        )
        self.assertEqual(action, "refuse")


class VaultImportPreviewTests(unittest.TestCase):
    def test_preview_for_new_vault_path_reports_full_bundle(self) -> None:
        bundle = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("notes.txt", 1024, [("ch_v1_" + "a" * 24, 1024, 1056)]),
                ("invoice.pdf", 4096, [("ch_v1_" + "b" * 24, 4096, 4128)]),
            ]),
            (PHOTOS_ID, "Photos", [
                ("trip.jpg", 8192, [("ch_v1_" + "c" * 24, 8192, 8224)]),
            ]),
        ])

        preview = preview_import(
            bundle_manifest=bundle,
            bundle_vault_id=VAULT_ID,
            active_manifest=None,
            source_label="File: vault.dcvault",
            chunks_already_on_relay=0,
            bundle_genesis_fingerprint="aabbccddeeff0011000000000000aaaa",
        )

        self.assertEqual(preview.fingerprint_status, "new_vault")
        self.assertEqual(preview.bundle_genesis_fingerprint, "aabbccddeeff")
        self.assertEqual(preview.source_label, "File: vault.dcvault")
        self.assertEqual(preview.bundle_logical_size, 1024 + 4096 + 8192)
        self.assertEqual(preview.current_files, 3)
        self.assertEqual(preview.tombstones, 0)
        self.assertEqual(preview.total_versions, 3)
        self.assertEqual(preview.conflicts, 0)
        self.assertEqual(preview.chunks_total, 3)
        self.assertEqual(preview.chunks_already_on_relay, 0)
        self.assertTrue(preview.will_change_head)
        # Per-folder field math: each folder summary matches manifest.
        by_id = {f.remote_folder_id: f for f in preview.folders}
        self.assertEqual(by_id[DOCS_ID].current_file_count, 2)
        self.assertEqual(by_id[DOCS_ID].logical_size, 1024 + 4096)
        self.assertEqual(by_id[PHOTOS_ID].current_file_count, 1)

    def test_preview_for_merge_path_counts_conflicts_and_chunks_on_relay(self) -> None:
        active = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("shared.txt", 1024, [("ch_v1_" + "a" * 24, 1024, 1056)]),
            ]),
        ])
        bundle = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                # Conflict: same path, but a *different* version_id (set
                # implicitly by _file_entry below).
                ("shared.txt", 2048, [("ch_v1_" + "z" * 24, 2048, 2080)]),
                ("new.txt", 100, [("ch_v1_" + "y" * 24, 100, 132)]),
            ]),
        ])

        preview = preview_import(
            bundle_manifest=bundle,
            bundle_vault_id=VAULT_ID,
            active_manifest=active,
            source_label="File: vault.dcvault",
            chunks_already_on_relay=1,
            bundle_genesis_fingerprint="abc123def456789a",
            active_genesis_fingerprint="abc123def456789a",
        )

        self.assertEqual(preview.fingerprint_status, "matches_active")
        self.assertEqual(preview.conflicts, 1)
        self.assertEqual(preview.chunks_total, 2)
        self.assertEqual(preview.chunks_already_on_relay, 1)
        self.assertTrue(preview.will_change_head)


# ---------------------------------------------------------------------------
# T8.4 — §D9 merge
# ---------------------------------------------------------------------------


class VaultImportMergeTests(unittest.TestCase):
    def test_no_conflict_appends_new_files_as_new_entries(self) -> None:
        active = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [("a.txt", 10, [("ch_v1_" + "0" * 24, 10, 42)])]),
        ])
        bundle = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [("b.txt", 20, [("ch_v1_" + "1" * 24, 20, 52)])]),
        ])

        result = merge_import_into(
            active_manifest=active,
            bundle_manifest=bundle,
            resolution=ImportMergeResolution(per_folder={}),
            author_device_id=AUTHOR,
            now="2026-05-04T18:00:00.000Z",
        )

        self.assertEqual(result.new_paths, ["Documents/b.txt"])
        self.assertEqual(result.overwritten_paths, [])
        self.assertEqual(result.skipped_paths, [])
        self.assertEqual(result.renamed_paths, [])
        self.assertIsNotNone(find_file_entry(result.manifest, DOCS_ID, "a.txt"))
        self.assertIsNotNone(find_file_entry(result.manifest, DOCS_ID, "b.txt"))

    def test_two_folders_with_distinct_conflicts_each_resolve_independently(self) -> None:
        """§D9 + §A4 acceptance: per-folder conflict batches; each batch
        gets its own mode (or the 'apply to remaining' default fills in)."""
        active = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("clash.txt", 100, [("ch_v1_" + "a" * 24, 100, 132)]),
            ]),
            (PHOTOS_ID, "Photos", [
                ("clash.jpg", 200, [("ch_v1_" + "b" * 24, 200, 232)]),
            ]),
        ])
        bundle = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("clash.txt", 150, [("ch_v1_" + "c" * 24, 150, 182)]),
            ]),
            (PHOTOS_ID, "Photos", [
                ("clash.jpg", 250, [("ch_v1_" + "d" * 24, 250, 282)]),
            ]),
        ])

        batches = find_conflict_batches(
            active_manifest=active, bundle_manifest=bundle,
        )
        self.assertEqual(len(batches), 2)
        ids = {b.remote_folder_id for b in batches}
        self.assertEqual(ids, {DOCS_ID, PHOTOS_ID})

        # The user picks per-folder modes (no default-for-remaining).
        result = merge_import_into(
            active_manifest=active,
            bundle_manifest=bundle,
            resolution=ImportMergeResolution(per_folder={
                DOCS_ID: "overwrite",
                PHOTOS_ID: "skip",
            }),
            author_device_id=AUTHOR,
            now="2026-05-04T18:00:00.000Z",
        )
        self.assertEqual(result.overwritten_paths, ["Documents/clash.txt"])
        self.assertEqual(result.skipped_paths, ["Photos/clash.jpg"])

    def test_apply_to_remaining_default_covers_unspecified_folders(self) -> None:
        active = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("a.txt", 10, [("ch_v1_" + "0" * 24, 10, 42)]),
            ]),
            (PHOTOS_ID, "Photos", [
                ("b.jpg", 20, [("ch_v1_" + "1" * 24, 20, 52)]),
            ]),
        ])
        bundle = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("a.txt", 11, [("ch_v1_" + "2" * 24, 11, 43)]),
            ]),
            (PHOTOS_ID, "Photos", [
                ("b.jpg", 21, [("ch_v1_" + "3" * 24, 21, 53)]),
            ]),
        ])
        # User answered "skip" on Documents and ticked "apply to remaining"
        # → Photos gets skipped too without a second prompt.
        result = merge_import_into(
            active_manifest=active,
            bundle_manifest=bundle,
            resolution=ImportMergeResolution(
                per_folder={DOCS_ID: "skip"},
                default_for_remaining="skip",
            ),
            author_device_id=AUTHOR,
            now="2026-05-04T18:00:00.000Z",
        )
        self.assertEqual(
            result.skipped_paths,
            ["Documents/a.txt", "Photos/b.jpg"],
        )
        self.assertEqual(result.overwritten_paths, [])
        self.assertEqual(result.renamed_paths, [])

    def test_rename_mode_uses_a20_imported_naming(self) -> None:
        active = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("report.docx", 100, [("ch_v1_" + "0" * 24, 100, 132)]),
            ]),
        ])
        bundle = _manifest_with_files(VAULT_ID, [
            (DOCS_ID, "Documents", [
                ("report.docx", 110, [("ch_v1_" + "1" * 24, 110, 142)]),
            ]),
        ])

        result = merge_import_into(
            active_manifest=active,
            bundle_manifest=bundle,
            resolution=ImportMergeResolution(per_folder={DOCS_ID: "rename"}),
            author_device_id=AUTHOR,
            now="2026-05-04T18:00:00.000Z",
        )

        self.assertEqual(len(result.renamed_paths), 1)
        original, renamed = result.renamed_paths[0]
        self.assertEqual(original, "Documents/report.docx")
        self.assertEqual(
            renamed,
            "Documents/report (conflict imported 2026-05-04 18-00).docx",
        )
        # Both files exist in the merged manifest.
        self.assertIsNotNone(find_file_entry(result.manifest, DOCS_ID, "report.docx"))
        self.assertIsNotNone(find_file_entry(
            result.manifest, DOCS_ID,
            "report (conflict imported 2026-05-04 18-00).docx",
        ))

    def test_default_mode_is_rename(self) -> None:
        # If the resolution doesn't list the folder and has no
        # default_for_remaining, the per-folder fallback is rename per §D9.
        self.assertEqual(DEFAULT_CONFLICT_MODE, "rename")
        self.assertEqual(
            ImportMergeResolution(per_folder={}).resolve("rf_v1_zzz"),
            "rename",
        )


# ---------------------------------------------------------------------------
# Manifest-builder helpers (test-local, no encryption involved)
# ---------------------------------------------------------------------------


def _manifest_with_files(
    vault_id: str,
    folders_data: list[tuple[str, str, list[tuple[str, int, list[tuple[str, int, int]]]]]],
) -> dict:
    """Build a manifest plaintext with file entries.

    Each ``folders_data`` element: ``(folder_id, display_name, entries)``.
    Each entry: ``(path, logical_size, chunks)`` where each chunk is
    ``(chunk_id, plaintext_size, ciphertext_size)``.
    """
    folders = []
    counter = [0]

    def make_id(prefix: str) -> str:
        counter[0] += 1
        return prefix + "v1_" + (str(counter[0]).rjust(24, "0"))[:24]

    for folder_id, display_name, entries_data in folders_data:
        entries = []
        for path, logical_size, chunks in entries_data:
            version_id = make_id("fv_")
            chunk_records = [
                {
                    "chunk_id": chunk_id,
                    "index": idx,
                    "plaintext_size": pt_size,
                    "ciphertext_size": ct_size,
                }
                for idx, (chunk_id, pt_size, ct_size) in enumerate(chunks)
            ]
            entries.append({
                "entry_id": make_id("fe_"),
                "type": "file",
                "path": path,
                "deleted": False,
                "latest_version_id": version_id,
                "versions": [{
                    "version_id": version_id,
                    "created_at": "2026-05-01T10:00:00.000Z",
                    "modified_at": "2026-05-01T10:00:00.000Z",
                    "logical_size": logical_size,
                    "ciphertext_size": sum(c["ciphertext_size"] for c in chunk_records),
                    "content_fingerprint": "abc",
                    "chunks": chunk_records,
                    "author_device_id": AUTHOR,
                }],
            })
        folders.append(make_remote_folder(
            remote_folder_id=folder_id,
            display_name_enc=display_name,
            created_at="2026-05-01T10:00:00.000Z",
            created_by_device_id=AUTHOR,
            entries=entries,
        ))
    return make_manifest(
        vault_id=vault_id,
        revision=1,
        parent_revision=0,
        created_at="2026-05-01T10:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=folders,
    )


class VaultImportRunnerTests(unittest.TestCase):
    """T8.5 backbone: read bundle → preview → upload missing chunks → CAS publish."""

    def setUp(self) -> None:
        import tempfile, shutil
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_import_runner_"))
        self._saved_xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        import shutil
        if self._saved_xdg_cache_home is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg_cache_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_run_import_uploads_missing_chunks_and_publishes_merged_manifest(self) -> None:
        from src.vault import Vault
        from src.vault_crypto import DefaultVaultCrypto
        from src.vault_export import write_export_bundle
        from src.vault_import import ImportMergeResolution
        from src.vault_import_runner import run_import
        from src.vault_upload import upload_file
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR as MASTER_AUTHOR,
            DOCS_ID as MASTER_DOCS_ID,
            MASTER_KEY,
            VAULT_ID as MASTER_VAULT_ID,
        )
        from tests.protocol.test_desktop_vault_upload import FakeUploadRelay

        # Use the master-key-bound test vault id so encrypt/decrypt match.
        VAULT_ACCESS_SECRET = "vault-secret"

        def make_vault() -> Vault:
            return Vault(
                vault_id=MASTER_VAULT_ID, master_key=MASTER_KEY,
                recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
                header_revision=1, manifest_revision=1,
                manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
            )

        empty = make_manifest(
            vault_id=MASTER_VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-01T10:00:00.000Z",
            author_device_id=MASTER_AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=MASTER_DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-01T10:00:00.000Z",
                    created_by_device_id=MASTER_AUTHOR,
                    entries=[],
                )
            ],
        )

        # 1. Build a relay that already has one file. Export it.
        relay_a = FakeUploadRelay(manifest=empty)
        vault = make_vault()
        try:
            local_a = self.tmpdir / "exported.txt"
            local_a.write_bytes(b"exported content for the import flow")
            uploaded = upload_file(
                vault=vault, relay=relay_a, manifest=empty,
                local_path=local_a, remote_folder_id=MASTER_DOCS_ID,
                remote_path="exported.txt", author_device_id=MASTER_AUTHOR,
            )
            bundle_path = self.tmpdir / "vault.dcvault"
            write_export_bundle(
                vault=vault, relay=relay_a,
                manifest_envelope=relay_a.current_envelope,
                manifest_plaintext=uploaded.manifest,
                output_path=bundle_path,
                passphrase="user-export-passphrase",
                argon_memory_kib=8192,
                argon_iterations=2,
            )
        finally:
            vault.close()

        # 2. Fresh empty target relay simulates "import to a different relay".
        relay_b = FakeUploadRelay(manifest=empty)
        active_active = empty
        vault = make_vault()
        try:
            result = run_import(
                vault=vault, relay=relay_b,
                bundle_path=bundle_path,
                passphrase="user-export-passphrase",
                active_manifest=active_active,
                resolution=ImportMergeResolution(per_folder={}),
                author_device_id=MASTER_AUTHOR,
            )
        finally:
            vault.close()

        self.assertEqual(result.action, "merge")
        self.assertGreater(result.chunks_uploaded, 0)
        self.assertEqual(result.chunks_skipped, 0)
        # All chunks landed on relay B.
        self.assertEqual(set(relay_b.chunks), set(relay_a.chunks))
        # Manifest published exactly once (one CAS publish — no conflicts).
        self.assertEqual(len(relay_b.published_manifests), 1)
        self.assertIsNotNone(result.published_manifest)
        # Imported file visible in the published manifest.
        self.assertIsNotNone(
            find_file_entry(result.published_manifest, MASTER_DOCS_ID, "exported.txt")
        )

    def test_run_import_refuses_when_vault_identity_mismatches(self) -> None:
        from src.vault import Vault
        from src.vault_crypto import DefaultVaultCrypto
        from src.vault_export import write_export_bundle
        from src.vault_import import ImportMergeResolution
        from src.vault_import_runner import run_import
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR as MASTER_AUTHOR,
            DOCS_ID as MASTER_DOCS_ID,
            MASTER_KEY,
            VAULT_ID as MASTER_VAULT_ID,
        )
        from tests.protocol.test_desktop_vault_upload import FakeUploadRelay

        VAULT_ACCESS_SECRET = "vault-secret"
        empty = make_manifest(
            vault_id=MASTER_VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-01T10:00:00.000Z",
            author_device_id=MASTER_AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=MASTER_DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-01T10:00:00.000Z",
                    created_by_device_id=MASTER_AUTHOR,
                    entries=[],
                )
            ],
        )
        relay = FakeUploadRelay(manifest=empty)
        vault = Vault(
            vault_id=MASTER_VAULT_ID, master_key=MASTER_KEY,
            recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
            header_revision=1, manifest_revision=1,
            manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
        )
        try:
            bundle_path = self.tmpdir / "vault.dcvault"
            write_export_bundle(
                vault=vault, relay=relay,
                manifest_envelope=relay.current_envelope or b"\x00" * 200,
                manifest_plaintext=empty,
                output_path=bundle_path,
                passphrase="user-export-passphrase",
                argon_memory_kib=8192, argon_iterations=2,
            )
            # Active vault claims a different genesis fingerprint —
            # decide_import_action should return "refuse".
            result = run_import(
                vault=vault, relay=relay, bundle_path=bundle_path,
                passphrase="user-export-passphrase",
                active_manifest=empty,
                resolution=ImportMergeResolution(per_folder={}),
                author_device_id=MASTER_AUTHOR,
                bundle_genesis_fingerprint="aabbccddeeff0011",
                active_genesis_fingerprint="00000000ffffffff",
            )
        finally:
            vault.close()

        self.assertEqual(result.action, "refuse")
        self.assertIsNone(result.published_manifest)


if __name__ == "__main__":
    unittest.main()
