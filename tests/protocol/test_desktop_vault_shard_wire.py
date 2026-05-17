"""Phase D unit tests for the shard-aware Vault wire methods.

Covers the additive new surface on ``Vault``:
``fetch_root_manifest`` / ``publish_root_manifest`` /
``fetch_folder_shard`` / ``publish_folder_shard`` /
``publish_shard_with_root`` / ``fetch_unified_manifest``.

Uses a hand-rolled ``FakeShardedRelay`` test double that stores root +
per-folder shard envelopes as opaque bytes, exposes the new wire
methods, and emits CAS conflict shapes that mirror the relay's §A1
contract. Tests verify per-shard CAS isolation (folder A's conflict
doesn't poison folder B's queued publish) and the atomic
``publish_shard_with_root`` path's "either both succeed or both
return the appropriate conflict shape" guarantee.
"""

from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    bump_root_revision,
    bump_shard_revision,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)
from src.vault.relay_errors import VaultCASConflictError  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
FOLDER_A = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
FOLDER_B = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
MASTER_KEY = bytes.fromhex("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20")
VAULT_ACCESS_SECRET = "test-vault-access-secret"


class FakeShardedRelay:
    """In-memory fake exposing the Phase D wire surface.

    Stores opaque envelope bytes + revision counters; emits
    ``VaultCASConflictError`` on stale CAS like the real relay. Each
    shard has its own per-folder CAS chain, mirroring the server
    ``vault_folder_shard_heads`` row.

    Per-call counters (``root_puts``, ``shard_puts``,
    ``shard_with_root_puts``, ``root_gets``, ``shard_gets[folder]``)
    let probe tests assert that a per-folder edit really did ship
    only that folder's shard.
    """

    def __init__(self) -> None:
        self.root_envelope: bytes = b""
        self.root_revision: int = 0
        self.root_hash: str = ""
        self.shards: dict[str, dict] = {}  # folder_id → {envelope, revision, hash}
        self.root_puts = 0
        self.root_gets = 0
        self.shard_puts: dict[str, int] = {}
        self.shard_gets: dict[str, int] = {}
        self.shard_with_root_puts = 0

    # -- root ----------------------------------------------------------
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
        self,
        vault_id,
        vault_access_secret,
        *,
        expected_current_root_revision,
        new_root_revision,
        parent_root_revision,
        root_hash,
        root_ciphertext,
    ):
        self.root_puts += 1
        if int(expected_current_root_revision) != self.root_revision:
            raise VaultCASConflictError({
                "code": "vault_root_conflict",
                "message": "fake CAS conflict",
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
        return {"root_revision": new_root_revision, "root_hash": root_hash}

    # -- shard ---------------------------------------------------------
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
        self,
        vault_id,
        vault_access_secret,
        remote_folder_id,
        *,
        expected_current_shard_revision,
        new_shard_revision,
        parent_shard_revision,
        shard_hash,
        shard_ciphertext,
    ):
        self.shard_puts[remote_folder_id] = self.shard_puts.get(remote_folder_id, 0) + 1
        current = self.shards.get(remote_folder_id)
        current_rev = current["revision"] if current else 0
        if int(expected_current_shard_revision) != current_rev:
            raise VaultCASConflictError({
                "code": "vault_shard_conflict",
                "message": "fake CAS conflict",
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
        return {"shard_revision": new_shard_revision, "shard_hash": shard_hash}

    def put_shard_with_root(
        self,
        vault_id,
        vault_access_secret,
        remote_folder_id,
        *,
        shard,
        root,
    ):
        self.shard_with_root_puts += 1
        # Peek both — both must be fresh, otherwise emit the right
        # conflict kind.
        current = self.shards.get(remote_folder_id)
        current_shard_rev = current["revision"] if current else 0
        if int(shard["expected_current_shard_revision"]) != current_shard_rev:
            raise VaultCASConflictError({
                "code": "vault_shard_conflict",
                "message": "fake CAS conflict (atomic, shard side)",
                "details": {
                    "remote_folder_id": remote_folder_id,
                    "current_shard_revision": current_shard_rev,
                    "current_shard_hash": current["hash"] if current else "",
                    "current_shard_ciphertext":
                        base64.b64encode(current["envelope"] if current else b"").decode("ascii"),
                    "current_shard_size": len(current["envelope"]) if current else 0,
                },
            })
        if int(root["expected_current_root_revision"]) != self.root_revision:
            raise VaultCASConflictError({
                "code": "vault_root_conflict",
                "message": "fake CAS conflict (atomic, root side)",
                "details": {
                    "current_root_revision": self.root_revision,
                    "current_root_hash": self.root_hash,
                    "current_root_ciphertext":
                        base64.b64encode(self.root_envelope).decode("ascii"),
                    "current_root_size": len(self.root_envelope),
                },
            })
        # Commit both.
        self.shards[remote_folder_id] = {
            "envelope": bytes(shard["shard_ciphertext"]),
            "revision": int(shard["new_shard_revision"]),
            "hash": shard["shard_hash"],
        }
        self.root_revision = int(root["new_root_revision"])
        self.root_envelope = bytes(root["root_ciphertext"])
        self.root_hash = root["root_hash"]
        return {
            "shard_revision": shard["new_shard_revision"],
            "shard_hash": shard["shard_hash"],
            "root_revision": root["new_root_revision"],
            "root_hash": root["root_hash"],
        }


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


def _seed_genesis(vault: Vault, relay: FakeShardedRelay) -> dict:
    """Publish a genesis root (empty folder list, rev 1) through the
    Vault — the fake starts at root_revision=0, so this first publish
    flips to 1.
    """
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=1,
        parent_root_revision=0,
        created_at="2026-05-04T10:00:00.000Z",
        author_device_id=AUTHOR,
    )
    return vault.publish_root_manifest(relay, root)


class RootManifestWireTests(unittest.TestCase):
    def test_publish_then_fetch_round_trips(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            self.assertEqual(relay.root_revision, 1)
            self.assertEqual(relay.root_puts, 1)

            fetched = vault.fetch_root_manifest(relay)
            self.assertEqual(fetched["root_revision"], 1)
            self.assertEqual(fetched["remote_folders"], [])
            self.assertEqual(relay.root_gets, 1)
        finally:
            vault.close()

    def test_stale_publish_surfaces_cas_conflict(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            # Bump the relay's root out of band so the next publish is stale.
            relay.root_revision = 5
            stale_root = make_root_manifest(
                vault_id=VAULT_ID,
                root_revision=2,
                parent_root_revision=1,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
            )
            with self.assertRaises(VaultCASConflictError):
                vault.publish_root_manifest(relay, stale_root)
        finally:
            vault.close()


class FolderShardWireTests(unittest.TestCase):
    def test_publish_shard_with_root_commits_both(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)

            shard = make_folder_shard(
                vault_id=VAULT_ID,
                remote_folder_id=FOLDER_A,
                shard_revision=1,
                parent_shard_revision=0,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
            )
            root_v2 = make_root_manifest(
                vault_id=VAULT_ID,
                root_revision=2,
                parent_root_revision=1,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
                remote_folders=[make_root_folder_pointer(
                    remote_folder_id=FOLDER_A,
                    display_name_enc="Documents",
                    created_at="2026-05-04T10:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    shard_revision=1,
                    shard_hash="placeholder-overwritten-by-vault",
                )],
            )
            vault.publish_shard_with_root(relay, FOLDER_A, shard, root_v2)
            self.assertEqual(relay.shard_with_root_puts, 1)
            self.assertEqual(relay.root_revision, 2)
            self.assertIn(FOLDER_A, relay.shards)
            self.assertEqual(relay.shards[FOLDER_A]["revision"], 1)
        finally:
            vault.close()

    def test_shard_isolation_two_folders(self) -> None:
        """Acceptance criterion: a publish to folder A's shard does
        not touch folder B's shard — the relay counters confirm.
        """
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)

            # Folder A: empty genesis shard + root advance.
            shard_a = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                shard_revision=1, parent_shard_revision=0,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
            )
            root_v2 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=2, parent_root_revision=1,
                created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
                remote_folders=[make_root_folder_pointer(
                    remote_folder_id=FOLDER_A, display_name_enc="A",
                    created_at="2026-05-04T10:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    shard_revision=1, shard_hash="ph",
                )],
            )
            vault.publish_shard_with_root(relay, FOLDER_A, shard_a, root_v2)

            # Folder B: equivalent first publish; bumps root again.
            shard_b = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_B,
                shard_revision=1, parent_shard_revision=0,
                created_at="2026-05-04T10:01:00.000Z",
                author_device_id=AUTHOR,
            )
            root_v3 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=3, parent_root_revision=2,
                created_at="2026-05-04T10:01:00.000Z", author_device_id=AUTHOR,
                remote_folders=[
                    make_root_folder_pointer(
                        remote_folder_id=FOLDER_A, display_name_enc="A",
                        created_at="2026-05-04T10:00:00.000Z",
                        created_by_device_id=AUTHOR,
                        shard_revision=1, shard_hash="ph",
                    ),
                    make_root_folder_pointer(
                        remote_folder_id=FOLDER_B, display_name_enc="B",
                        created_at="2026-05-04T10:01:00.000Z",
                        created_by_device_id=AUTHOR,
                        shard_revision=1, shard_hash="ph",
                    ),
                ],
            )
            vault.publish_shard_with_root(relay, FOLDER_B, shard_b, root_v3)

            # Now a third edit on folder A — bumps only folder A's shard.
            shard_a_v2 = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                shard_revision=2, parent_shard_revision=1,
                created_at="2026-05-04T10:02:00.000Z",
                author_device_id=AUTHOR,
            )
            root_v4 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=4, parent_root_revision=3,
                created_at="2026-05-04T10:02:00.000Z", author_device_id=AUTHOR,
                remote_folders=[
                    make_root_folder_pointer(
                        remote_folder_id=FOLDER_A, display_name_enc="A",
                        created_at="2026-05-04T10:00:00.000Z",
                        created_by_device_id=AUTHOR,
                        shard_revision=2, shard_hash="ph",
                    ),
                    make_root_folder_pointer(
                        remote_folder_id=FOLDER_B, display_name_enc="B",
                        created_at="2026-05-04T10:01:00.000Z",
                        created_by_device_id=AUTHOR,
                        shard_revision=1, shard_hash="ph",
                    ),
                ],
            )
            vault.publish_shard_with_root(relay, FOLDER_A, shard_a_v2, root_v4)

            # Folder A advanced; folder B did not.
            self.assertEqual(relay.shards[FOLDER_A]["revision"], 2)
            self.assertEqual(relay.shards[FOLDER_B]["revision"], 1)
            # Three atomic publishes total — one per folder bump.
            self.assertEqual(relay.shard_with_root_puts, 3)
        finally:
            vault.close()

    def test_shard_only_conflict_when_shard_stale(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            # Advance shard out of band — caller's local view says
            # revision 0 but the relay is already at revision 5.
            relay.shards[FOLDER_A] = {
                "envelope": b"\x00" * 100,
                "revision": 5,
                "hash": "out-of-band",
            }

            shard = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                shard_revision=1, parent_shard_revision=0,
                created_at="2026-05-04T10:00:00.000Z",
                author_device_id=AUTHOR,
            )
            root_v2 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=2, parent_root_revision=1,
                created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
                remote_folders=[make_root_folder_pointer(
                    remote_folder_id=FOLDER_A, display_name_enc="A",
                    created_at="2026-05-04T10:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    shard_revision=1, shard_hash="ph",
                )],
            )
            with self.assertRaises(VaultCASConflictError):
                vault.publish_shard_with_root(relay, FOLDER_A, shard, root_v2)
            # Root stayed put because the shard CAS aborted the whole call.
            self.assertEqual(relay.root_revision, 1)
        finally:
            vault.close()


