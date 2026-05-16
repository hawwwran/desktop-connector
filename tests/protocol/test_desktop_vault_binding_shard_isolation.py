"""Phase E acceptance tests for shard-aware sync.

These tests demonstrate the two **shard-isolation** invariants that
manifest sharding was designed to deliver:

1. **Shard isolation** — two bindings to different folders of the
   same vault can sync concurrently without one binding's publishes
   touching the other's shard. The probe counts per-shard PUTs.

2. **Cross-shard idempotence** — a CAS conflict on one folder's
   shard doesn't invalidate ops queued for another folder's shard.
   The probe drives a conflict-on-A run while folder B publishes
   cleanly.

The tests exercise the Phase D ``Vault.publish_shard_with_root`` path
directly — the sync engine's ``_publish_batch_with_cas_retry`` still
publishes the unified manifest (Phase H removes that). The
acceptance bar is that the shard-aware **wire surface** delivers
the isolation properties; the engine migration is mechanical and
ports later.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)
from src.vault.relay_errors import VaultCASConflictError  # noqa: E402

# Reuse the FakeShardedRelay from Phase D's wire tests.
from test_desktop_vault_shard_wire import (  # noqa: E402
    AUTHOR,
    FOLDER_A,
    FOLDER_B,
    FakeShardedRelay,
    MASTER_KEY,
    VAULT_ACCESS_SECRET,
    VAULT_ID,
    _seed_genesis,
)


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


def _empty_pointer(rf_id: str, *, shard_revision: int = 0) -> dict:
    return make_root_folder_pointer(
        remote_folder_id=rf_id,
        display_name_enc="X",
        created_at="2026-05-04T10:00:00.000Z",
        created_by_device_id=AUTHOR,
        shard_revision=shard_revision,
        shard_hash="placeholder-overwritten",
    )


class ShardIsolationTests(unittest.TestCase):
    """Acceptance criterion: a publish to folder A's shard does not
    touch folder B's shard. Probe via per-folder PUT counters.
    """

    def test_two_folder_bindings_isolated_publishes(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            # Bootstrap both folders into the vault.
            for idx, rf in enumerate([FOLDER_A, FOLDER_B], start=1):
                shard = make_folder_shard(
                    vault_id=VAULT_ID, remote_folder_id=rf,
                    shard_revision=1, parent_shard_revision=0,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                )
                root = make_root_manifest(
                    vault_id=VAULT_ID,
                    root_revision=1 + idx,
                    parent_root_revision=idx,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                    remote_folders=[_empty_pointer(rf, shard_revision=1)],
                )
                vault.publish_shard_with_root(relay, rf, shard, root)

            shard_with_root_count_before = relay.shard_with_root_puts
            shard_A_count_before = relay.shards[FOLDER_A]["revision"]
            shard_B_count_before = relay.shards[FOLDER_B]["revision"]

            # Now: simulate folder A's binding publishing 5 batched
            # mutations. Each publish bumps only folder A's shard +
            # the root.
            for k in range(5):
                shard_v = make_folder_shard(
                    vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                    shard_revision=2 + k, parent_shard_revision=1 + k,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                )
                root_v = make_root_manifest(
                    vault_id=VAULT_ID,
                    root_revision=4 + k, parent_root_revision=3 + k,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                    remote_folders=[
                        _empty_pointer(FOLDER_A, shard_revision=2 + k),
                        _empty_pointer(FOLDER_B, shard_revision=1),
                    ],
                )
                vault.publish_shard_with_root(relay, FOLDER_A, shard_v, root_v)

            # Folder A advanced 5 revs; folder B did not budge.
            self.assertEqual(relay.shards[FOLDER_A]["revision"], shard_A_count_before + 5)
            self.assertEqual(relay.shards[FOLDER_B]["revision"], shard_B_count_before)
            # All 5 publishes went through put_shard_with_root.
            self.assertEqual(
                relay.shard_with_root_puts - shard_with_root_count_before, 5,
            )
        finally:
            vault.close()

    def test_cross_shard_idempotence_after_conflict(self) -> None:
        """A CAS conflict on folder A's shard does not invalidate ops
        queued for folder B's shard — folder B's next publish lands
        cleanly on the un-touched shard chain.
        """
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)

            # Both folders bootstrapped.
            for idx, rf in enumerate([FOLDER_A, FOLDER_B], start=1):
                shard = make_folder_shard(
                    vault_id=VAULT_ID, remote_folder_id=rf,
                    shard_revision=1, parent_shard_revision=0,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                )
                root = make_root_manifest(
                    vault_id=VAULT_ID,
                    root_revision=1 + idx,
                    parent_root_revision=idx,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                    remote_folders=[_empty_pointer(rf, shard_revision=1)],
                )
                vault.publish_shard_with_root(relay, rf, shard, root)

            # Out-of-band concurrent writer advances folder A's shard so
            # the next folder-A publish is stale. Folder B is untouched.
            relay.shards[FOLDER_A]["revision"] = 99

            stale_shard_a = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                shard_revision=2, parent_shard_revision=1,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
            )
            root_v4 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=4, parent_root_revision=3,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
                remote_folders=[
                    _empty_pointer(FOLDER_A, shard_revision=2),
                    _empty_pointer(FOLDER_B, shard_revision=1),
                ],
            )
            with self.assertRaises(VaultCASConflictError):
                vault.publish_shard_with_root(relay, FOLDER_A, stale_shard_a, root_v4)

            # Folder B's shard is still at revision 1 — the failed
            # folder-A publish didn't touch it. Folder B can now publish
            # a fresh revision against its untouched chain.
            self.assertEqual(relay.shards[FOLDER_B]["revision"], 1)

            fresh_shard_b = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_B,
                shard_revision=2, parent_shard_revision=1,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
            )
            root_v4_b = make_root_manifest(
                vault_id=VAULT_ID, root_revision=4, parent_root_revision=3,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
                remote_folders=[
                    _empty_pointer(FOLDER_A, shard_revision=99),
                    _empty_pointer(FOLDER_B, shard_revision=2),
                ],
            )
            # Folder B's publish lands cleanly — its chain wasn't
            # affected by folder A's CAS conflict.
            vault.publish_shard_with_root(relay, FOLDER_B, fresh_shard_b, root_v4_b)
            self.assertEqual(relay.shards[FOLDER_B]["revision"], 2)
        finally:
            vault.close()


class CountShardEntriesTests(unittest.TestCase):
    def test_per_folder_count_returns_shard_local_total(self) -> None:
        from src.vault.binding.preflight import count_shard_entries
        shard = {
            "entries": [
                {
                    "type": "file", "path": "a.txt", "deleted": False,
                    "versions": [
                        {"version_id": "fv_v1_" + "a" * 24},
                        {"version_id": "fv_v1_" + "b" * 24},
                    ],
                },
                {
                    "type": "file", "path": "b.txt", "deleted": False,
                    "versions": [{"version_id": "fv_v1_" + "c" * 24}],
                },
            ],
        }
        self.assertEqual(count_shard_entries(shard), 3)

    def test_per_folder_count_ignores_non_file_entries(self) -> None:
        from src.vault.binding.preflight import count_shard_entries
        shard = {
            "entries": [
                {"type": "file", "versions": [{"version_id": "fv_v1_" + "a" * 24}]},
                {"type": "weird", "versions": [{"version_id": "fv_v1_" + "b" * 24}]},
                "not-a-dict",
            ],
        }
        # Files-only is the count_manifest_entries semantic too — we
        # mirror it exactly for the shard variant. Non-dict entries are
        # skipped silently; dict entries with any type are counted (so a
        # future ``type=folder`` entry inside a shard would count, but
        # v1 has no such shape).
        self.assertEqual(count_shard_entries(shard), 2)


if __name__ == "__main__":
    unittest.main()
