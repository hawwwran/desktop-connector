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
    download_version,
    previous_version_filename,
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
        # F-D11: skip the real ``time.sleep`` between chunk-missing
        # retries so tests don't pay 1+2+4 = 7 s per missing-chunk
        # case. The retry counter + log emission still execute as in
        # production; only the wall-clock wait is bypassed.
        self._sleep_patch = mock.patch(
            "src.vault_download.chunks._chunk_missing_sleep", lambda _s: None,
        )
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()
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

    def test_chunk_missing_retries_until_present_then_succeeds(self) -> None:
        """F-D11: per spec §6.9, ``vault_chunk_missing`` is auto-retried
        within the transfer budget. A relay that reports a chunk as
        missing on the first head call but present on the second
        (replication caught up) must trigger a successful download —
        not a terminal failure on the first attempt.
        """
        manifest, chunks = _manifest_and_chunks([b"hello ", b"vault"])
        relay = _FlakyChunkRelay(
            chunks=chunks,
            head_misses_for={CHUNK_A: 1, CHUNK_B: 1},  # 1 miss each
        )
        destination = self.tmpdir / "report.txt"
        vault = _vault()
        try:
            out = download_latest_file(
                vault=vault, relay=relay, manifest=manifest,
                path="Documents/report.txt", destination=destination,
            )
        finally:
            vault.close()
        self.assertEqual(out.read_bytes(), b"hello vault")
        # The retry helper re-polled batch_head_chunks at least twice.
        self.assertGreaterEqual(len(relay.batch_head_calls), 2)

    def test_chunk_missing_retry_budget_exhausted_raises(self) -> None:
        """F-D11: budget = 3 retries (4 attempts total). A chunk that
        stays missing across all attempts surfaces as terminal
        ``VaultChunkMissingError``.
        """
        manifest, chunks = _manifest_and_chunks([b"hello ", b"vault"])
        # Forever-missing chunk B.
        relay = _FlakyChunkRelay(
            chunks={CHUNK_A: chunks[CHUNK_A]},
            head_misses_for={CHUNK_B: 99},
        )
        destination = self.tmpdir / "report.txt"
        vault = _vault()
        try:
            with self.assertRaises(VaultChunkMissingError):
                download_latest_file(
                    vault=vault, relay=relay, manifest=manifest,
                    path="Documents/report.txt", destination=destination,
                )
        finally:
            vault.close()
        # 4 batch_head_calls total: 1 initial + 3 retries.
        self.assertEqual(len(relay.batch_head_calls), 4)
        self.assertFalse(destination.exists())

    def test_get_chunk_404_mid_fetch_retries_then_succeeds(self) -> None:
        """F-D11: a chunk that passes head but 404s on bytes-fetch
        (concurrent eviction race) must also retry. We exercise this
        by handing the helper a relay whose ``get_chunk`` raises
        ``VaultChunkMissingError`` on the first call, then succeeds.
        """
        manifest, chunks = _manifest_and_chunks([b"hello ", b"vault"])
        relay = _FlakyChunkRelay(
            chunks=chunks,
            get_chunk_raises_for={CHUNK_B: 1},  # 1 raise then OK
        )
        destination = self.tmpdir / "report.txt"
        vault = _vault()
        try:
            out = download_latest_file(
                vault=vault, relay=relay, manifest=manifest,
                path="Documents/report.txt", destination=destination,
            )
        finally:
            vault.close()
        self.assertEqual(out.read_bytes(), b"hello vault")
        # CHUNK_B was raised once before succeeding (counter decremented
        # to 0); both chunks made it into the success ledger exactly once.
        self.assertEqual(relay.get_chunk_raises_for[CHUNK_B], 0)
        self.assertEqual(relay.downloaded.count(CHUNK_B), 1)
        self.assertEqual(relay.downloaded.count(CHUNK_A), 1)

    def test_cache_unavailable_validation_forces_fresh_fetch(self) -> None:
        """F-D10: when the relay's batch HEAD omits *both* ``size`` and
        ``hash``, the cache is no longer trustable on its own (AEAD
        decrypt catches ciphertext bit-flips but not a hardlink-poisoned
        bytes-of-different-size swap). The loader returns ``None`` so
        the caller re-fetches from the relay.

        We exercise this by handing a relay that produces head dicts
        with neither field, pre-seeding a tampered cache file, and
        confirming the download re-fetches over the network.
        """
        from src.vault_download import _load_cached_chunk

        # Build a manifest + cache that would otherwise fast-path.
        manifest, chunks = _manifest_and_chunks([b"hello"])
        cache_dir = self.tmpdir / "cache"
        cache_path = (
            cache_dir / "chunks" / VAULT_ID / CHUNK_A[6:8] / CHUNK_A
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Tampered bytes (different size, same chunk_id).
        cache_path.write_bytes(b"BAD-CONTENT")
        # Empty head dict — neither size nor hash.
        loaded = _load_cached_chunk(
            chunk_cache_dir=cache_dir, vault_id=VAULT_ID,
            chunk_id=CHUNK_A, head={"present": True},
        )
        self.assertIsNone(
            loaded,
            "F-D10: cache must be treated as miss when head has no "
            "size + no hash, even if file exists on disk",
        )
        # Sanity: when head DOES include a size and the cached bytes
        # match, the loader returns the cached data (validates against
        # head fields rather than going to network).
        good_bytes = chunks[CHUNK_A]
        cache_path.write_bytes(good_bytes)
        loaded = _load_cached_chunk(
            chunk_cache_dir=cache_dir, vault_id=VAULT_ID,
            chunk_id=CHUNK_A, head={"present": True, "size": len(good_bytes)},
        )
        self.assertEqual(loaded, good_bytes)

    def test_chunk_missing_honours_server_retry_after_ms(self) -> None:
        """F-D11: when the relay supplies ``retry_after_ms`` in the
        error details, the backoff helper uses it instead of the
        exponential default. We pin the math by capturing the delays
        the helper requested via the ``_chunk_missing_sleep`` hook.
        """
        from src.vault_relay_errors import VaultChunkMissingError as _Exc

        delays_requested: list[float] = []

        def _capture(seconds: float) -> None:
            delays_requested.append(seconds)

        # A 0.5 s server hint; helper should request that, not 1.0 s.
        manifest, chunks = _manifest_and_chunks([b"hello"])
        called = {"n": 0}

        class _HinterRelay:
            def __init__(self) -> None:
                self.batch_head_calls: list[list[str]] = []
                self.downloaded: list[str] = []

            def batch_head_chunks(self, vault_id, vault_access_secret, chunk_ids):
                self.batch_head_calls.append(list(chunk_ids))
                return {
                    chunk_id: {"present": True, "size": len(chunks[chunk_id])}
                    for chunk_id in chunk_ids
                }

            def get_chunk(self, vault_id, vault_access_secret, chunk_id):
                called["n"] += 1
                if called["n"] == 1:
                    raise _Exc("vault chunk missing", )  # default details
                if called["n"] == 2:
                    err = _Exc("vault chunk missing")
                    err.details = {"retry_after_ms": 500}
                    raise err
                self.downloaded.append(chunk_id)
                return chunks[chunk_id]

        relay = _HinterRelay()
        destination = self.tmpdir / "report.txt"
        vault = _vault()
        with mock.patch(
            "src.vault_download.chunks._chunk_missing_sleep", _capture,
        ):
            try:
                download_latest_file(
                    vault=vault, relay=relay, manifest=manifest,
                    path="Documents/report.txt", destination=destination,
                )
            finally:
                vault.close()
        # First retry: no hint → default exp backoff (1.0 s).
        # Second retry: hint=500 ms → 0.5 s.
        self.assertEqual(delays_requested[:2], [1.0, 0.5])

    def test_chunk_cache_prune_caps_per_vault_size(self) -> None:
        """F-D04: ``prune_vault_chunk_cache`` caps the per-vault chunk
        cache by deleting oldest-touched files first. We pre-seed a
        cache with several files, set a tight cap, and verify that
        only the freshest survive.
        """
        from src.vault_download import (
            prune_vault_chunk_cache, vault_chunk_cache_path,
        )

        cache_dir = self.tmpdir / "cache"
        chunk_ids = [f"ch_v1_{i:024d}" for i in range(5)]
        for index, chunk_id in enumerate(chunk_ids):
            path = vault_chunk_cache_path(cache_dir, VAULT_ID, chunk_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"X" * 1024)
            # Stagger atimes by index seconds so the prune ordering is
            # deterministic regardless of the OS's atime semantics.
            atime = 1_000_000.0 + index
            os.utime(path, (atime, atime))

        # Cap = 2 KiB (only 2 of 5 files survive).
        freed = prune_vault_chunk_cache(
            cache_dir, VAULT_ID, max_bytes=2 * 1024,
        )
        self.assertGreater(freed, 0)
        surviving = sorted(
            p.name for p in (
                cache_dir / "chunks" / VAULT_ID
            ).rglob("*")
            if p.is_file()
        )
        # Three oldest deleted; two freshest kept.
        self.assertEqual(len(surviving), 2)
        # Freshest IDs are the last two by our atime stamping.
        self.assertIn(chunk_ids[3], surviving)
        self.assertIn(chunk_ids[4], surviving)

    def test_chunk_cache_prune_no_op_when_under_cap(self) -> None:
        """F-D04: under-cap caches are a no-op (no walk-and-sort cost
        beyond the initial size sum). Important so the per-store
        opportunistic call doesn't pay quadratic cost.
        """
        from src.vault_download import (
            prune_vault_chunk_cache, vault_chunk_cache_path,
        )
        cache_dir = self.tmpdir / "cache"
        chunk_id = "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
        path = vault_chunk_cache_path(cache_dir, VAULT_ID, chunk_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)
        freed = prune_vault_chunk_cache(
            cache_dir, VAULT_ID, max_bytes=10 * 1024,
        )
        self.assertEqual(freed, 0)
        self.assertTrue(path.exists())

    def test_chunk_cache_prune_handles_missing_directory(self) -> None:
        """F-D04: prune is safe to call on a vault with no cache yet
        (e.g. immediately after disconnect → reconnect).
        """
        from src.vault_download import prune_vault_chunk_cache
        cache_dir = self.tmpdir / "cache"  # Doesn't exist yet.
        freed = prune_vault_chunk_cache(cache_dir, VAULT_ID)
        self.assertEqual(freed, 0)

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
                "src.vault_download.paths.shutil.disk_usage",
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
            # F-D09: an unsafe path is skipped with a warning rather
            # than aborting the whole batch. With only the malicious
            # entry present, the result is an empty download — no
            # bytes ever land outside the destination root.
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
        self.assertFalse((self.tmpdir / "Documents" / "evil.txt").exists())

    def test_keep_both_folder_policy_preserves_existing_destination(self) -> None:
        destination = self.tmpdir / "Documents"
        destination.mkdir()
        out = resolve_folder_destination(destination, "keep_both")

        self.assertEqual(out.name, "Documents (downloaded 1)")

    def test_download_version_writes_side_path_matching_chosen_bytes(self) -> None:
        manifest, chunks, versions = _multi_version_manifest_and_chunks([
            (b"v1-bytes", "2026-04-01T10:00:00.000Z"),
            (b"v2-bytes-payload", "2026-04-15T11:00:00.000Z"),
            (b"v3-bytes-final-cut", "2026-05-01T12:00:00.000Z"),
        ])
        relay = FakeChunkRelay(chunks)
        vault = _vault()

        latest = self.tmpdir / "report.txt"
        latest.write_bytes(b"v3-bytes-final-cut")

        v2 = versions[1]
        target_name = previous_version_filename("report.txt", v2)
        destination = self.tmpdir / target_name

        try:
            out = download_version(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path="Documents/report.txt",
                version_id=v2["version_id"],
                destination=destination,
            )
        finally:
            vault.close()

        self.assertEqual(out, destination)
        self.assertNotEqual(out.name, "report.txt")
        self.assertIn("(version ", out.name)
        self.assertEqual(out.read_bytes(), b"v2-bytes-payload")
        self.assertEqual(latest.read_bytes(), b"v3-bytes-final-cut")

    def test_download_version_keep_both_when_side_path_exists(self) -> None:
        manifest, chunks, versions = _multi_version_manifest_and_chunks([
            (b"first", "2026-04-01T10:00:00.000Z"),
            (b"second", "2026-04-15T11:00:00.000Z"),
        ])
        relay = FakeChunkRelay(chunks)
        v1 = versions[0]
        suggested = previous_version_filename("report.txt", v1)
        existing = self.tmpdir / suggested
        existing.write_bytes(b"older")

        vault = _vault()
        try:
            out = download_version(
                vault=vault,
                relay=relay,
                manifest=manifest,
                path="Documents/report.txt",
                version_id=v1["version_id"],
                destination=existing,
                existing_policy="keep_both",
            )
        finally:
            vault.close()

        self.assertEqual(existing.read_bytes(), b"older")
        self.assertNotEqual(out, existing)
        self.assertEqual(out.read_bytes(), b"first")

    def test_download_version_unknown_version_raises(self) -> None:
        manifest, chunks, _versions = _multi_version_manifest_and_chunks([
            (b"only", "2026-04-01T10:00:00.000Z"),
        ])
        relay = FakeChunkRelay(chunks)
        vault = _vault()
        try:
            with self.assertRaisesRegex(KeyError, "version not found"):
                download_version(
                    vault=vault,
                    relay=relay,
                    manifest=manifest,
                    path="Documents/report.txt",
                    version_id="fv_v1_zzzzzzzzzzzzzzzzzzzzzzzz",
                    destination=self.tmpdir / "report (version 2026-04-01 10-00).txt",
                )
        finally:
            vault.close()

    def test_previous_version_filename_uses_a20_style_timestamp(self) -> None:
        version = {
            "modified_at": "2026-05-02T17:30:00.000Z",
            "version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        }
        self.assertEqual(
            previous_version_filename("report.docx", version),
            "report (version 2026-05-02 17-30).docx",
        )
        self.assertEqual(
            previous_version_filename("README", version),
            "README (version 2026-05-02 17-30)",
        )

    def test_previous_version_filename_falls_back_to_version_id(self) -> None:
        version = {"version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa"}
        out = previous_version_filename("report.txt", version)
        self.assertTrue(out.startswith("report (version fv_v1"))
        self.assertTrue(out.endswith(").txt"))


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


class _FlakyChunkRelay:
    """F-D11 test fixture: simulates chunks that are transiently missing.

    ``head_misses_for[chunk_id]`` is the number of head-checks that
    will report ``present=False`` before the chunk becomes available;
    ``get_chunk_raises_for[chunk_id]`` is the number of bytes-fetches
    that will raise ``VaultChunkMissingError`` before succeeding.
    Both counters decrement on each invocation, so a value of 1 means
    "transient miss followed by stable success".
    """

    def __init__(
        self,
        *,
        chunks: dict[str, bytes],
        head_misses_for: dict[str, int] | None = None,
        get_chunk_raises_for: dict[str, int] | None = None,
    ) -> None:
        self.chunks = dict(chunks)
        self.head_misses_for = dict(head_misses_for or {})
        self.get_chunk_raises_for = dict(get_chunk_raises_for or {})
        self.downloaded: list[str] = []
        self.batch_head_calls: list[list[str]] = []

    def batch_head_chunks(self, vault_id, vault_access_secret, chunk_ids):
        self.batch_head_calls.append(list(chunk_ids))
        out: dict[str, dict[str, object]] = {}
        for chunk_id in chunk_ids:
            misses_left = self.head_misses_for.get(chunk_id, 0)
            if misses_left > 0:
                self.head_misses_for[chunk_id] = misses_left - 1
                out[chunk_id] = {"present": False}
                continue
            if chunk_id in self.chunks:
                out[chunk_id] = {
                    "present": True,
                    "size": len(self.chunks[chunk_id]),
                    "hash": hashlib.sha256(self.chunks[chunk_id]).hexdigest(),
                }
            else:
                out[chunk_id] = {"present": False}
        return out

    def get_chunk(self, vault_id, vault_access_secret, chunk_id):
        raises_left = self.get_chunk_raises_for.get(chunk_id, 0)
        if raises_left > 0:
            self.get_chunk_raises_for[chunk_id] = raises_left - 1
            from src.vault_relay_errors import VaultChunkMissingError
            raise VaultChunkMissingError(f"vault chunk missing: {chunk_id}")
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


def _multi_version_manifest_and_chunks(
    versions: list[tuple[bytes, str]],
) -> tuple[dict, dict[str, bytes], list[dict]]:
    chunks: dict[str, bytes] = {}
    version_records: list[dict] = []
    alphabet = "abcdefghijklmnopqrstuvwx"
    for index, (plaintext, modified_at) in enumerate(versions):
        letter = alphabet[index % len(alphabet)]
        version_id = f"fv_v1_{letter * 24}"
        chunk_id = f"ch_v1_{letter * 24}"
        encrypted = _encrypt_chunk_for(plaintext, 0, FILE_ID, version_id)
        chunks[chunk_id] = encrypted
        version_records.append({
            "version_id": version_id,
            "created_at": modified_at,
            "modified_at": modified_at,
            "logical_size": len(plaintext),
            "ciphertext_size": len(encrypted),
            "content_fingerprint": "unused",
            "chunks": [{
                "chunk_id": chunk_id,
                "index": 0,
                "plaintext_size": len(plaintext),
                "ciphertext_size": len(encrypted),
            }],
            "author_device_id": AUTHOR,
        })

    entry = {
        "entry_id": FILE_ID,
        "type": "file",
        "path": "report.txt",
        "latest_version_id": version_records[-1]["version_id"],
        "deleted": False,
        "versions": version_records,
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
    return manifest, chunks, version_records


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