class UnifiedManifestCompatTests(unittest.TestCase):
    def test_fetch_unified_manifest_assembles_root_plus_shards(self) -> None:
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            # Add folder A with a shard via the atomic path.
            shard = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                shard_revision=1, parent_shard_revision=0,
                created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
            )
            root_v2 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=2, parent_root_revision=1,
                created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
                remote_folders=[make_root_folder_pointer(
                    remote_folder_id=FOLDER_A, display_name_enc="Documents",
                    created_at="2026-05-04T10:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    shard_revision=1, shard_hash="ph",
                )],
            )
            vault.publish_shard_with_root(relay, FOLDER_A, shard, root_v2)

            # Fetch the unified shape. The compat helper fetches root +
            # every listed shard and folds them into the legacy
            # vault-wide manifest dict.
            unified = vault.fetch_unified_manifest(relay)
            self.assertEqual(unified["schema"], "dc-vault-manifest-v1")
            self.assertEqual(unified["revision"], 2)
            self.assertEqual(len(unified["remote_folders"]), 1)
            self.assertEqual(unified["remote_folders"][0]["remote_folder_id"], FOLDER_A)
            self.assertEqual(unified["remote_folders"][0]["entries"], [])
            # Exactly one root GET + one shard GET hit the relay.
            self.assertEqual(relay.root_gets, 1)
            self.assertEqual(relay.shard_gets.get(FOLDER_A), 1)
        finally:
            vault.close()


