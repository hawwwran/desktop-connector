"""Phase F acceptance test — lazy shard load.

Per ``docs/plans/vault-manifest-sharding.md`` Phase F:

  > **Lazy shard load**: opening one folder fetches only that folder's
  > shard, not all of them. Counted via a probe relay.

This test asserts the bandwidth invariant directly at the Vault API
layer: ``Vault.fetch_folder_shard(folder_id)`` ships only that
folder's shard envelope; sibling shards stay un-fetched.

The browser-model refactor that wires this into the GUI is deferred
to Phase H's cleanup — the wire surface already provides the
mechanism, and a probe-counted test pins the contract. A GUI test
would add no information the wire test doesn't already cover
because the Vault wire methods are the choke-point.
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


FOLDER_C = "rf_v1_cccccccccccccccccccccccc"


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


def _pointer(rf: str, *, shard_revision: int = 1) -> dict:
    return make_root_folder_pointer(
        remote_folder_id=rf, display_name_enc="X",
        created_at="2026-05-04T10:00:00.000Z",
        created_by_device_id=AUTHOR,
        shard_revision=shard_revision,
        shard_hash="placeholder",
    )


class LazyShardLoadTests(unittest.TestCase):
    def test_open_one_folder_fetches_only_its_shard(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)

            # Bootstrap three folders so each has a shard the relay
            # can serve when asked.
            for idx, rf in enumerate([FOLDER_A, FOLDER_B, FOLDER_C], start=1):
                shard = make_folder_shard(
                    vault_id=VAULT_ID, remote_folder_id=rf,
                    shard_revision=1, parent_shard_revision=0,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                )
                root = make_root_manifest(
                    vault_id=VAULT_ID,
                    root_revision=1 + idx, parent_root_revision=idx,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                    remote_folders=[
                        _pointer(prev_rf, shard_revision=1)
                        for prev_rf in [FOLDER_A, FOLDER_B, FOLDER_C][:idx]
                    ],
                )
                vault.publish_shard_with_root(relay, rf, shard, root)

            # Reset the probe counters so the assertion below ignores
            # the bootstrap traffic.
            relay.shard_gets = {}

            # Now simulate "user opens folder A". The shard-aware path
            # fetches only folder A's shard.
            shard_a = vault.fetch_folder_shard(relay, FOLDER_A)
            self.assertEqual(shard_a["remote_folder_id"], FOLDER_A)

            # The probe: only folder A's shard hit the wire.
            self.assertEqual(relay.shard_gets.get(FOLDER_A), 1)
            self.assertNotIn(FOLDER_B, relay.shard_gets)
            self.assertNotIn(FOLDER_C, relay.shard_gets)

            # Open folder B next; folder C still untouched.
            vault.fetch_folder_shard(relay, FOLDER_B)
            self.assertEqual(relay.shard_gets.get(FOLDER_A), 1)
            self.assertEqual(relay.shard_gets.get(FOLDER_B), 1)
            self.assertNotIn(FOLDER_C, relay.shard_gets)
        finally:
            vault.close()

    def test_unified_manifest_compat_path_fetches_every_shard(self) -> None:
        """Contrast — the legacy compat path (``fetch_unified_manifest``)
        deliberately fetches every shard so callers that haven't been
        ported still see the vault-wide view. This pins the
        bandwidth trade-off: the new path is per-folder, the compat
        path is vault-wide.
        """
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            for idx, rf in enumerate([FOLDER_A, FOLDER_B, FOLDER_C], start=1):
                shard = make_folder_shard(
                    vault_id=VAULT_ID, remote_folder_id=rf,
                    shard_revision=1, parent_shard_revision=0,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                )
                root = make_root_manifest(
                    vault_id=VAULT_ID,
                    root_revision=1 + idx, parent_root_revision=idx,
                    created_at="2026-05-04T10:00:00.000Z",
                    author_device_id=AUTHOR,
                    remote_folders=[
                        _pointer(prev_rf, shard_revision=1)
                        for prev_rf in [FOLDER_A, FOLDER_B, FOLDER_C][:idx]
                    ],
                )
                vault.publish_shard_with_root(relay, rf, shard, root)

            relay.shard_gets = {}
            relay.root_gets = 0
            vault.fetch_unified_manifest(relay)
            self.assertEqual(relay.root_gets, 1)
            # Every folder's shard was fetched — the compat path is
            # vault-wide by design.
            self.assertEqual(set(relay.shard_gets), {FOLDER_A, FOLDER_B, FOLDER_C})
        finally:
            vault.close()


if __name__ == "__main__":
    unittest.main()
