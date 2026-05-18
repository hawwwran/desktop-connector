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

from src.vault.import_.bundle import (  # noqa: E402
    DEFAULT_CONFLICT_MODE,
    ImportMergeResolution,
    decide_import_action,
    find_conflict_batches,
    merge_import_into,
    preview_import,
)
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
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
        from src.vault.crypto import DefaultVaultCrypto
        from src.vault.export.bundle import write_export_bundle
        from src.vault.import_.bundle import ImportMergeResolution
        from src.vault.import_.runner import run_import
        from src.vault.upload import upload_file
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR as MASTER_AUTHOR,
            DOCS_ID as MASTER_DOCS_ID,
            MASTER_KEY,
            VAULT_ID as MASTER_VAULT_ID,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay,
            seed_sharded_state,
        )

        # Use the master-key-bound test vault id so encrypt/decrypt match.
        VAULT_ACCESS_SECRET = "vault-secret"

        def make_vault() -> Vault:
            return Vault(
                vault_id=MASTER_VAULT_ID, master_key=MASTER_KEY,
                recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
                header_revision=1, manifest_revision=1,
                manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
            )

        def encrypt_manifest_envelope(manifest: dict) -> bytes:
            """Encrypt a unified manifest into the legacy envelope shape
            still embedded by the export bundle."""
            from src.vault.crypto import (
                aead_encrypt, build_manifest_aad,
                build_manifest_envelope, derive_subkey,
            )
            from src.vault.manifest import (
                canonical_manifest_json, normalize_manifest_plaintext,
            )
            import secrets as _secrets
            normalized = normalize_manifest_plaintext(manifest)
            plaintext = canonical_manifest_json(normalized)
            subkey = derive_subkey("dc-vault-v1/manifest", bytes(MASTER_KEY))
            nonce = _secrets.token_bytes(24)
            aad = build_manifest_aad(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
            )
            ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
            return build_manifest_envelope(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
                nonce=nonce,
                aead_ciphertext_and_tag=ciphertext,
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
        relay_a = FakeUploadRelay()
        vault = make_vault()
        try:
            seed_sharded_state(
                vault, relay_a,
                vault_id=empty['vault_id'],
                remote_folders=empty['remote_folders'],
                created_at=empty['created_at'],
                author_device_id=empty['author_device_id'],
            )
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
                manifest_envelope=encrypt_manifest_envelope(assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard})),
                manifest_plaintext=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                output_path=bundle_path,
                passphrase="user-export-passphrase",
                argon_memory_kib=8192,
                argon_iterations=2,
            )
        finally:
            vault.close()

        # 2. Fresh empty target relay simulates "import to a different relay".
        relay_b = FakeUploadRelay()
        active_active = empty
        vault = make_vault()
        try:
            # Phase H step 7c: ``run_import`` publishes via per-folder
            # ``publish_shard_with_root`` (sharded) — seed the sharded
            # state so the relay's root + shard surface is ready before
            # the merged-shard publish lands. Reset counters after the
            # seed so the assertion below counts only run_import's
            # publishes.
            seed_sharded_state(
                vault, relay_b,
                vault_id=empty['vault_id'],
                remote_folders=empty['remote_folders'],
                created_at=empty['created_at'],
                author_device_id=empty['author_device_id'],
            )
            relay_b.published_shards = []
            relay_b.published_roots = []
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
        # One shard published exactly once (one CAS publish — no
        # conflicts). The legacy ``published_manifests`` count stays
        # at the bootstrap publish from ``FakeUploadRelay.__init__``-
        # path seeding; ``published_shards`` is the post-step-7c
        # authoritative count.
        self.assertEqual(len(relay_b.published_shards), 1)
        self.assertIsNotNone(result.published_manifest)
        # Imported file visible in the published manifest.
        self.assertIsNotNone(
            find_file_entry(result.published_manifest, MASTER_DOCS_ID, "exported.txt")
        )

    def test_run_import_refuses_when_vault_identity_mismatches(self) -> None:
        from src.vault import Vault
        from src.vault.crypto import DefaultVaultCrypto
        from src.vault.export.bundle import write_export_bundle
        from src.vault.import_.bundle import ImportMergeResolution
        from src.vault.import_.runner import run_import
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR as MASTER_AUTHOR,
            DOCS_ID as MASTER_DOCS_ID,
            MASTER_KEY,
            VAULT_ID as MASTER_VAULT_ID,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay,
            seed_sharded_state,
        )

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
        relay = FakeUploadRelay()
        vault = Vault(
            vault_id=MASTER_VAULT_ID, master_key=MASTER_KEY,
            recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
            header_revision=1, manifest_revision=1,
            manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
        )
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=empty['vault_id'],
                remote_folders=empty['remote_folders'],
                created_at=empty['created_at'],
                author_device_id=empty['author_device_id'],
            )
            bundle_path = self.tmpdir / "vault.dcvault"
            write_export_bundle(
                vault=vault, relay=relay,
                manifest_envelope=b"\x00" * 200,
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

    def test_open_bundle_for_preview_counts_chunks_already_on_relay(self) -> None:
        """Review §5.H4: ``open_bundle_for_preview`` now takes a relay
        and calls ``batch_head_chunks`` so the preview's
        ``chunks_already_on_relay`` reflects reality BEFORE the user
        clicks Import. Pre-fix the wizard passed
        ``chunks_already_on_relay=0`` and the real head-count happened
        inside ``run_import`` after commit — bandwidth claims were
        fiction. This test exports a bundle then opens-for-preview
        against both a relay that already has every chunk (export
        relay) and a fresh empty relay; the count must match each.
        """
        from src.vault import Vault
        from src.vault.crypto import DefaultVaultCrypto
        from src.vault.export.bundle import write_export_bundle
        from src.vault.import_.runner import open_bundle_for_preview
        from src.vault.upload import upload_file
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR as MASTER_AUTHOR,
            DOCS_ID as MASTER_DOCS_ID,
            MASTER_KEY,
            VAULT_ID as MASTER_VAULT_ID,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay,
            seed_sharded_state,
        )

        VAULT_ACCESS_SECRET = "vault-secret"

        def make_vault() -> Vault:
            return Vault(
                vault_id=MASTER_VAULT_ID, master_key=MASTER_KEY,
                recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
                header_revision=1, manifest_revision=1,
                manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
            )

        def encrypt_manifest_envelope(manifest: dict) -> bytes:
            from src.vault.crypto import (
                aead_encrypt, build_manifest_aad,
                build_manifest_envelope, derive_subkey,
            )
            from src.vault.manifest import (
                canonical_manifest_json, normalize_manifest_plaintext,
            )
            import secrets as _secrets
            normalized = normalize_manifest_plaintext(manifest)
            plaintext = canonical_manifest_json(normalized)
            subkey = derive_subkey("dc-vault-v1/manifest", bytes(MASTER_KEY))
            nonce = _secrets.token_bytes(24)
            aad = build_manifest_aad(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
            )
            ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
            return build_manifest_envelope(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
                nonce=nonce,
                aead_ciphertext_and_tag=ciphertext,
            )

        empty = make_manifest(
            vault_id=MASTER_VAULT_ID, revision=1, parent_revision=0,
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

        relay_a = FakeUploadRelay()
        vault = make_vault()
        try:
            seed_sharded_state(
                vault, relay_a,
                vault_id=empty['vault_id'],
                remote_folders=empty['remote_folders'],
                created_at=empty['created_at'],
                author_device_id=empty['author_device_id'],
            )
            local_a = self.tmpdir / "exported.txt"
            local_a.write_bytes(b"exported bytes for the preview")
            uploaded = upload_file(
                vault=vault, relay=relay_a, manifest=empty,
                local_path=local_a, remote_folder_id=MASTER_DOCS_ID,
                remote_path="exported.txt", author_device_id=MASTER_AUTHOR,
            )
            bundle_path = self.tmpdir / "vault.dcvault"
            write_export_bundle(
                vault=vault, relay=relay_a,
                manifest_envelope=encrypt_manifest_envelope(assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard})),
                manifest_plaintext=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                output_path=bundle_path,
                passphrase="preview-passphrase",
                argon_memory_kib=8192, argon_iterations=2,
                genesis_fingerprint="ff" * 16,
            )
            chunk_ids_in_bundle = set()
            for folder in assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}).get("remote_folders", []) or []:
                for entry in folder.get("entries", []) or []:
                    for version in entry.get("versions", []) or []:
                        for chunk in version.get("chunks", []) or []:
                            chunk_ids_in_bundle.add(chunk["chunk_id"])
            self.assertGreater(len(chunk_ids_in_bundle), 0)
        finally:
            vault.close()

        # Preview against the export relay — every chunk should already
        # be there.
        vault = make_vault()
        try:
            _contents, _manifest, preview_same = open_bundle_for_preview(
                vault=vault,
                relay=relay_a,
                bundle_path=bundle_path,
                passphrase="preview-passphrase",
                active_manifest=empty,
                active_genesis_fingerprint="ff" * 16,
                bundle_genesis_fingerprint="ff" * 16,
            )
        finally:
            vault.close()
        self.assertEqual(
            preview_same.chunks_already_on_relay,
            len(chunk_ids_in_bundle),
        )

        # Preview against a fresh empty relay — count must be zero so
        # the user sees the real upload cost.
        relay_b = FakeUploadRelay()
        vault = make_vault()
        try:
            seed_sharded_state(
                vault, relay_b,
                vault_id=empty['vault_id'],
                remote_folders=empty['remote_folders'],
                created_at=empty['created_at'],
                author_device_id=empty['author_device_id'],
            )
            _contents, _manifest, preview_empty = open_bundle_for_preview(
                vault=vault,
                relay=relay_b,
                bundle_path=bundle_path,
                passphrase="preview-passphrase",
                active_manifest=empty,
                active_genesis_fingerprint="ff" * 16,
                bundle_genesis_fingerprint="ff" * 16,
            )
        finally:
            vault.close()
        self.assertEqual(preview_empty.chunks_already_on_relay, 0)

    def test_open_bundle_for_preview_does_not_write_to_relay(self) -> None:
        """Review §7.H3: a preview is a read-only operation. The
        wizard's Open Bundle → Preview round-trip must NEVER
        produce a put_chunk / publish_shard_with_root call against
        the relay; only the ``batch_head_chunks`` head-count is
        allowed. Pre-fix the existing preview tests verified shape
        but didn't pass a RecordingRelay so a future refactor that
        accidentally invoked an upload during preview wouldn't be
        caught here. Memory ``feedback_no_fake_tests`` applies."""
        from src.vault import Vault
        from src.vault.crypto import DefaultVaultCrypto
        from src.vault.export.bundle import write_export_bundle
        from src.vault.import_.runner import open_bundle_for_preview
        from src.vault.upload import upload_file
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR as MASTER_AUTHOR,
            DOCS_ID as MASTER_DOCS_ID,
            MASTER_KEY,
            VAULT_ID as MASTER_VAULT_ID,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay,
            seed_sharded_state,
        )

        VAULT_ACCESS_SECRET = "vault-secret"

        def make_vault() -> Vault:
            return Vault(
                vault_id=MASTER_VAULT_ID, master_key=MASTER_KEY,
                recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
                header_revision=1, manifest_revision=1,
                manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
            )

        def encrypt_manifest_envelope(manifest: dict) -> bytes:
            from src.vault.crypto import (
                aead_encrypt, build_manifest_aad,
                build_manifest_envelope, derive_subkey,
            )
            from src.vault.manifest import (
                canonical_manifest_json, normalize_manifest_plaintext,
            )
            import secrets as _secrets
            normalized = normalize_manifest_plaintext(manifest)
            plaintext = canonical_manifest_json(normalized)
            subkey = derive_subkey("dc-vault-v1/manifest", bytes(MASTER_KEY))
            nonce = _secrets.token_bytes(24)
            aad = build_manifest_aad(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
            )
            ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
            return build_manifest_envelope(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
                nonce=nonce,
                aead_ciphertext_and_tag=ciphertext,
            )

        empty = make_manifest(
            vault_id=MASTER_VAULT_ID, revision=1, parent_revision=0,
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

        export_relay = FakeUploadRelay()
        vault = make_vault()
        try:
            seed_sharded_state(
                vault, export_relay,
                vault_id=empty['vault_id'],
                remote_folders=empty['remote_folders'],
                created_at=empty['created_at'],
                author_device_id=empty['author_device_id'],
            )
            local = self.tmpdir / "exported.txt"
            local.write_bytes(b"some bytes")
            uploaded = upload_file(
                vault=vault, relay=export_relay, manifest=empty,
                local_path=local, remote_folder_id=MASTER_DOCS_ID,
                remote_path="exported.txt", author_device_id=MASTER_AUTHOR,
            )
            bundle_path = self.tmpdir / "vault.dcvault"
            write_export_bundle(
                vault=vault, relay=export_relay,
                manifest_envelope=encrypt_manifest_envelope(assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard})),
                manifest_plaintext=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                output_path=bundle_path,
                passphrase="bundle-passphrase",
                argon_memory_kib=8192, argon_iterations=2,
                genesis_fingerprint="aa" * 16,
            )
        finally:
            vault.close()

        # Fresh recording relay — empty. The preview must NOT mutate it.
        recording_relay = FakeUploadRelay()
        vault = make_vault()
        try:
            seed_sharded_state(
                vault, recording_relay,
                vault_id=empty['vault_id'],
                remote_folders=empty['remote_folders'],
                created_at=empty['created_at'],
                author_device_id=empty['author_device_id'],
            )
            # Reset counters after the seed so we only see preview activity.
            put_calls_before = list(recording_relay.put_calls)
            published_shards_before = list(recording_relay.published_shards)
            published_roots_before = list(recording_relay.published_roots)
            chunks_before = dict(recording_relay.chunks)
            shard_with_root_puts_before = recording_relay.shard_with_root_puts
            batch_heads_before = len(recording_relay.batch_head_calls)

            open_bundle_for_preview(
                vault=vault, relay=recording_relay,
                bundle_path=bundle_path,
                passphrase="bundle-passphrase",
                active_manifest=empty,
                active_genesis_fingerprint="aa" * 16,
                bundle_genesis_fingerprint="aa" * 16,
            )
        finally:
            vault.close()

        # The head call is the ONLY relay interaction allowed during preview.
        self.assertGreater(
            len(recording_relay.batch_head_calls), batch_heads_before,
            "preview must call batch_head_chunks to compute chunks_already_on_relay",
        )
        # Every write surface stays untouched.
        self.assertEqual(recording_relay.put_calls, put_calls_before,
                         "preview must not put_chunk")
        self.assertEqual(recording_relay.chunks, chunks_before,
                         "preview must not store chunk bytes")
        self.assertEqual(recording_relay.published_shards, published_shards_before,
                         "preview must not publish shards")
        self.assertEqual(recording_relay.published_roots, published_roots_before,
                         "preview must not publish roots")
        self.assertEqual(recording_relay.shard_with_root_puts,
                         shard_with_root_puts_before,
                         "preview must not call publish_shard_with_root")

    def test_run_import_creates_root_pointers_for_bundle_only_folders(self) -> None:
        """Phase H step 7c crash-fix: importing a bundle whose folder set
        is a superset of the active vault's must auto-create the missing
        root pointers before per-folder shard publish — otherwise
        ``fetch_folder_state`` raises ``ValueError`` partway through,
        leaving the import partially applied.
        """
        from src.vault import Vault
        from src.vault.crypto import DefaultVaultCrypto
        from src.vault.export.bundle import write_export_bundle
        from src.vault.import_.bundle import ImportMergeResolution
        from src.vault.import_.runner import run_import
        from src.vault.upload import upload_file
        from tests.protocol.test_desktop_vault_manifest import (
            AUTHOR as MASTER_AUTHOR,
            DOCS_ID as MASTER_DOCS_ID,
            MASTER_KEY,
            VAULT_ID as MASTER_VAULT_ID,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay,
            seed_sharded_state,
        )

        PICS_ID = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
        VAULT_ACCESS_SECRET = "vault-secret"

        def make_vault() -> Vault:
            return Vault(
                vault_id=MASTER_VAULT_ID, master_key=MASTER_KEY,
                recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
                header_revision=1, manifest_revision=1,
                manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
            )

        def encrypt_manifest_envelope(manifest: dict) -> bytes:
            from src.vault.crypto import (
                aead_encrypt, build_manifest_aad,
                build_manifest_envelope, derive_subkey,
            )
            from src.vault.manifest import (
                canonical_manifest_json, normalize_manifest_plaintext,
            )
            import secrets as _secrets
            normalized = normalize_manifest_plaintext(manifest)
            plaintext = canonical_manifest_json(normalized)
            subkey = derive_subkey("dc-vault-v1/manifest", bytes(MASTER_KEY))
            nonce = _secrets.token_bytes(24)
            aad = build_manifest_aad(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
            )
            ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
            return build_manifest_envelope(
                vault_id=str(normalized["vault_id"]),
                revision=int(normalized["revision"]),
                parent_revision=int(normalized["parent_revision"]),
                author_device_id=str(normalized["author_device_id"]),
                nonce=nonce,
                aead_ciphertext_and_tag=ciphertext,
            )

        manifest_a = make_manifest(
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
                ),
                make_remote_folder(
                    remote_folder_id=PICS_ID,
                    display_name_enc="Pictures",
                    created_at="2026-05-01T10:00:00.000Z",
                    created_by_device_id=MASTER_AUTHOR,
                    entries=[],
                ),
            ],
        )
        # Vault A: two folders, uploads one file into the PICS_ID folder
        # that doesn't yet exist in vault B's manifest.
        relay_a = FakeUploadRelay()
        vault = make_vault()
        try:
            seed_sharded_state(
                vault, relay_a,
                vault_id=manifest_a['vault_id'],
                remote_folders=manifest_a['remote_folders'],
                created_at=manifest_a['created_at'],
                author_device_id=manifest_a['author_device_id'],
            )
            local_pic = self.tmpdir / "exported.png"
            local_pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 100)
            uploaded = upload_file(
                vault=vault, relay=relay_a, manifest=manifest_a,
                local_path=local_pic, remote_folder_id=PICS_ID,
                remote_path="exported.png", author_device_id=MASTER_AUTHOR,
            )
            bundle_path = self.tmpdir / "vault.dcvault"
            write_export_bundle(
                vault=vault, relay=relay_a,
                manifest_envelope=encrypt_manifest_envelope(assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard})),
                manifest_plaintext=assemble_unified_manifest(uploaded.root, {uploaded.remote_folder_id: uploaded.shard}),
                output_path=bundle_path,
                passphrase="user-export-passphrase",
                argon_memory_kib=8192, argon_iterations=2,
            )
        finally:
            vault.close()

        # Vault B: starts with only DOCS_ID — PICS_ID is bundle-only.
        manifest_b_initial = make_manifest(
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
                ),
            ],
        )
        relay_b = FakeUploadRelay()
        vault = make_vault()
        try:
            seed_sharded_state(
                vault, relay_b,
                vault_id=manifest_b_initial['vault_id'],
                remote_folders=manifest_b_initial['remote_folders'],
                created_at=manifest_b_initial['created_at'],
                author_device_id=manifest_b_initial['author_device_id'],
            )
            relay_b.published_shards = []
            relay_b.published_roots = []
            result = run_import(
                vault=vault, relay=relay_b,
                bundle_path=bundle_path,
                passphrase="user-export-passphrase",
                active_manifest=manifest_b_initial,
                resolution=ImportMergeResolution(per_folder={}),
                author_device_id=MASTER_AUTHOR,
            )
        finally:
            vault.close()

        self.assertEqual(result.action, "merge")
        # Pre-flight published a root with the new PICS_ID pointer; the
        # per-folder shard publish then landed the imported file.
        self.assertGreaterEqual(len(relay_b.published_roots), 1)
        self.assertEqual(len(relay_b.published_shards), 1)
        self.assertEqual(relay_b.published_shards[0]["remote_folder_id"], PICS_ID)
        # Final manifest has both folder pointers and the imported file.
        self.assertIsNotNone(result.published_manifest)
        folder_ids = {
            f["remote_folder_id"]
            for f in result.published_manifest.get("remote_folders", [])
        }
        self.assertIn(MASTER_DOCS_ID, folder_ids)
        self.assertIn(PICS_ID, folder_ids)
        self.assertIsNotNone(
            find_file_entry(result.published_manifest, PICS_ID, "exported.png")
        )