class ShardHashChainTests(unittest.TestCase):
    """§10.C verifies that ``sha256(shard_envelope_bytes)`` matches the
    trusted root pointer's ``shard_hash`` before plaintext entries are
    consumed. These tests exercise the verification path at the wire
    layer (the only place the envelope bytes are still in scope).
    """

    def _publish_a(self, vault: Vault, relay: FakeShardedRelay) -> None:
        """Publish folder A with one empty shard via the atomic path so
        the relay carries a valid (shard, root) pair where the root's
        pointer hash matches the stored shard envelope.
        """
        shard = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=1, parent_shard_revision=0,
            created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
        )
        root_v2 = make_root_manifest(
            vault_id=VAULT_ID, root_revision=2, parent_root_revision=1,
            created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
            remote_folders=[make_root_folder_pointer(
                remote_folder_id=FOLDER_A, display_name_enc="A",
                created_at="2026-05-04T10:00:00.000Z",
                created_by_device_id=AUTHOR,
                shard_revision=1, shard_hash="ph-overwritten-by-vault",
            )],
        )
        vault.publish_shard_with_root(relay, FOLDER_A, shard, root_v2)

    def test_publish_shard_with_root_patches_pointer_hash(self) -> None:
        """Sanity: the atomic publish writes a root pointer whose
        ``shard_hash`` equals ``sha256(published_shard_envelope)``.
        """
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            self._publish_a(vault, relay)

            # The relay's stored root must agree with the relay's
            # stored shard on the §10.C anchor.
            root_back = vault.fetch_root_manifest(relay)
            pointer = next(
                p for p in root_back["remote_folders"]
                if p["remote_folder_id"] == FOLDER_A
            )
            import hashlib
            actual_envelope_hash = hashlib.sha256(
                relay.shards[FOLDER_A]["envelope"]
            ).hexdigest()
            self.assertEqual(pointer["shard_hash"], actual_envelope_hash)
        finally:
            vault.close()

    def test_fetch_unified_manifest_detects_shard_rollback(self) -> None:
        """The §10.C anchor catches a per-shard rollback: a relay that
        serves an older but still AEAD-valid shard envelope for a
        folder must surface as ``VaultShardHashMismatchError`` before
        the legacy entry shape is assembled.
        """
        from src.vault.relay_errors import VaultShardHashMismatchError

        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            self._publish_a(vault, relay)

            # Snapshot the current (revision 1) envelope.
            old_envelope = relay.shards[FOLDER_A]["envelope"]
            old_hash = relay.shards[FOLDER_A]["hash"]

            # Bump folder A to revision 2 by publishing a new shard
            # under the same vault key. This writes a fresh envelope
            # whose hash differs from ``old_hash`` and also patches
            # the root pointer.
            shard_v2 = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                shard_revision=2, parent_shard_revision=1,
                created_at="2026-05-04T10:00:30.000Z",
                author_device_id=AUTHOR,
            )
            root_v3 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=3, parent_root_revision=2,
                created_at="2026-05-04T10:00:30.000Z", author_device_id=AUTHOR,
                remote_folders=[make_root_folder_pointer(
                    remote_folder_id=FOLDER_A, display_name_enc="A",
                    created_at="2026-05-04T10:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    shard_revision=2, shard_hash="ph-overwritten-by-vault",
                )],
            )
            vault.publish_shard_with_root(relay, FOLDER_A, shard_v2, root_v3)
            new_hash = relay.shards[FOLDER_A]["hash"]
            self.assertNotEqual(old_hash, new_hash)

            # Simulate a relay-side rollback: keep the root at revision
            # 3 (so its pointer carries the new envelope's hash), but
            # serve the *old* envelope on the next get_shard call.
            relay.shards[FOLDER_A]["envelope"] = old_envelope
            relay.shards[FOLDER_A]["hash"] = old_hash

            with self.assertRaises(VaultShardHashMismatchError) as cm:
                vault.fetch_unified_manifest(relay)
            err = cm.exception
            self.assertEqual(err.remote_folder_id, FOLDER_A)
            self.assertEqual(err.expected_shard_hash, new_hash)
        finally:
            vault.close()

    def test_fetch_folder_shard_explicit_hash_check(self) -> None:
        """``fetch_folder_shard`` with ``expected_shard_hash`` raises
        on mismatch but succeeds when the hash matches.
        """
        from src.vault.relay_errors import VaultShardHashMismatchError

        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)
            self._publish_a(vault, relay)

            correct_hash = relay.shards[FOLDER_A]["hash"]
            # Happy path: the right hash decrypts successfully.
            shard = vault.fetch_folder_shard(
                relay, FOLDER_A, expected_shard_hash=correct_hash,
            )
            self.assertEqual(shard["remote_folder_id"], FOLDER_A)

            # Wrong hash raises before AEAD even runs.
            with self.assertRaises(VaultShardHashMismatchError):
                vault.fetch_folder_shard(
                    relay, FOLDER_A,
                    expected_shard_hash="00" * 32,
                )
        finally:
            vault.close()


