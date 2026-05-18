"""§4.M1 — orphan-chunk reaper acceptance tests.

Library: :func:`vault.ops.eviction.reap_orphan_chunks`. The reaper
runs as a stage-0 pre-pass to the autosync cycle: it lists every
chunk the relay still holds, subtracts the set referenced by the
live manifest, and DELETEs the diff via the existing gc plumbing.
"""

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

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
)
from src.vault.ops.eviction import (  # noqa: E402
    OrphanReapResult,
    reap_orphan_chunks,
)
from src.vault.upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import (  # noqa: E402
    FakeUploadRelay,
    seed_sharded_state,
)


class OrphanReapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_orphan_reap_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_orphans_returns_empty_result(self) -> None:
        """When the manifest references every chunk on the relay,
        the reaper is a strict no-op — empty lists, zero bytes."""
        manifest = _empty_manifest()
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
            local = self.tmpdir / "tracked.txt"
            local.write_bytes(b"every chunk here is referenced")
            upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="tracked.txt",
                author_device_id=AUTHOR,
            )

            chunks_before = set(relay.chunks)
            result = reap_orphan_chunks(
                vault=vault, relay=relay, author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        self.assertIsInstance(result, OrphanReapResult)
        self.assertEqual(result.orphan_chunk_ids, [])
        self.assertEqual(result.deleted_chunk_ids, [])
        self.assertEqual(result.bytes_freed, 0)
        # Reaper didn't touch any legitimately-referenced chunk.
        self.assertEqual(set(relay.chunks), chunks_before)

    def test_empty_vault_returns_noop(self) -> None:
        """A fresh empty manifest + empty relay → noop. Guards against
        the reaper firing gc_plan with an empty candidate list."""
        manifest = _empty_manifest()
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
            result = reap_orphan_chunks(
                vault=vault, relay=relay, author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        self.assertEqual(result.orphan_chunk_ids, [])
        self.assertEqual(result.deleted_chunk_ids, [])

    def test_unreferenced_chunks_get_deleted(self) -> None:
        """Inject a chunk into the relay that the manifest never
        references; the reaper computes the diff and DELETEs it via
        the gc plumbing."""
        manifest = _empty_manifest()
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
            local = self.tmpdir / "real.txt"
            local.write_bytes(b"real upload bytes")
            upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="real.txt",
                author_device_id=AUTHOR,
            )

            referenced_before = set(relay.chunks)

            # Inject a synthetic orphan chunk (40 hex chars per the
            # chunk-id regex). The manifest never references it.
            orphan_id = "ab" * 32  # 64 hex chars (matches sha256 hex shape)
            relay.chunks[orphan_id] = b"orphan ciphertext content"

            result = reap_orphan_chunks(
                vault=vault, relay=relay, author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        self.assertEqual(result.orphan_chunk_ids, [orphan_id])
        self.assertEqual(result.deleted_chunk_ids, [orphan_id])
        self.assertGreater(result.bytes_freed, 0)
        # Referenced chunks survived; only the orphan got dropped.
        self.assertEqual(set(relay.chunks), referenced_before)

    def test_uses_purpose_sync_not_forced_eviction(self) -> None:
        """Orphan chunks aren't user data — the reaper goes through
        the existing gc plumbing with ``purpose='sync'`` (default
        role), NOT the admin-gated ``purpose='forced_eviction'`` path.
        A compromised sync-only device can still trigger this
        cleanup; that's fine because by definition the chunks are
        unreachable through any manifest reference."""
        manifest = _empty_manifest()
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
            orphan_id = "cd" * 32
            relay.chunks[orphan_id] = b"orphan bytes"

            reap_orphan_chunks(
                vault=vault, relay=relay, author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        purposes = [p["purpose"] for p in relay.gc_plans.values()]
        self.assertIn("sync", purposes)
        self.assertNotIn("forced_eviction", purposes)

    def test_max_orphans_per_pass_caps_diff(self) -> None:
        """When the orphan set exceeds the cap, only the first N are
        attempted on this pass; the residue is left for the next
        tick. Bounds the time an autosync tick spends on cleanup."""
        manifest = _empty_manifest()
        relay = FakeUploadRelay()
        vault = _vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
            # Seed 5 distinct orphan chunk_ids.
            orphan_ids = [
                f"{i:0>2x}" * 32 for i in range(5)
            ]
            for oid in orphan_ids:
                relay.chunks[oid] = f"orphan {oid[:6]}".encode()

            result = reap_orphan_chunks(
                vault=vault, relay=relay, author_device_id=AUTHOR,
                max_orphans_per_pass=2,
            )
        finally:
            vault.close()

        self.assertEqual(len(result.orphan_chunk_ids), 2)
        self.assertEqual(len(result.deleted_chunk_ids), 2)
        # The 3 capped-off orphans still sit on the relay; a future
        # tick will pick them up.
        self.assertEqual(
            sum(1 for oid in orphan_ids if oid in relay.chunks),
            3,
        )


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


if __name__ == "__main__":
    unittest.main()
