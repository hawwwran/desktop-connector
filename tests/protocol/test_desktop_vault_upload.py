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
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.download import download_latest_file  # noqa: E402
from src.vault.manifest import find_file_entry, make_manifest, make_remote_folder  # noqa: E402
from src.vault.relay_errors import VaultCASConflictError, VaultQuotaExceededError  # noqa: E402
from src.vault.upload import (  # noqa: E402
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
        # Redirect XDG_CACHE_HOME so default_upload_resume_dir() lands
        # inside the per-test tmpdir — otherwise the upload_file()
        # session-state writes leak into the real
        # ~/.cache/desktop-connector/vault/uploads/ dir on every run.
        self._saved_xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg_cache_home is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg_cache_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_upload_file_result_manifest_surfaces_new_entry_in_browser_view(self) -> None:
        """Boundary guard for the "upload but file doesn't show" failure mode.

        Asserts the contract the vault browser leans on:
        ``result.manifest`` returned by ``upload_file`` actually carries
        the new file entry — both via direct lookup and via the
        ``list_folder`` walk the file list calls. If this drifts, the
        browser will silently fail to render new uploads no matter how
        clean ``state["manifest"] = result.manifest`` is.
        """
        from src.vault.ui.browser_model import list_folder

        local = self.tmpdir / "guarded.txt"
        local.write_bytes(b"manifest-must-reflect-this-upload")
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            result = upload_file(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_path=local,
                remote_folder_id=DOCS_ID,
                remote_path="guarded.txt",
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        # 1. Direct entry lookup hits the new file with the right version_id.
        entry = find_file_entry(result.manifest, DOCS_ID, "guarded.txt")
        self.assertIsNotNone(entry, "result.manifest is missing the uploaded entry")
        self.assertEqual(entry["latest_version_id"], result.version_id)

        # 2. result.manifest is NOT the input dict (caller can swap it in
        # without worrying about shared state).
        self.assertIsNot(result.manifest, manifest)
        # The pre-upload manifest must not be mutated under the caller's feet.
        self.assertIsNone(find_file_entry(manifest, DOCS_ID, "guarded.txt"))

        # 3. list_folder — the call the browser's render_file_list runs —
        # surfaces the file row at the requested folder display name.
        _folders, files = list_folder(result.manifest, "Documents")
        names = [str(f.get("name")) for f in files]
        self.assertIn("guarded.txt", names)

    def test_upload_folder_result_manifest_surfaces_every_uploaded_file(self) -> None:
        """Same guard for ``upload_folder`` — every walked file must land
        in the published manifest under the configured sub-path."""
        from src.vault.ui.browser_model import list_folder

        root = self.tmpdir / "tree"
        (root / "sub").mkdir(parents=True)
        (root / "top.txt").write_bytes(b"top content")
        (root / "sub" / "leaf.txt").write_bytes(b"leaf content")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            from src.vault.upload import upload_folder

            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            result = upload_folder(
                vault=vault,
                relay=relay,
                manifest=manifest,
                local_root=root,
                remote_folder_id=DOCS_ID,
                remote_sub_path="batch",
                author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        for rel in ("batch/top.txt", "batch/sub/leaf.txt"):
            self.assertIsNotNone(
                find_file_entry(result.manifest, DOCS_ID, rel),
                f"folder upload published manifest is missing {rel}",
            )
        self.assertIsNot(result.manifest, manifest)

        # Browser-shaped walks at both depths see the new files.
        _f, top_files = list_folder(result.manifest, "Documents/batch")
        self.assertIn("top.txt", [str(f.get("name")) for f in top_files])
        _f, leaf_files = list_folder(result.manifest, "Documents/batch/sub")
        self.assertIn("leaf.txt", [str(f.get("name")) for f in leaf_files])

    def test_upload_then_download_roundtrips_bytes(self) -> None:
        payload = (b"hello vault " * 4096) + b"!! end"
        local = self.tmpdir / "report.txt"
        local.write_bytes(payload)

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            mirror_legacy_from_sharded(vault, relay)
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            mirror_legacy_from_sharded(vault, relay)
        finally:
            vault.close()

        new_puts = relay.put_calls[len(puts_before_resume):]
        self.assertEqual(len(new_puts), 3)  # exactly the 3 still-pending chunks
        self.assertEqual(len(relay.chunks), 6)
        self.assertEqual(len(relay.published_manifests), 1)
        self.assertEqual(result.chunks_uploaded, 3)
        self.assertEqual(result.chunks_skipped, 3)
        self.assertEqual(list_resumable_sessions(VAULT_ID, cache_dir), [])

    def test_resume_seeks_past_already_done_chunks_instead_of_reading(self) -> None:
        """F-D12: when both the session AND the relay agree a chunk is
        already PUT, ``resume_upload`` advances the file pointer with
        ``seek`` rather than reading + re-deriving the chunk_id.
        Resuming the last 50 MB of a 2 GiB file no longer burns
        1.95 GiB of disk reads.

        We count bytes read from the local file during resume — the
        count must equal the bytes of *only the chunks the relay
        reports as missing* (the ones we actually need to PUT).
        Chunks already on the relay get seeked, even when the local
        session's ``done`` flag is stale.
        """
        local = self.tmpdir / "big.bin"
        # 12 chunks @ 8 KiB = 96 KiB so the math is easy.
        local.write_bytes(b"X" * (12 * 8 * 1024))

        manifest = _empty_manifest()
        # fail_after_n_puts=4 means 4 PUTs land on the relay; the 5th
        # raises SimulatedCrashError. The session's ``done`` flag is
        # written *after* a successful PUT, so on the crash window
        # there's typically a 1-chunk gap between relay state and
        # session state — the F-D12 mixed-signal branch is exercised
        # naturally here without needing a contrived fixture.
        relay = CrashingRelay(manifest=manifest, fail_after_n_puts=4)
        cache_dir = self.tmpdir / "resume_cache"
        vault = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            with self.assertRaises(SimulatedCrashError):
                upload_file(
                    vault=vault, relay=relay, manifest=manifest,
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path="big.bin", author_device_id=AUTHOR,
                    chunk_size=8 * 1024,
                    resume_cache_dir=cache_dir,
                )

            sessions = list_resumable_sessions(VAULT_ID, cache_dir)
            session = sessions[0]
            relay.fail_after_n_puts = None
            # Whatever is on the relay now will be seeked, not read.
            chunks_on_relay_before_resume = len(relay.chunks)
            chunks_to_read_during_resume = (
                len(session.chunks) - chunks_on_relay_before_resume
            )

            real_open = open
            bytes_read_total = {"n": 0}

            class _CountingFile:
                def __init__(self, fh):
                    self._fh = fh
                def read(self, n):
                    out = self._fh.read(n)
                    bytes_read_total["n"] += len(out)
                    return out
                def seek(self, offset, whence=0):
                    return self._fh.seek(offset, whence)
                def __enter__(self):
                    return self
                def __exit__(self, exc_type, exc, tb):
                    self._fh.__exit__(exc_type, exc, tb)
                def __getattr__(self, name):
                    return getattr(self._fh, name)

            def counting_open(path, mode="r", *args, **kwargs):
                fh = real_open(path, mode, *args, **kwargs)
                if str(path) == str(local):
                    return _CountingFile(fh)
                return fh

            # Patch ``open`` in the module that owns the function reading
            # the file (``resume``); patching the shim's namespace would be
            # silently ineffective post-split.
            with mock.patch("src.vault.upload.resume.open", counting_open):
                resume_upload(
                    vault=vault, relay=relay, manifest=manifest,
                    session=session, resume_cache_dir=cache_dir,
                )
        finally:
            vault.close()

        # Bytes read must equal exactly the chunks the relay didn't
        # already have — no read for chunks the seek-branch handled.
        self.assertEqual(
            bytes_read_total["n"],
            chunks_to_read_during_resume * 8 * 1024,
            "F-D12: resume must seek past chunks already on the "
            "relay, not read + re-derive them",
        )
        self.assertGreater(
            chunks_on_relay_before_resume, 0,
            "test setup must leave at least one chunk on the relay so "
            "F-D12's seek-branch is exercised",
        )

    def test_upload_session_cleared_after_successful_publish(self) -> None:
        local = self.tmpdir / "tidy.txt"
        local.write_bytes(b"clean session contents")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        cache_dir = self.tmpdir / "resume_cache"
        vault = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            mirror_legacy_from_sharded(vault, relay)
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

    def test_unsupported_ignore_pattern_logs_once_per_process(self) -> None:
        """F-D14: ``**`` and rooted ``/foo`` patterns aren't supported by
        the v1 fnmatch matcher — they silently never match. The matcher
        now warns once per process per pattern so the user has a
        breadcrumb explaining why their rule didn't match.
        """
        from src.vault.upload import (
            _UNSUPPORTED_PATTERN_WARNED, _matches_ignore,
        )
        # Reset the per-process dedup so this test is deterministic.
        _UNSUPPORTED_PATTERN_WARNED.clear()
        with self.assertLogs("src.vault.upload", level="WARNING") as captured:
            # Two passes with the same unsupported pattern: only the
            # first should log.
            _matches_ignore(
                "main.py", "src/main.py", patterns=["**/*.tmp"], is_dir=False,
            )
            _matches_ignore(
                "other.py", "src/other.py", patterns=["**/*.tmp"], is_dir=False,
            )
            # A different unsupported pattern: logs once more.
            _matches_ignore(
                "any.txt", "any.txt", patterns=["/rooted"], is_dir=False,
            )
        warnings = [
            line for line in captured.output
            if "ignore_pattern_unsupported_shape" in line
        ]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("**/*.tmp" in line for line in warnings))
        self.assertTrue(any("/rooted" in line for line in warnings))

    def test_supported_ignore_patterns_do_not_warn(self) -> None:
        """F-D14 negative: known-good patterns must not trip the
        unsupported-shape warning.
        """
        import logging
        from src.vault.upload import (
            _UNSUPPORTED_PATTERN_WARNED, _matches_ignore,
        )
        _UNSUPPORTED_PATTERN_WARNED.clear()
        # ``assertLogs`` requires at least one record; emit one of our
        # own to satisfy that contract.
        with self.assertLogs("src.vault.upload", level="WARNING") as captured:
            _matches_ignore(
                "main.py", "src/main.py", patterns=["*.pyc", ".git/", "node_modules/"],
                is_dir=False,
            )
            logging.getLogger("src.vault.upload").warning(
                "test_anchor_to_satisfy_assertLogs"
            )
        warnings = [
            line for line in captured.output
            if "ignore_pattern_unsupported_shape" in line
        ]
        self.assertEqual(warnings, [])

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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            with self.assertLogs("src.vault.upload", level="INFO") as captured:
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            with self.assertLogs("src.vault.upload", level="INFO") as captured:
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

    def test_upload_folder_lstat_error_classifies_as_error_not_special(self) -> None:
        """F-D13 — a permission-denied / dangling-symlink / transient I/O
        path that fails ``lstat`` should be classified as ``"error"``
        with the captured ``errno``, NOT silently rebranded as ``"special"``.
        Otherwise the user sees "special" in the UI for files that were
        meant to upload but couldn't be read.
        """
        from unittest import mock as _mock
        import errno as errno_mod
        import src.vault.upload as vault_upload_mod

        root = self.tmpdir / "perms"
        root.mkdir()
        readable = root / "readable.txt"
        readable.write_bytes(b"OK")
        denied = root / "no-permission.txt"
        denied.write_bytes(b"x")
        original_lstat = Path.lstat

        def _denied_lstat(self_inner):
            if str(self_inner) == str(denied):
                raise PermissionError(errno_mod.EACCES, "permission denied", str(denied))
            return original_lstat(self_inner)

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            with _mock.patch.object(Path, "lstat", _denied_lstat), \
                 self.assertLogs("src.vault.upload", level="INFO") as captured:
                result = upload_folder(
                    vault=vault, relay=relay, manifest=manifest,
                    local_root=root, remote_folder_id=DOCS_ID,
                    remote_sub_path="", author_device_id=AUTHOR,
                )
        finally:
            vault.close()

        self.assertEqual([r.path for r in result.uploaded], ["readable.txt"])
        skipped = [s for s in result.skipped if s.relative_path == "no-permission.txt"]
        self.assertEqual(len(skipped), 1, result.skipped)
        self.assertEqual(skipped[0].reason, "error")
        self.assertEqual(skipped[0].errno, errno_mod.EACCES)
        # Walk-error gets a WARNING-level log line, not the INFO-level
        # "special_file_skipped" event.
        self.assertTrue(any(
            "vault.sync.file_walk_error" in line and "no-permission.txt" in line
            and f"errno={errno_mod.EACCES}" in line
            for line in captured.output
        ), captured.output)
        self.assertFalse(any(
            "vault.sync.special_file_skipped" in line and "no-permission.txt" in line
            for line in captured.output
        ), "F-D13 regression: lstat-failed path classified as special")

    def test_upload_folder_idempotent_re_upload_zero_publishes(self) -> None:
        root = self.tmpdir / "stable"
        root.mkdir()
        (root / "alpha.txt").write_bytes(b"alpha contents")
        (root / "beta.txt").write_bytes(b"beta contents")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            first = upload_folder(
                vault=vault, relay=relay, manifest=manifest, local_root=root,
                remote_folder_id=DOCS_ID, remote_sub_path="batch",
                author_device_id=AUTHOR,
            )
            mirror_legacy_from_sharded(vault, relay)
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            seed_vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(seed_vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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
            mirror_legacy_from_sharded(seed_vault, relay)
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
            mirror_legacy_from_sharded(vault_a, relay)
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
            mirror_legacy_from_sharded(vault_b, relay)
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            seed_vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(seed_vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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

        # Phase H step 4: simulate B's pre-A view by snapshotting the
        # sharded state before A publishes — pass via ``parent_state``
        # so B's publish hits CAS, decrypts A's server head, and runs
        # the §D4 merge tie-break.
        from src.vault.upload.folder_state import fetch_folder_state
        vault_a = _vault()
        vault_b = _vault()
        try:
            pre_a_state_b = fetch_folder_state(vault_b, relay, DOCS_ID, device_b)
            res_a = upload_file(
                vault=vault_a, relay=relay, manifest=shared_parent, local_path=local_a,
                remote_folder_id=DOCS_ID, remote_path="tied.txt",
                author_device_id=device_a, created_at=same_ts,
            )
            res_b = upload_file(
                vault=vault_b, relay=relay, manifest=shared_parent, local_path=local_b,
                remote_folder_id=DOCS_ID, remote_path="tied.txt",
                author_device_id=device_b, created_at=same_ts,
                parent_state=pre_a_state_b,
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault_a.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault_a, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            # Phase H step 4: snapshot B's pre-A view before A publishes
            # so B's upload sees no existing "taken.bin" entry, generates
            # a fresh entry_id, hits CAS on publish, and the §D4 merge
            # then runs the path-collision rename branch.
            from src.vault.upload.folder_state import fetch_folder_state
            pre_a_state_b = fetch_folder_state(vault_b, relay, DOCS_ID, "b" * 32)
            upload_file(
                vault=vault_a, relay=relay, manifest=manifest, local_path=local_a,
                remote_folder_id=DOCS_ID, remote_path="taken.bin",
                author_device_id="a" * 32,
            )
            mirror_legacy_from_sharded(vault_a, relay)
            # Different file_id because B started from the pre-A manifest
            # and didn't see A's entry — same path, fresh entry_id.
            upload_file(
                vault=vault_b, relay=relay, manifest=manifest, local_path=local_b,
                remote_folder_id=DOCS_ID, remote_path="taken.bin",
                author_device_id="b" * 32,
                parent_state=pre_a_state_b,
            )
            mirror_legacy_from_sharded(vault_b, relay)
        finally:
            vault_a.close()
            vault_b.close()

        # Walk the final manifest: original "taken.bin" (from A) and the
        # imported-renamed copy ("taken (imported).bin" from B) coexist.
        folder = next(
            f for f in relay.published_manifests[-1]["ciphertext"][:0] or []
            if False
        ) if False else None  # placeholder so the assertion below reads cleanly
        from src.vault.ui.browser_model import decrypt_manifest as _decrypt
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
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault, relay, manifest)
            # Reset publish counters so tests can count only post-init publishes.
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
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

    def test_two_device_concurrent_folder_upload_tie_break_by_device_hash(self) -> None:
        """§D4 tie-break must run on the folder-batch CAS-retry path.

        Two devices upload folders containing the same path under the
        same entry_id; concurrent publishes race; the loser merges via
        the new ``_merge_additions_into_shard_with_bump`` helper which
        re-resolves ``latest_version_id`` via
        ``(modified_at, sha256(author_device_id))`` per §D4.
        """
        device_seed = "0" * 32
        device_a = "00" * 16
        device_b = "ff" * 16
        same_ts = "2026-05-04T12:00:00.000Z"

        # Seed: one file already at "shared/tied.txt" so both devices
        # see the same parent entry_id E0 when they walk their folder.
        seed_dir = self.tmpdir / "seed_folder"
        (seed_dir / "shared").mkdir(parents=True)
        (seed_dir / "shared" / "tied.txt").write_bytes(b"seed for tied path")
        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        seed_vault = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            seed_vault.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(seed_vault, relay, manifest)
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            shared_parent = upload_folder(
                vault=seed_vault,
                relay=relay,
                manifest=manifest,
                local_root=seed_dir,
                remote_folder_id=DOCS_ID,
                remote_sub_path="",
                author_device_id=device_seed,
                created_at="2026-05-04T11:00:00.000Z",
            ).manifest
        finally:
            seed_vault.close()

        # Each device has its own folder with same path, different content.
        dir_a = self.tmpdir / "from_a"
        (dir_a / "shared").mkdir(parents=True)
        (dir_a / "shared" / "tied.txt").write_bytes(
            b"alpha unique payload alpha alpha"
        )
        dir_b = self.tmpdir / "from_b"
        (dir_b / "shared").mkdir(parents=True)
        (dir_b / "shared" / "tied.txt").write_bytes(
            b"beta unique payload beta beta beta"
        )

        from src.vault.upload.folder_state import fetch_folder_state
        vault_a = _vault()
        vault_b = _vault()
        try:
            # Snapshot B's pre-A view; pass via parent_state so B's
            # publish 409s and runs the §D4 merge tie-break.
            pre_a_state_b = fetch_folder_state(vault_b, relay, DOCS_ID, device_b)
            res_a = upload_folder(
                vault=vault_a, relay=relay, manifest=shared_parent,
                local_root=dir_a,
                remote_folder_id=DOCS_ID, remote_sub_path="",
                author_device_id=device_a, created_at=same_ts,
            )
            res_b = upload_folder(
                vault=vault_b, relay=relay, manifest=shared_parent,
                local_root=dir_b,
                remote_folder_id=DOCS_ID, remote_sub_path="",
                author_device_id=device_b, created_at=same_ts,
                parent_state=pre_a_state_b,
            )
        finally:
            vault_a.close()
            vault_b.close()

        # Both devices used the same entry_id E0 (from the seed). The
        # winner's version_id should land as latest after B's merge.
        winner = max(
            (device_a, device_b),
            key=lambda d: hashlib.sha256(d.encode("utf-8")).digest(),
        )
        # Find A's and B's version_ids from their per-file upload results.
        # Each folder upload returns one UploadResult per file uploaded.
        a_version_id = next(
            r.version_id for r in res_a.uploaded
            if r.path == "shared/tied.txt"
        )
        b_version_id = next(
            r.version_id for r in res_b.uploaded
            if r.path == "shared/tied.txt"
        )
        expected = a_version_id if winner == device_a else b_version_id
        entry = find_file_entry(res_b.manifest, DOCS_ID, "shared/tied.txt")
        self.assertEqual(entry["latest_version_id"], expected)

    def test_concurrent_new_file_at_same_path_renames_imported_folder_batch(self) -> None:
        """§D4 row 1 on folder-batch CAS-retry path: two devices create
        different files at the same path; the late publisher's entry is
        renamed via ``_imported_rename`` per the merge helper."""
        dir_a = self.tmpdir / "from_a"
        (dir_a / "shared").mkdir(parents=True)
        (dir_a / "shared" / "taken.bin").write_bytes(b"alpha alpha alpha alpha alpha")
        dir_b = self.tmpdir / "from_b"
        (dir_b / "shared").mkdir(parents=True)
        (dir_b / "shared" / "taken.bin").write_bytes(b"beta beta beta beta beta beta beta")

        manifest = _empty_manifest()
        relay = FakeUploadRelay(manifest=manifest)
        vault_a = _vault()
        vault_b = _vault()
        try:
            relay.current_revision = int(manifest.get("parent_revision", 0))
            vault_a.publish_manifest(relay, manifest)
            seed_sharded_state_from_manifest(vault_a, relay, manifest)
            relay.published_manifests = []
            relay.published_shards = []
            relay.published_roots = []
            from src.vault.upload.folder_state import fetch_folder_state
            # Snapshot B's pre-A view so its upload generates a fresh
            # entry_id; B's CAS-retry merge then runs row-1 rename.
            pre_a_state_b = fetch_folder_state(vault_b, relay, DOCS_ID, "b" * 32)
            upload_folder(
                vault=vault_a, relay=relay, manifest=manifest,
                local_root=dir_a,
                remote_folder_id=DOCS_ID, remote_sub_path="",
                author_device_id="a" * 32,
            )
            upload_folder(
                vault=vault_b, relay=relay, manifest=manifest,
                local_root=dir_b,
                remote_folder_id=DOCS_ID, remote_sub_path="",
                author_device_id="b" * 32,
                parent_state=pre_a_state_b,
            )
        finally:
            vault_a.close()
            vault_b.close()

        # Decode the post-publish sharded state and verify both entries
        # coexist: original "shared/taken.bin" + rename-imported variant.
        from src.vault.manifest import assemble_unified_manifest
        observer = _vault()
        try:
            root = observer.fetch_root_manifest(relay)
            shard = observer.fetch_folder_shard(relay, DOCS_ID)
            head = assemble_unified_manifest(root, {DOCS_ID: shard})
        finally:
            observer.close()
        entry_paths = {
            e["path"]
            for f in head["remote_folders"]
            if f["remote_folder_id"] == DOCS_ID
            for e in f["entries"]
        }
        self.assertIn("shared/taken.bin", entry_paths)
        self.assertIn("shared/taken (imported).bin", entry_paths)


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
        # Phase H sharded surface (root + per-folder shards). The
        # legacy manifest state above is kept separate so existing
        # tests stay green; sync tests that need the sharded surface
        # seed it explicitly via the publish_root_manifest /
        # publish_shard_with_root path (mirrors FakeShardedRelay).
        self.root_envelope: bytes = b""
        self.root_revision: int = 0
        self.root_hash: str = ""
        self.shards: dict[str, dict] = {}   # folder_id → {envelope, revision, hash}
        self.published_shards: list[dict] = []
        self.published_roots: list[dict] = []
        self.shard_with_root_puts = 0
        self.root_gets = 0
        self.shard_gets: dict[str, int] = {}

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
        # Phase H step 7d crash-recovery: candidates whose chunks no
        # longer exist server-side land in ``already_deleted_chunk_ids``
        # so the client can clean stale shard entries without re-running
        # ``gc_execute``.
        already_deleted = [c for c in candidate_chunk_ids if c not in self.chunks]
        self.gc_plans[plan_id] = {
            "manifest_revision": manifest_revision,
            "safe_to_delete": safe,
        }
        return {
            "plan_id": plan_id,
            "safe_to_delete": list(safe),
            "still_referenced": [],
            "already_deleted_chunk_ids": list(already_deleted),
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

    # --- sharded surface (Phase H) -------------------------------------
    def get_root(self, vault_id, vault_access_secret):
        self.root_gets += 1
        return {
            "root_revision": self.root_revision,
            "parent_root_revision": max(0, self.root_revision - 1),
            "root_hash": self.root_hash,
            "root_ciphertext": self.root_envelope,
            "root_size": len(self.root_envelope),
        }

    def put_root(
        self, vault_id, vault_access_secret, *,
        expected_current_root_revision, new_root_revision,
        parent_root_revision, root_hash, root_ciphertext,
    ):
        if int(expected_current_root_revision) != self.root_revision:
            import base64
            raise VaultCASConflictError({
                "code": "vault_root_conflict",
                "message": "fake root CAS conflict",
                "details": {
                    "current_root_revision": self.root_revision,
                    "current_root_hash": self.root_hash,
                    "current_root_ciphertext":
                        base64.b64encode(self.root_envelope).decode("ascii"),
                    "current_root_size": len(self.root_envelope),
                },
            })
        self.root_revision = int(new_root_revision)
        self.root_envelope = bytes(root_ciphertext)
        self.root_hash = root_hash
        self.published_roots.append({
            "new_root_revision": new_root_revision,
            "root_hash": root_hash,
        })
        return {"root_revision": new_root_revision, "root_hash": root_hash}

    def get_shard(self, vault_id, vault_access_secret, remote_folder_id):
        self.shard_gets[remote_folder_id] = self.shard_gets.get(remote_folder_id, 0) + 1
        head = self.shards.get(remote_folder_id)
        if head is None:
            from src.vault.relay_errors import VaultNotFoundError
            raise VaultNotFoundError(f"shard {remote_folder_id} not found")
        return {
            "remote_folder_id": remote_folder_id,
            "shard_revision": head["revision"],
            "parent_shard_revision": max(0, head["revision"] - 1),
            "shard_hash": head["hash"],
            "shard_ciphertext": head["envelope"],
            "shard_size": len(head["envelope"]),
        }

    def put_shard(
        self, vault_id, vault_access_secret, remote_folder_id, *,
        expected_current_shard_revision, new_shard_revision,
        parent_shard_revision, shard_hash, shard_ciphertext,
    ):
        current = self.shards.get(remote_folder_id)
        current_rev = current["revision"] if current else 0
        if int(expected_current_shard_revision) != current_rev:
            import base64
            raise VaultCASConflictError({
                "code": "vault_shard_conflict",
                "message": "fake shard CAS conflict",
                "details": {
                    "remote_folder_id": remote_folder_id,
                    "current_shard_revision": current_rev,
                    "current_shard_hash": current["hash"] if current else "",
                    "current_shard_ciphertext":
                        base64.b64encode(current["envelope"] if current else b"").decode("ascii"),
                    "current_shard_size": len(current["envelope"]) if current else 0,
                },
            })
        self.shards[remote_folder_id] = {
            "envelope": bytes(shard_ciphertext),
            "revision": int(new_shard_revision),
            "hash": shard_hash,
        }
        self.published_shards.append({
            "remote_folder_id": remote_folder_id,
            "new_shard_revision": new_shard_revision,
            "shard_hash": shard_hash,
        })
        return {"shard_revision": new_shard_revision, "shard_hash": shard_hash}

    def put_shard_with_root(
        self, vault_id, vault_access_secret, remote_folder_id, *,
        shard, root,
    ):
        self.shard_with_root_puts += 1
        import base64
        current = self.shards.get(remote_folder_id)
        current_shard_rev = current["revision"] if current else 0
        shard_stale = int(shard["expected_current_shard_revision"]) != current_shard_rev
        root_stale = int(root["expected_current_root_revision"]) != self.root_revision
        if shard_stale and root_stale:
            raise VaultCASConflictError({
                "code": "vault_shard_root_conflict",
                "message": "fake atomic CAS conflict (both)",
                "details": {
                    "remote_folder_id": remote_folder_id,
                    "current_shard_revision": current_shard_rev,
                    "current_shard_hash": current["hash"] if current else "",
                    "current_shard_ciphertext":
                        base64.b64encode(current["envelope"] if current else b"").decode("ascii"),
                    "current_shard_size": len(current["envelope"]) if current else 0,
                    "current_root_revision": self.root_revision,
                    "current_root_hash": self.root_hash,
                    "current_root_ciphertext":
                        base64.b64encode(self.root_envelope).decode("ascii"),
                    "current_root_size": len(self.root_envelope),
                },
            })
        if shard_stale:
            raise VaultCASConflictError({
                "code": "vault_shard_conflict",
                "message": "fake atomic CAS conflict (shard)",
                "details": {
                    "remote_folder_id": remote_folder_id,
                    "current_shard_revision": current_shard_rev,
                    "current_shard_hash": current["hash"] if current else "",
                    "current_shard_ciphertext":
                        base64.b64encode(current["envelope"] if current else b"").decode("ascii"),
                    "current_shard_size": len(current["envelope"]) if current else 0,
                },
            })
        if root_stale:
            raise VaultCASConflictError({
                "code": "vault_root_conflict",
                "message": "fake atomic CAS conflict (root)",
                "details": {
                    "current_root_revision": self.root_revision,
                    "current_root_hash": self.root_hash,
                    "current_root_ciphertext":
                        base64.b64encode(self.root_envelope).decode("ascii"),
                    "current_root_size": len(self.root_envelope),
                },
            })
        # Commit both atomically.
        self.shards[remote_folder_id] = {
            "envelope": bytes(shard["shard_ciphertext"]),
            "revision": int(shard["new_shard_revision"]),
            "hash": shard["shard_hash"],
        }
        self.root_revision = int(root["new_root_revision"])
        self.root_envelope = bytes(root["root_ciphertext"])
        self.root_hash = root["root_hash"]
        self.published_shards.append({
            "remote_folder_id": remote_folder_id,
            "new_shard_revision": shard["new_shard_revision"],
            "shard_hash": shard["shard_hash"],
        })
        self.published_roots.append({
            "new_root_revision": root["new_root_revision"],
            "root_hash": root["root_hash"],
        })
        return {
            "shard_revision": shard["new_shard_revision"],
            "shard_hash": shard["shard_hash"],
            "root_revision": root["new_root_revision"],
            "root_hash": root["root_hash"],
        }


def seed_sharded_state_from_manifest(vault, relay, manifest: dict) -> None:
    """Mirror a legacy unified manifest into FakeUploadRelay's sharded
    surface so the Phase-H-ported sync engine can fetch a root + shard
    pair instead of the legacy manifest.

    Bumps the root chain by one publish per folder so each folder's
    pointer carries its actual shard_hash (auto-patched by
    publish_shard_with_root). The legacy ``vault.publish_manifest``
    path is what sync tests historically used to seed state; this
    helper is the missing second-half-of-publish that the Phase H
    sync engine needs.

    Re-runnable: calling it after each mutation keeps the sharded
    view aligned with the legacy ``relay.current_manifest`` view.
    """
    from src.vault.manifest import (
        make_folder_shard,
        make_root_folder_pointer,
        make_root_manifest,
    )

    folders = list(manifest.get("remote_folders", []) or [])
    created_at = str(manifest.get("created_at", ""))
    author = str(manifest.get("author_device_id", ""))
    vault_id = str(manifest.get("vault_id", "")) or vault.vault_id

    pointers = []
    for folder in folders:
        rf_id = str(folder["remote_folder_id"])
        head = relay.shards.get(rf_id, {})
        pointers.append(make_root_folder_pointer(
            remote_folder_id=rf_id,
            display_name_enc=str(folder.get("display_name_enc", "")),
            created_at=str(folder.get("created_at", created_at)),
            created_by_device_id=str(folder.get("created_by_device_id", author)),
            retention_policy=folder.get("retention_policy"),
            ignore_patterns=list(folder.get("ignore_patterns", []) or []),
            state=str(folder.get("state", "active")),
            shard_revision=int(head.get("revision", 0)),
            shard_hash=str(head.get("hash", "")),
        ))

    next_root_revision = int(relay.root_revision) + 1
    root = make_root_manifest(
        vault_id=vault_id,
        root_revision=next_root_revision,
        parent_root_revision=int(relay.root_revision),
        created_at=created_at, author_device_id=author,
        remote_folders=pointers,
    )
    vault.publish_root_manifest(relay, root)

    for folder in folders:
        rf_id = str(folder["remote_folder_id"])
        current_shard_revision = int(relay.shards.get(rf_id, {}).get("revision", 0))
        shard = make_folder_shard(
            vault_id=vault_id,
            remote_folder_id=rf_id,
            shard_revision=current_shard_revision + 1,
            parent_shard_revision=current_shard_revision,
            created_at=created_at,
            author_device_id=author,
            entries=list(folder.get("entries", []) or []),
        )
        # publish_shard_with_root requires a fresh root revision; bump
        # whatever the relay currently has so the CAS passes.
        current_root = vault.fetch_root_manifest(relay)
        next_root = dict(current_root)
        next_root["root_revision"] = int(current_root["root_revision"]) + 1
        next_root["parent_root_revision"] = int(current_root["root_revision"])
        next_root["created_at"] = created_at
        next_root["author_device_id"] = author
        vault.publish_shard_with_root(relay, rf_id, shard, next_root)


def mirror_legacy_from_sharded(vault, relay) -> None:
    """Rebuild the unified manifest from the current root + shards and
    publish it via legacy ``put_manifest`` so legacy ``fetch_manifest``
    reflects the latest sharded mutations.

    Advances ``relay.current_revision`` by exactly one per call (the
    next legacy revision after the relay's current state), regardless
    of how many sharded operations have happened underneath. Tests
    that count legacy revisions ("bootstrap + upload + delete = 3
    publishes") therefore stay readable as the upload module ports
    over to sharded. Removed in step 7 alongside the legacy path.
    """
    from src.vault.manifest import assemble_unified_manifest

    root = vault.decrypt_root_envelope(relay.root_envelope)
    shards = {
        fid: vault.decrypt_shard_envelope(s["envelope"], fid)
        for fid, s in relay.shards.items()
    }
    unified = assemble_unified_manifest(root, shards)
    # Advance the legacy revision chain by exactly one — independent
    # of the sharded chain's revision pacing.
    legacy_parent = int(relay.current_revision)
    unified["parent_revision"] = legacy_parent
    unified["revision"] = legacy_parent + 1
    vault.publish_manifest(relay, unified)


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
