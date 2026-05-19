"""§5.C1 / §5.M6 — migration preflight + clear_previous_relay.

The full ``run_migration`` end-to-end suite lives in
``test_desktop_vault_migration_runner.py``. This file pins the two
new helpers landed for the wizard build:

- ``migration_preflight`` returns inventory counts without writing
  to the target relay (the wizard surfaces this on its Confirm page).
- ``clear_previous_relay`` resets the carried-over ``previous_relay_url``
  so an A → B → C migration sequence records ``previous = B`` rather
  than the stale A (§5.M6 regression guard).
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
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)
from src.vault.migration.runner import (  # noqa: E402
    MigrationInventory,
    migration_preflight,
)
from src.vault.migration.state import (  # noqa: E402
    MigrationRecord,
    clear_previous_relay,
    transition,
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


SOURCE = "https://old.example.com"
TARGET = "https://new.example.com"
THIRD = "https://newer.example.com"
T_START = "2026-05-04T10:00:00.000Z"


# ---- §5.M6 regression -----------------------------------------------


class ClearPreviousRelayTests(unittest.TestCase):
    def _make_started(self, *, previous: str | None = None) -> MigrationRecord:
        rec = MigrationRecord(
            vault_id=VAULT_ID,
            state="idle",
            source_relay_url=SOURCE,
            target_relay_url=TARGET,
            started_at=T_START,
            previous_relay_url=previous,
        )
        return transition(rec, to="started", now=T_START)

    def test_returns_record_with_previous_relay_url_none(self) -> None:
        rec = self._make_started(previous="https://stale.example.com")
        cleaned = clear_previous_relay(rec)
        self.assertIsNone(cleaned.previous_relay_url)

    def test_preserves_every_other_field(self) -> None:
        rec = self._make_started(previous="https://stale.example.com")
        cleaned = clear_previous_relay(rec)
        self.assertEqual(cleaned.vault_id, rec.vault_id)
        self.assertEqual(cleaned.state, rec.state)
        self.assertEqual(cleaned.source_relay_url, rec.source_relay_url)
        self.assertEqual(cleaned.target_relay_url, rec.target_relay_url)
        self.assertEqual(cleaned.started_at, rec.started_at)
        self.assertEqual(cleaned.verified_at, rec.verified_at)
        self.assertEqual(cleaned.committed_at, rec.committed_at)
        self.assertEqual(cleaned.migration_token, rec.migration_token)

    def test_a_to_b_to_c_records_previous_b_not_a(self) -> None:
        """The full regression scenario: after committing A → B, start
        a fresh B → C migration. Without ``clear_previous_relay`` the
        committed C record would still carry ``previous=A``."""
        a_to_b = MigrationRecord(
            vault_id=VAULT_ID,
            state="idle",
            source_relay_url=SOURCE,
            target_relay_url=TARGET,
            started_at=T_START,
            previous_relay_url=None,
        )
        a_to_b = transition(a_to_b, to="started", now=T_START)
        a_to_b = transition(a_to_b, to="copying", now=T_START)
        a_to_b = transition(a_to_b, to="verified", now=T_START)
        a_to_b = transition(a_to_b, to="committed", now=T_START)
        self.assertEqual(a_to_b.previous_relay_url, SOURCE)

        # Now device wants to migrate B → C. The wizard's first move:
        # call ``clear_previous_relay`` before transitioning.
        b_to_c = MigrationRecord(
            vault_id=VAULT_ID,
            state="idle",
            source_relay_url=TARGET,
            target_relay_url=THIRD,
            started_at="2026-05-10T10:00:00.000Z",
            # Imagine the wizard loaded the old record + reused its
            # previous_relay_url field by mistake — this is what
            # clear_previous_relay protects against.
            previous_relay_url=a_to_b.previous_relay_url,
        )
        b_to_c = clear_previous_relay(b_to_c)
        b_to_c = transition(b_to_c, to="started", now="2026-05-10T10:00:00.000Z")
        b_to_c = transition(b_to_c, to="copying", now="2026-05-10T10:00:00.000Z")
        b_to_c = transition(b_to_c, to="verified", now="2026-05-10T10:00:00.000Z")
        b_to_c = transition(b_to_c, to="committed", now="2026-05-10T10:00:00.000Z")

        self.assertEqual(b_to_c.previous_relay_url, TARGET)
        self.assertNotEqual(b_to_c.previous_relay_url, SOURCE)


# ---- §5.C1 preflight ----------------------------------------------


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
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=1,
        parent_root_revision=0,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
            )
        ],
    )
    shard = make_folder_shard(
        vault_id=VAULT_ID,
        remote_folder_id=DOCS_ID,
        shard_revision=1,
        parent_shard_revision=0,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        entries=[],
    )
    return assemble_unified_manifest(root, {DOCS_ID: shard})


class MigrationPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_migration_preflight_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_vault_returns_zero_chunks(self) -> None:
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
            inventory = migration_preflight(
                vault=vault, source_relay=relay,
            )
        finally:
            vault.close()

        self.assertIsInstance(inventory, MigrationInventory)
        self.assertEqual(inventory.chunk_count, 0)
        self.assertEqual(inventory.ciphertext_bytes_total, 0)
        # The single seeded folder is at shard_revision=0 (pointer
        # only, no shard published yet), so it's not counted as
        # edited.
        self.assertFalse(inventory.has_edited_shards)

    def test_uploaded_chunks_counted(self) -> None:
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
            for path in ("note1.txt", "note2.txt"):
                local = self.tmpdir / path
                local.write_bytes(f"some content for {path}".encode("utf-8") * 4)
                upload_file(
                    vault=vault, relay=relay, manifest=manifest,
                    local_path=local,
                    remote_folder_id=DOCS_ID, remote_path=path,
                    author_device_id=AUTHOR,
                )

            chunks_before = set(relay.chunks)
            inventory = migration_preflight(
                vault=vault, source_relay=relay,
            )
        finally:
            vault.close()

        self.assertEqual(inventory.chunk_count, len(chunks_before))
        self.assertGreater(inventory.ciphertext_bytes_total, 0)
        # Preflight is read-only — no relay state changed.
        self.assertEqual(set(relay.chunks), chunks_before)

    def test_has_edited_shards_flips_when_shard_revision_gt_1(self) -> None:
        """Preflight surfaces the §5.M2 idempotency-gap warning by
        flagging any folder whose shard_revision is > 1."""
        local = self.tmpdir / "doc.txt"
        local.write_bytes(b"v1 content")
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
            v1 = upload_file(
                vault=vault, relay=relay, manifest=manifest, local_path=local,
                remote_folder_id=DOCS_ID, remote_path="doc.txt",
                author_device_id=AUTHOR,
                created_at="2026-04-01T10:00:00.000Z",
            )
            local.write_bytes(b"v2 content -- different bytes")
            upload_file(
                vault=vault, relay=relay,
                manifest=assemble_unified_manifest(
                    v1.root, {v1.remote_folder_id: v1.shard},
                ),
                local_path=local,
                remote_folder_id=DOCS_ID, remote_path="doc.txt",
                author_device_id=AUTHOR,
                created_at="2026-05-01T10:00:00.000Z",
            )

            inventory = migration_preflight(
                vault=vault, source_relay=relay,
            )
        finally:
            vault.close()

        self.assertTrue(inventory.has_edited_shards)
        self.assertGreaterEqual(inventory.shard_revisions[DOCS_ID], 2)


if __name__ == "__main__":
    unittest.main()