class VaultImportRunnerVaultIdAssertTests(unittest.TestCase):
    """F-C14 polish — defensive layering on top of the wrap-AAD vault_id
    binding. ``read_export_bundle`` already binds the AEAD-decryption key
    to the caller-supplied vault_id, so a bundle whose internal
    ``header.vault_id`` disagrees can only show up if a future regression
    weakens that binding. The helper catches that case explicitly with
    ``vault_export_vault_mismatch`` instead of letting the merge step
    proceed with a foreign manifest.
    """

    def test_passes_when_vault_ids_match_canonically(self) -> None:
        from src.vault.import_.runner import _assert_bundle_vault_id_matches
        _assert_bundle_vault_id_matches(
            bundle_vault_id="ABCD2345WXYZ",
            expected_vault_id="ABCD2345WXYZ",
        )  # no raise

    def test_passes_when_only_dashing_differs(self) -> None:
        from src.vault.import_.runner import _assert_bundle_vault_id_matches
        # normalize_vault_id strips dashes + uppercases.
        _assert_bundle_vault_id_matches(
            bundle_vault_id="ABCD-2345-WXYZ",
            expected_vault_id="abcd2345wxyz",
        )  # no raise

    def test_raises_export_error_when_vault_ids_disagree(self) -> None:
        from src.vault.import_.runner import _assert_bundle_vault_id_matches
        from src.vault.export.bundle import ExportError
        with self.assertRaises(ExportError) as ctx:
            _assert_bundle_vault_id_matches(
                bundle_vault_id="AAAA2345WXYZ",
                expected_vault_id="ABCD2345WXYZ",
            )
        self.assertEqual(ctx.exception.code, "vault_export_vault_mismatch")


if __name__ == "__main__":
    unittest.main()
