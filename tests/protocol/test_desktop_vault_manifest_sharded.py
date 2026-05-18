"""Phase C unit tests for the shard-aware manifest model.

Spec: ``docs/protocol/vault-v1-formats.md`` §10.A / §10.B and
``temp/finished-plans/vault-manifest-sharding.md`` Phase C.

Covers the in-memory dict builders + canonicalizers + entry-level
helpers Phase C introduced (``make_root_manifest`` / ``make_folder_shard``
+ shard-aware variants of every entry helper) plus the
``assemble_unified_manifest`` soft-migration surface that lets older
callers consume the new shape until Phase E / Phase F migrates them.

Round-trip cases use the existing crypto primitives
(``build_root_envelope`` / ``build_shard_envelope`` etc., Phase A) so
the byte-shape contract is exercised end-to-end without leaning on
runtime wire calls (those land in Phase D).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import unittest

import nacl.exceptions

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.crypto import (  # noqa: E402
    aead_decrypt,
    aead_encrypt,
    build_root_aad,
    build_root_envelope,
    build_shard_aad,
    build_shard_envelope,
    derive_subkey,
)
from src.vault.manifest import (  # noqa: E402
    DEFAULT_RETENTION_POLICY,
    ManifestRevisionInvariantError,
    add_or_append_file_version_in_shard,
    assemble_unified_manifest,
    assert_publishable_root_revision,
    assert_publishable_shard_revision,
    bump_root_revision,
    bump_shard_revision,
    canonical_root_json,
    canonical_shard_json,
    find_file_entry_in_shard,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
    merge_shard_with_remote_head,
    normalize_root_manifest_plaintext,
    normalize_shard_plaintext,
    restore_file_entry_in_shard,
    tombstone_file_entry_in_shard,
    tombstone_files_under_in_shard,
)


VAULT_ID = "ABCD2345WXYZ"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
FOLDER_A = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
FOLDER_B = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
MASTER_KEY = bytes.fromhex("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20")


def _file_version(idx: int = 1, content_bytes: int = 12345) -> dict:
    """Build a minimal but valid file-version dict for entry helpers."""
    # Use only chars from the base32-lower alphabet [a-z2-7]; map 1→2, etc.
    suffix_char = "abcdefghijklmnop"[max(0, min(15, idx - 1))]
    return {
        "version_id": f"fv_v1_{suffix_char * 24}",
        "created_at": "2026-05-03T09:55:00.000Z",
        "modified_at": "2026-05-03T09:55:00.000Z",
        "logical_size": content_bytes,
        "ciphertext_size": content_bytes + 48,
        "content_fingerprint": "Zm9v",
        "chunks": [
            {
                "chunk_id": "ch_v1_dddddddddddddddddddddddd",
                "index": 0,
                "plaintext_size": content_bytes,
                "ciphertext_size": content_bytes + 24,
            }
        ],
        "author_device_id": AUTHOR,
    }


class RootManifestBuilderTests(unittest.TestCase):
    def test_make_root_manifest_normalizes_defaults(self) -> None:
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=1,
            parent_root_revision=0,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
        )
        self.assertEqual(root["schema"], "dc-vault-root-v1")
        self.assertEqual(root["root_revision"], 1)
        self.assertEqual(root["parent_root_revision"], 0)
        self.assertEqual(root["remote_folders"], [])
        self.assertEqual(root["retention_policy"], DEFAULT_RETENTION_POLICY)
        self.assertEqual(root["manifest_format_version"], 1)

    def test_make_root_manifest_with_folder_pointer(self) -> None:
        pointer = make_root_folder_pointer(
            remote_folder_id=FOLDER_A,
            display_name_enc="Documents",
            created_at="2026-05-03T10:00:00.000Z",
            created_by_device_id=AUTHOR,
            shard_revision=5,
            shard_hash="abcd" * 16,
        )
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=2,
            parent_root_revision=1,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[pointer],
        )
        self.assertEqual(len(root["remote_folders"]), 1)
        rf = root["remote_folders"][0]
        self.assertEqual(rf["remote_folder_id"], FOLDER_A)
        self.assertEqual(rf["shard_revision"], 5)
        self.assertEqual(rf["shard_hash"], "abcd" * 16)
        self.assertNotIn("entries", rf, "root pointer must not carry entries[]")

    def test_normalize_root_drops_legacy_entries_field(self) -> None:
        # A caller migrating from the legacy shape might still hand in a
        # folder dict that carries ``entries``. The root normalizer
        # silently drops it (entries live in shards now).
        legacy = make_root_folder_pointer(
            remote_folder_id=FOLDER_A,
            display_name_enc="Docs",
            created_at="2026-05-03T10:00:00.000Z",
            created_by_device_id=AUTHOR,
        )
        legacy["entries"] = [{"entry_id": "fe_v1_should_not_survive"}]
        normalized = normalize_root_manifest_plaintext({
            "vault_id": VAULT_ID,
            "root_revision": 1,
            "parent_root_revision": 0,
            "created_at": "2026-05-03T10:00:00.000Z",
            "author_device_id": AUTHOR,
            "remote_folders": [legacy],
        })
        self.assertNotIn("entries", normalized["remote_folders"][0])

    def test_canonical_root_json_is_sorted_compact_utf8(self) -> None:
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=1,
            parent_root_revision=0,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
        )
        raw = canonical_root_json(root)
        # Round-tripping the canonical bytes through json.loads + canon
        # must be a fixed point.
        again = canonical_root_json(json.loads(raw.decode("utf-8")))
        self.assertEqual(raw, again)

    def test_bump_root_revision_sets_pair_byte_exactly(self) -> None:
        parent = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=7,
            parent_root_revision=6,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
        )
        child = {"vault_id": parent["vault_id"]}
        bump_root_revision(child, from_parent=parent)
        self.assertEqual(child["root_revision"], 8)
        self.assertEqual(child["parent_root_revision"], 7)

    def test_assert_publishable_root_revision_rejects_unbumped_pair(self) -> None:
        with self.assertRaises(ManifestRevisionInvariantError):
            assert_publishable_root_revision({"root_revision": 5, "parent_root_revision": 3})


class FolderShardBuilderTests(unittest.TestCase):
    def test_make_folder_shard_normalizes_empty(self) -> None:
        shard = make_folder_shard(
            vault_id=VAULT_ID,
            remote_folder_id=FOLDER_A,
            shard_revision=1,
            parent_shard_revision=0,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
        )
        self.assertEqual(shard["schema"], "dc-vault-shard-v1")
        self.assertEqual(shard["remote_folder_id"], FOLDER_A)
        self.assertEqual(shard["entries"], [])

    def test_make_folder_shard_rejects_bad_folder_id(self) -> None:
        with self.assertRaises(ValueError):
            make_folder_shard(
                vault_id=VAULT_ID,
                remote_folder_id="not-an-rf-id",
                shard_revision=1,
                parent_shard_revision=0,
                created_at="2026-05-03T10:00:00.000Z",
                author_device_id=AUTHOR,
            )

    def test_canonical_shard_json_is_sorted_compact_utf8(self) -> None:
        shard = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=3, parent_shard_revision=2,
            created_at="2026-05-03T11:00:00.000Z",
            author_device_id=AUTHOR,
        )
        raw = canonical_shard_json(shard)
        again = canonical_shard_json(json.loads(raw.decode("utf-8")))
        self.assertEqual(raw, again)

    def test_bump_shard_revision_sets_pair(self) -> None:
        parent = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=4, parent_shard_revision=3,
            created_at="2026-05-03T11:00:00.000Z",
            author_device_id=AUTHOR,
        )
        child = {"remote_folder_id": FOLDER_A}
        bump_shard_revision(child, from_parent=parent)
        self.assertEqual(child["shard_revision"], 5)
        self.assertEqual(child["parent_shard_revision"], 4)

    def test_assert_publishable_shard_revision_rejects_unbumped_pair(self) -> None:
        with self.assertRaises(ManifestRevisionInvariantError):
            assert_publishable_shard_revision(
                {"shard_revision": 7, "parent_shard_revision": 5},
            )


class ShardAwareEntryHelperTests(unittest.TestCase):
    def _empty_shard(self) -> dict:
        return make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=1, parent_shard_revision=0,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
        )

    def test_add_or_append_creates_new_entry(self) -> None:
        shard = self._empty_shard()
        out = add_or_append_file_version_in_shard(
            shard,
            path="report.pdf",
            version=_file_version(1),
        )
        self.assertEqual(len(out["entries"]), 1)
        entry = out["entries"][0]
        self.assertEqual(entry["path"], "report.pdf")
        self.assertEqual(entry["latest_version_id"], _file_version(1)["version_id"])
        self.assertFalse(entry["deleted"])

    def test_add_or_append_idempotent_on_same_version_id(self) -> None:
        # F-D05: re-publishing the same version_id is a no-op.
        shard = self._empty_shard()
        v = _file_version(1)
        shard1 = add_or_append_file_version_in_shard(shard, path="report.pdf", version=v)
        shard2 = add_or_append_file_version_in_shard(shard1, path="report.pdf", version=v)
        self.assertEqual(canonical_shard_json(shard1), canonical_shard_json(shard2))

    def test_find_file_entry_in_shard_matches_by_nfc_path(self) -> None:
        shard = self._empty_shard()
        shard = add_or_append_file_version_in_shard(
            shard, path="Café/menu.pdf", version=_file_version(1),
        )
        # Different Unicode normalization on lookup path still matches.
        found = find_file_entry_in_shard(shard, path="Café/menu.pdf")
        self.assertIsNotNone(found)
        self.assertEqual(found["path"], "Café/menu.pdf")

    def test_tombstone_file_entry_marks_deleted_with_recoverable_until(self) -> None:
        shard = self._empty_shard()
        shard = add_or_append_file_version_in_shard(
            shard, path="report.pdf", version=_file_version(1),
        )
        shard = tombstone_file_entry_in_shard(
            shard,
            path="report.pdf",
            deleted_at="2026-05-03T11:00:00.000Z",
            author_device_id=AUTHOR,
            folder_retention_policy={"keep_deleted_days": 30, "keep_versions": 10},
        )
        entry = shard["entries"][0]
        self.assertTrue(entry["deleted"])
        self.assertEqual(entry["deleted_at"], "2026-05-03T11:00:00.000Z")
        self.assertIn("recoverable_until", entry)

    def test_restore_file_entry_clears_tombstone(self) -> None:
        shard = self._empty_shard()
        shard = add_or_append_file_version_in_shard(
            shard, path="report.pdf", version=_file_version(1),
        )
        shard = tombstone_file_entry_in_shard(
            shard, path="report.pdf",
            deleted_at="2026-05-03T11:00:00.000Z",
            author_device_id=AUTHOR,
        )
        shard = restore_file_entry_in_shard(
            shard, path="report.pdf",
            new_version=_file_version(2),
            author_device_id=AUTHOR,
        )
        entry = shard["entries"][0]
        self.assertFalse(entry["deleted"])
        self.assertNotIn("deleted_at", entry)
        self.assertEqual(entry["restored_by_device_id"], AUTHOR)
        self.assertEqual(entry["latest_version_id"], _file_version(2)["version_id"])

    def test_tombstone_files_under_walks_subtree_only(self) -> None:
        shard = self._empty_shard()
        # Distinct, regex-valid version_ids per file.
        version_ids = [
            "fv_v1_" + c * 24 for c in "abc"
        ]
        for idx, p in enumerate(("a/x.txt", "a/sub/y.txt", "b/z.txt")):
            shard = add_or_append_file_version_in_shard(
                shard, path=p, version={**_file_version(1), "version_id": version_ids[idx]},
            )
        shard, tombed = tombstone_files_under_in_shard(
            shard,
            path_prefix="a",
            deleted_at="2026-05-03T11:00:00.000Z",
            author_device_id=AUTHOR,
        )
        self.assertEqual(set(tombed), {"a/x.txt", "a/sub/y.txt"})
        b_entry = next(e for e in shard["entries"] if e["path"] == "b/z.txt")
        self.assertFalse(b_entry["deleted"])

    def test_merge_shard_with_remote_head_rebuilds_on_server(self) -> None:
        # Parent shard: empty. Server head: revision 5 with one file.
        # Local attempt: revision 5 + 1 with a different file. Merge
        # should rebase to revision 6 with both files present.
        parent = self._empty_shard()
        server = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=5, parent_shard_revision=4,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            entries=[
                {
                    "entry_id": "fe_v1_serverexists0000000000zz",
                    "type": "file",
                    "path": "server-only.txt",
                    "deleted": False,
                    "latest_version_id": _file_version(1)["version_id"],
                    "versions": [_file_version(1)],
                },
            ],
        )
        local_attempt = add_or_append_file_version_in_shard(
            parent, path="local-only.txt", version=_file_version(2),
        )
        local_attempt["shard_revision"] = 5
        local_attempt["parent_shard_revision"] = 4

        merged = merge_shard_with_remote_head(
            parent=parent,
            local_attempt=local_attempt,
            server_head=server,
            author_device_id=AUTHOR,
            now="2026-05-03T11:00:00.000Z",
        )
        self.assertEqual(merged["shard_revision"], 6)
        self.assertEqual(merged["parent_shard_revision"], 5)
        paths = sorted(e["path"] for e in merged["entries"])
        self.assertEqual(paths, ["local-only.txt", "server-only.txt"])


class CryptoRoundTripTests(unittest.TestCase):
    """Verify the new helpers round-trip through the build/encrypt path."""

    def test_root_envelope_roundtrips_through_builders(self) -> None:
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=1, parent_root_revision=0,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_root_folder_pointer(
                    remote_folder_id=FOLDER_A,
                    display_name_enc="Documents",
                    created_at="2026-05-03T10:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    shard_revision=3, shard_hash="ab" * 32,
                ),
            ],
        )
        plain = canonical_root_json(root)
        key = derive_subkey("dc-vault-v1/root", MASTER_KEY)
        nonce = secrets.token_bytes(24)
        aad = build_root_aad(VAULT_ID, 1, 0, AUTHOR)
        ct = aead_encrypt(plain, key, nonce, aad)
        envelope = build_root_envelope(
            vault_id=VAULT_ID, root_revision=1, parent_root_revision=0,
            author_device_id=AUTHOR, nonce=nonce, aead_ciphertext_and_tag=ct,
        )
        # Round-trip through decrypt → canonical comparison.
        recovered = aead_decrypt(ct, key, nonce, aad)
        self.assertEqual(recovered, plain)
        # Envelope header byte 0 is version 0x01.
        self.assertEqual(envelope[0], 0x01)

    def test_root_wrong_aad_fails_closed(self) -> None:
        root = make_root_manifest(
            vault_id=VAULT_ID, root_revision=1, parent_root_revision=0,
            created_at="2026-05-03T10:00:00.000Z", author_device_id=AUTHOR,
        )
        plain = canonical_root_json(root)
        key = derive_subkey("dc-vault-v1/root", MASTER_KEY)
        nonce = secrets.token_bytes(24)
        aad_right = build_root_aad(VAULT_ID, 1, 0, AUTHOR)
        ct = aead_encrypt(plain, key, nonce, aad_right)
        # Decrypt with a wrong root_revision in the AAD.
        aad_wrong = build_root_aad(VAULT_ID, 999, 0, AUTHOR)
        with self.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(ct, key, nonce, aad_wrong)

    def test_shard_envelope_roundtrips_with_entries(self) -> None:
        shard = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=1, parent_shard_revision=0,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            entries=[
                {
                    "entry_id": "fe_v1_oneentryexample00000000zz",
                    "type": "file",
                    "path": "report.pdf",
                    "deleted": False,
                    "latest_version_id": _file_version(1)["version_id"],
                    "versions": [_file_version(1)],
                },
            ],
        )
        plain = canonical_shard_json(shard)
        key = derive_subkey("dc-vault-v1/shard", MASTER_KEY)
        nonce = secrets.token_bytes(24)
        aad = build_shard_aad(VAULT_ID, FOLDER_A, 1, 0, AUTHOR)
        ct = aead_encrypt(plain, key, nonce, aad)
        envelope = build_shard_envelope(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=1, parent_shard_revision=0,
            author_device_id=AUTHOR, nonce=nonce, aead_ciphertext_and_tag=ct,
        )
        recovered = aead_decrypt(ct, key, nonce, aad)
        self.assertEqual(recovered, plain)
        self.assertEqual(envelope[0], 0x01)

    def test_shard_cross_folder_aad_fails_closed(self) -> None:
        shard = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=1, parent_shard_revision=0,
            created_at="2026-05-03T10:00:00.000Z", author_device_id=AUTHOR,
        )
        plain = canonical_shard_json(shard)
        key = derive_subkey("dc-vault-v1/shard", MASTER_KEY)
        nonce = secrets.token_bytes(24)
        aad_a = build_shard_aad(VAULT_ID, FOLDER_A, 1, 0, AUTHOR)
        ct = aead_encrypt(plain, key, nonce, aad_a)
        # Replay folder A's envelope under folder B's AAD — must fail.
        aad_b = build_shard_aad(VAULT_ID, FOLDER_B, 1, 0, AUTHOR)
        with self.assertRaises(nacl.exceptions.CryptoError):
            aead_decrypt(ct, key, nonce, aad_b)


class AssembleUnifiedManifestTests(unittest.TestCase):
    def test_assemble_with_one_folder_matches_legacy_shape(self) -> None:
        v = _file_version(1)
        pointer = make_root_folder_pointer(
            remote_folder_id=FOLDER_A,
            display_name_enc="Documents",
            created_at="2026-05-03T10:00:00.000Z",
            created_by_device_id=AUTHOR,
            shard_revision=3, shard_hash="ab" * 32,
        )
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=12, parent_root_revision=11,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[pointer],
        )
        shard = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=3, parent_shard_revision=2,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            entries=[
                {
                    "entry_id": "fe_v1_assembleexample0000000zz",
                    "type": "file",
                    "path": "report.pdf",
                    "deleted": False,
                    "latest_version_id": v["version_id"],
                    "versions": [v],
                },
            ],
        )

        unified = assemble_unified_manifest(root, {FOLDER_A: shard})

        # The unified shape matches the pre-sharding ``Vault.fetch_manifest``
        # output: schema renamed back to the legacy value, revision pair
        # renamed (no `root_*` prefix), one ``remote_folders[]`` array
        # with its own ``entries[]`` array.
        self.assertEqual(unified["schema"], "dc-vault-manifest-v1")
        self.assertEqual(unified["revision"], 12)
        self.assertEqual(unified["parent_revision"], 11)
        self.assertEqual(len(unified["remote_folders"]), 1)
        rf = unified["remote_folders"][0]
        self.assertEqual(rf["remote_folder_id"], FOLDER_A)
        self.assertEqual(rf["display_name_enc"], "Documents")
        self.assertEqual(len(rf["entries"]), 1)
        self.assertEqual(rf["entries"][0]["path"], "report.pdf")
        # Pointer-only metadata (shard_revision / shard_hash) doesn't
        # bleed into the unified shape — that's relay-side bookkeeping.
        self.assertNotIn("shard_revision", rf)
        self.assertNotIn("shard_hash", rf)

    def test_assemble_with_missing_shard_emits_empty_entries(self) -> None:
        pointer = make_root_folder_pointer(
            remote_folder_id=FOLDER_A,
            display_name_enc="Photos",
            created_at="2026-05-03T10:00:00.000Z",
            created_by_device_id=AUTHOR,
        )
        root = make_root_manifest(
            vault_id=VAULT_ID, root_revision=1, parent_root_revision=0,
            created_at="2026-05-03T10:00:00.000Z", author_device_id=AUTHOR,
            remote_folders=[pointer],
        )
        unified = assemble_unified_manifest(root, {})  # no shards supplied
        rf = unified["remote_folders"][0]
        self.assertEqual(rf["entries"], [])

    def test_assemble_round_trips_through_canonical_legacy_shape(self) -> None:
        # Build the pieces, assemble, then run the result through
        # canonical_manifest_json — the output should equal the legacy
        # canonical_manifest_json applied to the same manifest hand-built
        # in the pre-sharding shape.
        from src.vault.manifest import canonical_manifest_json  # legacy
        v = _file_version(1)
        pointer = make_root_folder_pointer(
            remote_folder_id=FOLDER_A,
            display_name_enc="Documents",
            created_at="2026-05-03T10:00:00.000Z",
            created_by_device_id=AUTHOR,
            shard_revision=3, shard_hash="ab" * 32,
        )
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=12, parent_root_revision=11,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[pointer],
        )
        shard = make_folder_shard(
            vault_id=VAULT_ID, remote_folder_id=FOLDER_A,
            shard_revision=3, parent_shard_revision=2,
            created_at="2026-05-03T10:00:00.000Z",
            author_device_id=AUTHOR,
            entries=[
                {
                    "entry_id": "fe_v1_assembleexample0000000zz",
                    "type": "file",
                    "path": "report.pdf",
                    "deleted": False,
                    "latest_version_id": v["version_id"],
                    "versions": [v],
                },
            ],
        )
        unified = assemble_unified_manifest(root, {FOLDER_A: shard})
        # Canonical JSON applied to the unified dict shouldn't raise.
        raw = canonical_manifest_json(unified)
        again = canonical_manifest_json(json.loads(raw.decode("utf-8")))
        self.assertEqual(raw, again)


if __name__ == "__main__":
    unittest.main()