class PublishShardWithRootGuardrailTests(unittest.TestCase):
    def test_raises_when_pointer_for_folder_missing(self) -> None:
        """A misuse: caller forgot to add a folder pointer to the root
        before calling ``publish_shard_with_root``. Without the
        pointer, there's nowhere to record the shard's hash; we fail
        fast instead of silently publishing an unanchored shard.
        """
        relay = FakeShardedRelay()
        vault = _vault()
        try:
            _seed_genesis(vault, relay)

            shard = make_folder_shard(
                vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
                shard_revision=1, parent_shard_revision=0,
                created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
            )
            # Root with NO folder pointer for FOLDER_A.
            root_v2 = make_root_manifest(
                vault_id=VAULT_ID, root_revision=2, parent_root_revision=1,
                created_at="2026-05-04T10:00:00.000Z", author_device_id=AUTHOR,
                remote_folders=[],
            )
            with self.assertRaises(ValueError) as cm:
                vault.publish_shard_with_root(relay, FOLDER_A, shard, root_v2)
            self.assertIn(FOLDER_A, str(cm.exception))
            # No state moved on the relay.
            self.assertEqual(relay.shard_with_root_puts, 0)
            self.assertEqual(relay.root_revision, 1)
            self.assertNotIn(FOLDER_A, relay.shards)
        finally:
            vault.close()


if __name__ == "__main__":
    unittest.main()
