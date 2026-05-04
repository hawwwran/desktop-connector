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
from src.vault_relay_errors import VaultCASConflictError, VaultQuotaExceededError  # noqa: E402
from src.vault_upload import (  # noqa: E402
    FileSkipped,
    UploadConflictError,
    UploadSession,
    describe_quota_exceeded,
    detect_path_conflict,
    list_resumable_sessions,
    make_conflict_renamed_path,
    resume_upload,
    upload_file,
    upload_folder,
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

    def test_describe_quota_exceeded_offers_eviction_when_history_available(self) -> None:
        """T6.6 acceptance scenario A: full-with-history-available."""
        err = VaultQuotaExceededError({
            "code": "vault_quota_exceeded",
            "message": "out of room",
            "details": {
                "used_ciphertext_bytes": 950,
                "quota_ciphertext_bytes": 1000,
                "eviction_available": True,
            },
        })
        info = describe_quota_exceeded(err)

        self.assertTrue(info["eviction_available"])
        self.assertEqual(info["used_bytes"], 950)
        self.assertEqual(info["quota_bytes"], 1000)
        self.assertEqual(info["percent"], 95)
        self.assertIn("make space", info["heading"].lower())
        self.assertEqual(info["primary_action_label"], "Make space")

    def test_describe_quota_exceeded_terminal_when_no_history(self) -> None:
        """T6.6 acceptance scenario B: full-with-no-history-remaining."""
        err = VaultQuotaExceededError({
            "code": "vault_quota_exceeded",
            "message": "out of room",
            "details": {
                "used_ciphertext_bytes": 1000,
                "quota_ciphertext_bytes": 1000,
                "eviction_available": False,
            },
        })
        info = describe_quota_exceeded(err)

        self.assertFalse(info["eviction_available"])
        self.assertEqual(info["percent"], 100)
        self.assertIn("no backup history", info["heading"].lower())
        self.assertIn("export", info["body"].lower())
        self.assertIn("migrate", info["body"].lower())

    def test_upload_resume_after_simulated_crash_finishes_without_double_put(self) -> None:
        """T6.5 acceptance: kill mid-upload, restart, no chunk uploaded twice."""
        local = self.tmpdir / "resume.bin"
        local.write_bytes(b"resume-test " * 4096)  # 49152 bytes → 6 chunks @ 8 KiB

        manifest = _empty_manifest()
        relay = CrashingRelay(manifest=manifest, fail_after_n_puts=3)
        cache_dir = self.tmpdir / "resume_cache"
        vault = _vault()
        try:
            with self.assertRaises(SimulatedCrashError):
                upload_file(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    local_path=local,
                    remote_folder_id=DOCS_ID,
                    remote_path="resume.bin",
                    author_device_id=AUTHOR,
                    chunk_size=8 * 1024,
                    resume_cache_dir=cache_dir,
                )

            # State on disk must reflect the partial progress. The crash
            # fires *after* the 3rd PUT lands on the relay but *before*
            # ``upload_file`` flips that chunk's session flag — so the
            # session shows 2 done while the relay has 3 stored.
            sessions = list_resumable_sessions(VAULT_ID, cache_dir)
            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.phase, "uploading")
            done = sum(1 for c in session.chunks if c["done"])
            self.assertEqual(done, 2)
            self.assertEqual(len(relay.chunks), 3)
            self.assertEqual(len(relay.published_manifests), 0)

            # Resume on a fresh non-crashing relay that *retains* the chunks
            # already stored. Acceptance: only the missing chunks are PUT.
            puts_before_resume = list(relay.put_calls)
            relay.fail_after_n_puts = None
            result = resume_upload(
                vault=vault,
                relay=relay,
                manifest=manifest,
                session=session,
                resume_cache_dir=cache_dir,
            )
        finally:
            vault.close()

        new_puts = relay.put_calls[len(puts_before_resume):]
        self.assertEqual(len(new_puts), 3)  # exactly the 3 still-pending chunks
        self.assertEqual(len(relay.chunks), 6)
        self.assertEqual(len(relay.published_manifests), 1)
        self.assertEqual(result.chunks_uploaded, 3)
        self.assertEqual(result.chunks_skipped, 3)
        self.assertEqual(list_resumable_sessions(VAULT_ID, cache_dir), [])

    def test_upload_session_cleared_after_successful_publish(self) -> None:
        local = self.tmpdir / "tidy.txt"
        local.write_bytes(b"clean session contents")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        cache_dir = self.tmpdir / "resume_cache"
        vault = _vault()
        try:
            upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="tidy.txt",
                author_device_id=AUTHOR, resume_cache_dir=cache_dir,
            )
        finally:
            vault.close()

        # No leftover sessions for this vault.
        self.assertEqual(list_resumable_sessions(VAULT_ID, cache_dir), [])

    def test_upload_folder_walks_recursively_and_lands_one_publish(self) -> None:
        root = self.tmpdir / "src"
        (root / "docs").mkdir(parents=True)
        (root / "src" / "lib").mkdir(parents=True)
        (root / "src" / "main.py").write_bytes(b"print('hi')\n")
        (root / "src" / "lib" / "util.py").write_bytes(b"def util(): pass\n")
        (root / "docs" / "README.md").write_bytes(b"# Project\n")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        progress: list = []
        try:
            result = upload_folder(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_root=root,
                remote_folder_id=DOCS_ID,
                remote_sub_path="batch",
                author_device_id=AUTHOR,
                chunk_size=8 * 1024,
                progress=progress.append,
            )
        finally:
            vault.close()

        self.assertEqual(len(result.uploaded), 3)
        self.assertEqual(len(result.skipped), 0)
        # All three additions land in a single CAS-publish — atomic batch.
        self.assertEqual(len(relay.published_manifests), 1)
        self.assertEqual(relay.current_revision, 2)

        # Final manifest holds all three entries under the requested sub-path.
        for rel in ("batch/src/main.py", "batch/src/lib/util.py", "batch/docs/README.md"):
            entry = find_file_entry(result.manifest, DOCS_ID, rel)
            self.assertIsNotNone(entry, f"missing manifest entry for {rel}")

        self.assertEqual(progress[-1].phase, "done")
        self.assertEqual(progress[-1].files_completed, 3)

    def test_upload_folder_applies_default_ignore_patterns(self) -> None:
        root = self.tmpdir / "with_junk"
        (root / "src").mkdir(parents=True)
        (root / ".git").mkdir(parents=True)
        (root / "node_modules" / "leftpad").mkdir(parents=True)
        (root / "src" / "main.py").write_bytes(b"import sys\n")
        (root / "src" / "main.pyc").write_bytes(b"\x00\x01\x02bytecode")
        (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main")
        (root / "node_modules" / "leftpad" / "package.json").write_bytes(b"{}")

        # Use a remote folder with the §gaps §7 default ignore subset.
        ignore = [".git/", "node_modules/", "*.pyc"]
        manifest = make_manifest(
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
                    ignore_patterns=ignore,
                    entries=[],
                )
            ],
        )
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            result = upload_folder(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_root=root,
                remote_folder_id=DOCS_ID,
                remote_sub_path="",
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        uploaded_paths = sorted(r.path for r in result.uploaded)
        self.assertEqual(uploaded_paths, ["src/main.py"])

        skip_reasons = {(s.relative_path.rstrip("/"), s.reason) for s in result.skipped}
        self.assertIn((".git", "ignored"), skip_reasons)
        self.assertIn(("node_modules", "ignored"), skip_reasons)
        self.assertIn(("src/main.pyc", "ignored"), skip_reasons)
        # The walker prunes dir subtrees, so .git/HEAD doesn't appear.
        self.assertNotIn((".git/HEAD", "ignored"), skip_reasons)

    def test_upload_folder_size_cap_skips_oversize_with_logged_event(self) -> None:
        root = self.tmpdir / "big"
        root.mkdir()
        (root / "tiny.txt").write_bytes(b"OK")
        (root / "huge.bin").write_bytes(b"x" * 10 * 1024)

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            with self.assertLogs("src.vault_upload", level="INFO") as captured:
                result = upload_folder(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    local_root=root,
                    remote_folder_id=DOCS_ID,
                    remote_sub_path="",
                    author_device_id=AUTHOR,
                    max_file_bytes=4 * 1024,
                )
        finally:
            vault.close()

        self.assertEqual([r.path for r in result.uploaded], ["tiny.txt"])
        self.assertEqual([s.relative_path for s in result.skipped], ["huge.bin"])
        self.assertTrue(any(
            "vault.sync.file_skipped_too_large" in line for line in captured.output
        ))

    def test_upload_folder_skips_special_files(self) -> None:
        root = self.tmpdir / "linky"
        root.mkdir()
        (root / "real.txt").write_bytes(b"contents")
        (root / "alias.txt").symlink_to(root / "real.txt")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            with self.assertLogs("src.vault_upload", level="INFO") as captured:
                result = upload_folder(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    local_root=root,
                    remote_folder_id=DOCS_ID,
                    remote_sub_path="",
                    author_device_id=AUTHOR,
                )
        finally:
            vault.close()

        self.assertEqual([r.path for r in result.uploaded], ["real.txt"])
        self.assertIn(
            FileSkipped(relative_path="alias.txt", reason="special", size_bytes=0),
            result.skipped,
        )
        self.assertTrue(any(
            "vault.sync.special_file_skipped" in line for line in captured.output
        ))

    def test_upload_folder_idempotent_re_upload_zero_publishes(self) -> None:
        root = self.tmpdir / "stable"
        root.mkdir()
        (root / "alpha.txt").write_bytes(b"alpha contents")
        (root / "beta.txt").write_bytes(b"beta contents")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            first = upload_folder(
                vault=vault, relay=relay, manifest=manifest, local_root=root,
                remote_folder_id=DOCS_ID, remote_sub_path="batch",
                author_device_id=AUTHOR,
            )
            self.assertEqual(len(relay.published_manifests), 1)
            second = upload_folder(
                vault=vault, relay=relay, manifest=first.manifest, local_root=root,
                remote_folder_id=DOCS_ID, remote_sub_path="batch",
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        # Identical contents: zero new chunk PUTs and zero new manifest
        # publishes the second time around.
        self.assertEqual(len(relay.published_manifests), 1)
        self.assertEqual(second.bytes_uploaded, 0)
        self.assertTrue(all(r.skipped_identical for r in second.uploaded))

    def test_two_device_concurrent_upload_merges_via_cas_retry(self) -> None:
        """§D4 row 2 acceptance: both devices append a new version of an
        existing file; CAS retry merges so both versions land and the
        latest_version_id is deterministic."""
        device_seed = "0" * 32
        device_a = "a" * 32
        device_b = "b" * 32

        # Bootstrap: a seed device puts down v1 so both A and B see the
        # entry on first fetch.
        seed_local = self.tmpdir / "seed.txt"
        seed_local.write_bytes(b"seed payload, becomes version 1")
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        seed_vault = _vault()
        try:
            seed_res = upload_file(
                vault=seed_vault,
                relay=relay,
                manifest=manifest,
                local_path=seed_local,
                remote_folder_id=DOCS_ID,
                remote_path="report.txt",
                author_device_id=device_seed,
                created_at="2026-05-04T09:00:00.000Z",
            )
        finally:
            seed_vault.close()

        # Both A and B fetch the post-seed manifest as their "parent".
        shared_parent = seed_res.manifest

        local_a = self.tmpdir / "from_a.txt"
        local_a.write_bytes(b"alpha bytes one")
        local_b = self.tmpdir / "from_b.txt"
        local_b.write_bytes(
            b"beta bytes one - distinct content so no fingerprint match"
        )

        vault_a = _vault()
        vault_b = _vault()
        try:
            res_a = upload_file(
                vault=vault_a,
                relay=relay,
                manifest=shared_parent,
                local_path=local_a,
                remote_folder_id=DOCS_ID,
                remote_path="report.txt",
                author_device_id=device_a,
                created_at="2026-05-04T10:00:00.000Z",
            )
            self.assertEqual(relay.current_revision, 3)

            res_b = upload_file(
                vault=vault_b,
                relay=relay,
                manifest=shared_parent,  # pre-A view; CAS will fire
                local_path=local_b,
                remote_folder_id=DOCS_ID,
                remote_path="report.txt",
                author_device_id=device_b,
                # Later modified_at so B wins the tie-break deterministically.
                created_at="2026-05-04T11:00:00.000Z",
            )
        finally:
            vault_a.close()
            vault_b.close()

        # Three CAS-publishes total (seed + A direct + B after retry).
        self.assertEqual(relay.current_revision, 4)
        self.assertEqual(len(relay.published_manifests), 3)

        entry = find_file_entry(res_b.manifest, DOCS_ID, "report.txt")
        self.assertIsNotNone(entry)
        version_ids = {v["version_id"] for v in entry["versions"]}
        # All three versions live in F.versions.
        self.assertIn(seed_res.version_id, version_ids)
        self.assertIn(res_a.version_id, version_ids)
        self.assertIn(res_b.version_id, version_ids)
        # B's modified_at > A's > seed's → B wins.
        self.assertEqual(entry["latest_version_id"], res_b.version_id)

    def test_two_device_concurrent_upload_tie_break_by_device_hash(self) -> None:
        """When timestamps are equal, sha256(device_id) decides — same answer
        from either device's vantage point."""
        device_seed = "0" * 32
        device_a = "00" * 16
        device_b = "ff" * 16
        same_ts = "2026-05-04T12:00:00.000Z"

        seed_local = self.tmpdir / "seed.txt"
        seed_local.write_bytes(b"seed for tied path")
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        seed_vault = _vault()
        try:
            shared_parent = upload_file(
                vault=seed_vault,
                relay=relay,
                manifest=manifest,
                local_path=seed_local,
                remote_folder_id=DOCS_ID,
                remote_path="tied.txt",
                author_device_id=device_seed,
                created_at="2026-05-04T11:00:00.000Z",
            ).manifest
        finally:
            seed_vault.close()

        local_a = self.tmpdir / "a.txt"
        local_a.write_bytes(b"alpha unique payload alpha alpha")
        local_b = self.tmpdir / "b.txt"
        local_b.write_bytes(b"beta unique payload beta beta beta")

        vault_a = _vault()
        vault_b = _vault()
        try:
            res_a = upload_file(
                vault=vault_a, relay=relay, manifest=shared_parent, local_path=local_a,
                remote_folder_id=DOCS_ID, remote_path="tied.txt",
                author_device_id=device_a, created_at=same_ts,
            )
            res_b = upload_file(
                vault=vault_b, relay=relay, manifest=shared_parent, local_path=local_b,
                remote_folder_id=DOCS_ID, remote_path="tied.txt",
                author_device_id=device_b, created_at=same_ts,
            )
        finally:
            vault_a.close()
            vault_b.close()

        winner = max(
            (device_a, device_b),
            key=lambda d: hashlib.sha256(d.encode("utf-8")).digest(),
        )
        expected = res_a.version_id if winner == device_a else res_b.version_id
        entry = find_file_entry(res_b.manifest, DOCS_ID, "tied.txt")
        self.assertEqual(entry["latest_version_id"], expected)

    def test_concurrent_new_file_at_same_path_renames_imported(self) -> None:
        """§D4 row 1: two devices create different files at the same path."""
        local_a = self.tmpdir / "from_a.bin"
        local_a.write_bytes(b"alpha alpha alpha alpha alpha")
        local_b = self.tmpdir / "from_b.bin"
        local_b.write_bytes(b"beta beta beta beta beta beta beta")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault_a = _vault()
        vault_b = _vault()
        try:
            upload_file(
                vault=vault_a, relay=relay, manifest=manifest, local_path=local_a,
                remote_folder_id=DOCS_ID, remote_path="taken.bin",
                author_device_id="a" * 32,
            )
            # Different file_id because B started from the pre-A manifest
            # and didn't see A's entry — same path, fresh entry_id.
            upload_file(
                vault=vault_b, relay=relay, manifest=manifest, local_path=local_b,
                remote_folder_id=DOCS_ID, remote_path="taken.bin",
                author_device_id="b" * 32,
            )
        finally:
            vault_a.close()
            vault_b.close()

        # Walk the final manifest: original "taken.bin" (from A) and the
        # imported-renamed copy ("taken (imported).bin" from B) coexist.
        folder = next(
            f for f in relay.published_manifests[-1]["ciphertext"][:0] or []
            if False
        ) if False else None  # placeholder so the assertion below reads cleanly
        from src.vault_browser_model import decrypt_manifest as _decrypt
        published_envelope = relay.current_envelope
        vault_observer = _vault()
        try:
            head = _decrypt(vault_observer, published_envelope)
        finally:
            vault_observer.close()
        entry_paths = {
            e["path"]
            for f in head["remote_folders"]
            if f["remote_folder_id"] == DOCS_ID
            for e in f["entries"]
        }
        self.assertIn("taken.bin", entry_paths)
        self.assertIn("taken (imported).bin", entry_paths)

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


class SimulatedCrashError(RuntimeError):
    """Raised by ``CrashingRelay`` to model a process death mid-upload."""


class FakeUploadRelay:
    """In-memory fake of the chunk + manifest relay surface.

    Implements just enough CAS to drive the §D4 retry loop in tests:
    the latest-published envelope and revision are kept, and a stale
    ``expected_current_revision`` raises a ``VaultCASConflictError``
    with the freshly-published envelope embedded — same shape the real
    server returns per §A1.
    """

    def __init__(self, *, manifest: dict, quota_after_n_chunks: int | None = None) -> None:
        self.chunks: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.batch_head_calls: list[list[str]] = []
        self.published_manifests: list[dict] = []
        self.current_manifest = manifest
        self.current_revision = int(manifest.get("revision", 0))
        self.current_envelope: bytes = b""
        self.current_hash: str = ""
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

    # --- gc relay (T7.5) -----------------------------------------------
    def gc_plan(self, vault_id, vault_access_secret, *, manifest_revision, candidate_chunk_ids):
        plan_id = f"pl_{len(getattr(self, 'gc_plans', {}))}"
        if not hasattr(self, "gc_plans"):
            self.gc_plans = {}
        safe = [c for c in candidate_chunk_ids if c in self.chunks]
        self.gc_plans[plan_id] = {
            "manifest_revision": manifest_revision,
            "safe_to_delete": safe,
        }
        return {
            "plan_id": plan_id,
            "safe_to_delete": list(safe),
            "still_referenced": [],
            "expires_at": "2099-01-01T00:00:00.000Z",
        }

    def gc_execute(self, vault_id, vault_access_secret, *, plan_id, purge_secret=None):
        plan = getattr(self, "gc_plans", {}).get(plan_id)
        if plan is None:
            raise RuntimeError(f"unknown gc plan: {plan_id}")
        deleted = 0
        freed_bytes = 0
        for cid in plan["safe_to_delete"]:
            envelope = self.chunks.pop(cid, None)
            if envelope is not None:
                deleted += 1
                freed_bytes += len(envelope)
        return {
            "plan_id": plan_id,
            "deleted_count": deleted,
            "skipped_count": len(plan["safe_to_delete"]) - deleted,
            "freed_ciphertext_bytes": freed_bytes,
        }

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
            "manifest_revision": self.current_revision,
            "manifest_ciphertext": self.current_envelope,
            "manifest_hash": self.current_hash,
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
        if int(expected_current_revision) != self.current_revision:
            import base64

            raise VaultCASConflictError({
                "code": "vault_manifest_conflict",
                "message": "fake CAS conflict",
                "details": {
                    "current_revision": self.current_revision,
                    "current_manifest_hash": self.current_hash,
                    "current_manifest_ciphertext":
                        base64.b64encode(self.current_envelope).decode("ascii"),
                    "current_manifest_size": len(self.current_envelope),
                },
            })

        self.published_manifests.append({
            "expected_current_revision": expected_current_revision,
            "new_revision": new_revision,
            "parent_revision": parent_revision,
            "manifest_hash": manifest_hash,
            "ciphertext": bytes(manifest_ciphertext),
        })
        self.current_revision = int(new_revision)
        self.current_envelope = bytes(manifest_ciphertext)
        self.current_hash = manifest_hash
        return {"new_revision": new_revision}


class CrashingRelay(FakeUploadRelay):
    """Variant that raises ``SimulatedCrashError`` after N PUTs.

    Mirrors a process death right after the Nth chunk PUT — the data
    landed on the server but the client never got the chance to record
    "done" or move on to chunk N+1.
    """

    def __init__(self, *, manifest, fail_after_n_puts):
        super().__init__(manifest=manifest)
        self.fail_after_n_puts = fail_after_n_puts

    def put_chunk(self, vault_id, vault_access_secret, chunk_id, body):
        result = super().put_chunk(vault_id, vault_access_secret, chunk_id, body)
        if self.fail_after_n_puts is not None and len(self.put_calls) >= self.fail_after_n_puts:
            self.fail_after_n_puts = None  # only crash once
            raise SimulatedCrashError("simulated kill mid-upload")
        return result


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
