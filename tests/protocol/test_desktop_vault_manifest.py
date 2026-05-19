"""T4.1 — Vault manifest plaintext schema helpers."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
    normalize_manifest_plaintext,
)


VAULT_ID = "ABCD2345WXYZ"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
PHOTOS_ID = "rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
MASTER_KEY = bytes.fromhex("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20")


class VaultManifestSchemaTests(unittest.TestCase):
    def test_missing_remote_folders_defaults_to_empty_list(self) -> None:
        manifest = {
            "schema": "dc-vault-manifest-v1",
            "vault_id": VAULT_ID,
            "revision": 1,
            "parent_revision": 0,
            "created_at": "2026-05-03T13:00:00.000Z",
            "author_device_id": AUTHOR,
            "manifest_format_version": 1,
            "operation_log_tail": [],
            "archived_op_segments": [],
        }

        normalized = normalize_manifest_plaintext(manifest)

        self.assertEqual(normalized["remote_folders"], [])

    def test_legacy_folder_names_normalize_to_t4_fields(self) -> None:
        normalized = normalize_manifest_plaintext({
            "schema": "dc-vault-manifest-v1",
            "vault_id": VAULT_ID,
            "revision": 2,
            "parent_revision": 1,
            "created_at": "2026-05-03T13:00:00.000Z",
            "author_device_id": AUTHOR,
            "manifest_format_version": 1,
            "remote_folders": [{
                "remote_folder_id": DOCS_ID,
                "name": "Documents",
                "retention": {"keep_deleted_days": 30, "keep_versions": 10},
            }],
            "operation_log_tail": [],
            "archived_op_segments": [],
        })

        folder = normalized["remote_folders"][0]
        self.assertNotIn("name", folder)
        self.assertNotIn("retention", folder)
        self.assertEqual(folder["display_name_enc"], "Documents")
        self.assertEqual(folder["created_at"], "2026-05-03T13:00:00.000Z")
        self.assertEqual(folder["created_by_device_id"], AUTHOR)
        self.assertEqual(folder["retention_policy"], {"keep_deleted_days": 30, "keep_versions": 10})
        self.assertEqual(folder["ignore_patterns"], [])
        self.assertEqual(folder["state"], "active")



class ManifestRevisionInvariantTests(unittest.TestCase):
    """F-Y21 — ``revision == parent_revision + 1`` enforcement.

    Manifest mutators (tombstone / restore / folder add/remove / merge)
    inherit revision/parent_revision through ``copy.deepcopy`` from the
    parent. Callers are responsible for bumping both fields. The
    invariant check at the publish boundary is the safety net that
    catches a drift before the relay fork-the-revision-history bug
    becomes visible.
    """

    def _manifest_at(self, *, revision: int, parent_revision: int) -> dict:
        root = make_root_manifest(
            vault_id=VAULT_ID,
            root_revision=revision,
            parent_root_revision=parent_revision,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[],
        )
        return assemble_unified_manifest(root, {})

    def test_assert_publishable_revision_accepts_valid_pairs(self) -> None:
        from src.vault.manifest import assert_publishable_revision

        # Genesis manifest.
        assert_publishable_revision(
            self._manifest_at(revision=1, parent_revision=0),
        )
        # Mid-history manifest.
        assert_publishable_revision(
            self._manifest_at(revision=42, parent_revision=41),
        )

    def test_assert_publishable_revision_rejects_inherited_pair(self) -> None:
        """The dangerous case: the candidate inherited the parent's
        ``revision`` instead of getting bumped, so it'd republish the
        same revision number — overwriting the parent in CAS terms.
        """
        from src.vault.manifest import (
            ManifestRevisionInvariantError,
            assert_publishable_revision,
        )

        with self.assertRaises(ManifestRevisionInvariantError):
            assert_publishable_revision(
                self._manifest_at(revision=5, parent_revision=5),
            )

    def test_assert_publishable_revision_rejects_unbumped_parent(self) -> None:
        """Caller forgot the parent_revision bump but advanced revision."""
        from src.vault.manifest import (
            ManifestRevisionInvariantError,
            assert_publishable_revision,
        )

        with self.assertRaises(ManifestRevisionInvariantError):
            assert_publishable_revision(
                self._manifest_at(revision=6, parent_revision=4),
            )

    def test_assert_publishable_revision_rejects_zero_revision(self) -> None:
        from src.vault.manifest import (
            ManifestRevisionInvariantError,
            assert_publishable_revision,
        )

        with self.assertRaises(ManifestRevisionInvariantError):
            assert_publishable_revision(
                self._manifest_at(revision=0, parent_revision=-1),
            )

    def test_assert_publishable_revision_rejects_non_integer_pair(self) -> None:
        """Catches a deepcopy of an encrypted-payload field that
        skipped normalization (e.g. ``revision`` arrived as a string).
        """
        from src.vault.manifest import (
            ManifestRevisionInvariantError,
            assert_publishable_revision,
        )

        bad = {
            "schema": "dc-vault-manifest-v1",
            "vault_id": VAULT_ID,
            "author_device_id": AUTHOR,
            "revision": "two",
            "parent_revision": "one",
            "created_at": "2026-05-04T12:00:00.000Z",
        }
        with self.assertRaises(ManifestRevisionInvariantError):
            assert_publishable_revision(bad)

    def test_bump_revision_sets_pair_byte_exactly(self) -> None:
        """``bump_revision`` is the named alternative to open-coding the
        bump in callers. Result must satisfy the publish invariant.
        """
        from src.vault.manifest import (
            assert_publishable_revision, bump_revision,
        )

        parent = self._manifest_at(revision=10, parent_revision=9)
        candidate = self._manifest_at(revision=10, parent_revision=9)
        bumped = bump_revision(candidate, from_parent=parent)
        self.assertIs(bumped, candidate)  # in-place by design
        self.assertEqual(bumped["revision"], 11)
        self.assertEqual(bumped["parent_revision"], 10)
        # Result satisfies the publish invariant.
        assert_publishable_revision(bumped)


DEVICE_A = AUTHOR
DEVICE_B = "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7"


def _make_root_with_two_folders(root_tail: list[dict] | None = None) -> dict:
    return make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=4,
        parent_root_revision=3,
        created_at="2026-05-19T12:00:00.000Z",
        author_device_id=DEVICE_A,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Docs",
                created_at="2026-05-19T12:00:00.000Z",
                created_by_device_id=DEVICE_A,
                shard_revision=2,
                shard_hash="x" * 64,
            ),
            make_root_folder_pointer(
                remote_folder_id=PHOTOS_ID,
                display_name_enc="Photos",
                created_at="2026-05-19T12:00:00.000Z",
                created_by_device_id=DEVICE_A,
                shard_revision=3,
                shard_hash="y" * 64,
            ),
        ],
        operation_log_tail=root_tail or [],
    )


def _make_shard(
    *, remote_folder_id: str, shard_revision: int, tail: list[dict],
) -> dict:
    return make_folder_shard(
        vault_id=VAULT_ID,
        remote_folder_id=remote_folder_id,
        shard_revision=shard_revision,
        parent_shard_revision=shard_revision - 1,
        created_at="2026-05-19T12:00:00.000Z",
        author_device_id=DEVICE_A,
        entries=[],
        operation_log_tail=tail,
    )


def _op_entry(*, ts: int, type: str, device_id: str, revision: int, path: str = "") -> dict:
    e = {"ts": ts, "type": type, "device_id": device_id, "revision": revision}
    if path:
        e["path"] = path
    return e


class AssembleUnifiedManifestOpLogMergeTests(unittest.TestCase):
    """D2 — ``assemble_unified_manifest`` must merge root + shard op-log
    tails into the unified view; tie-break sort keeps the timeline
    deterministic across re-fetches.

    Before this fix the unified ``operation_log_tail`` carried only the
    root's tail — every shard-scoped entry (uploads, deletes, restores,
    eviction) was invisible to the Activity tab.
    """

    def test_root_tail_alone_unchanged(self) -> None:
        root_tail = [
            _op_entry(ts=1_700_000_001, type="vault.grant.created",
                      device_id=DEVICE_A, revision=4),
        ]
        root = _make_root_with_two_folders(root_tail=root_tail)
        unified = assemble_unified_manifest(root, {})
        # Shards omitted from shards_by_id contribute no entries.
        self.assertEqual(unified["operation_log_tail"], root_tail)

    def test_shard_tails_merged_into_unified(self) -> None:
        docs_shard = _make_shard(
            remote_folder_id=DOCS_ID, shard_revision=2,
            tail=[
                _op_entry(ts=1_700_000_005, type="vault.upload.completed",
                          device_id=DEVICE_A, revision=2, path="Docs/a.txt"),
            ],
        )
        photos_shard = _make_shard(
            remote_folder_id=PHOTOS_ID, shard_revision=3,
            tail=[
                _op_entry(ts=1_700_000_004, type="vault.delete.completed",
                          device_id=DEVICE_A, revision=3, path="Photos/b.jpg"),
            ],
        )
        root = _make_root_with_two_folders(root_tail=[])
        unified = assemble_unified_manifest(
            root, {DOCS_ID: docs_shard, PHOTOS_ID: photos_shard},
        )
        types = [e["type"] for e in unified["operation_log_tail"]]
        # Sorted by ts ascending: ts=1_700_000_004 (delete) before ts=1_700_000_005 (upload).
        self.assertEqual(types, ["vault.delete.completed", "vault.upload.completed"])

    def test_tie_break_by_device_id_then_revision(self) -> None:
        # Two devices both author entries at the same ts; ties resolve
        # lexicographically by device_id, then by revision.
        docs_tail = [
            _op_entry(ts=1, type="vault.upload.completed",
                      device_id=DEVICE_B, revision=10, path="late"),
            _op_entry(ts=1, type="vault.upload.completed",
                      device_id=DEVICE_A, revision=20, path="A second"),
            _op_entry(ts=1, type="vault.upload.completed",
                      device_id=DEVICE_A, revision=10, path="A first"),
        ]
        docs_shard = _make_shard(
            remote_folder_id=DOCS_ID, shard_revision=2, tail=docs_tail,
        )
        root = _make_root_with_two_folders(root_tail=[])
        unified = assemble_unified_manifest(root, {DOCS_ID: docs_shard})
        paths = [e["path"] for e in unified["operation_log_tail"]]
        # DEVICE_A (a1b2...) < DEVICE_B (b2c3...) lexicographically;
        # within DEVICE_A revisions ascend 10 then 20.
        self.assertEqual(paths, ["A first", "A second", "late"])

    def test_missing_shard_contributes_no_entries(self) -> None:
        # Only Docs is in shards_by_id; Photos pointer exists in root but
        # the shard is absent (mid-fetch state). Photos contributes nothing.
        docs_shard = _make_shard(
            remote_folder_id=DOCS_ID, shard_revision=2,
            tail=[_op_entry(ts=42, type="vault.upload.completed",
                            device_id=DEVICE_A, revision=2)],
        )
        root = _make_root_with_two_folders(root_tail=[])
        unified = assemble_unified_manifest(root, {DOCS_ID: docs_shard})
        self.assertEqual(len(unified["operation_log_tail"]), 1)
        self.assertEqual(unified["operation_log_tail"][0]["revision"], 2)

    def test_root_and_shard_tails_merge_under_one_sort(self) -> None:
        # Vault-wide events live on root, file-level on shards. A real
        # vault has both simultaneously; the unified view must interleave
        # them by ts so the Activity tab renders chronologically.
        root_tail = [
            _op_entry(ts=10, type="vault.grant.created",
                      device_id=DEVICE_A, revision=4),
            _op_entry(ts=30, type="vault.vault.cleared",
                      device_id=DEVICE_A, revision=6),
        ]
        docs_shard = _make_shard(
            remote_folder_id=DOCS_ID, shard_revision=2,
            tail=[_op_entry(ts=20, type="vault.upload.completed",
                            device_id=DEVICE_A, revision=2)],
        )
        root = _make_root_with_two_folders(root_tail=root_tail)
        unified = assemble_unified_manifest(root, {DOCS_ID: docs_shard})
        ts_seq = [e["ts"] for e in unified["operation_log_tail"]]
        self.assertEqual(ts_seq, [10, 20, 30])

    def test_shard_tail_entries_are_copies(self) -> None:
        # Mutating the unified output must not bleed back into the shard
        # the caller passed in — matches the existing entries[] deepcopy.
        shard_entry = _op_entry(ts=1, type="vault.upload.completed",
                                device_id=DEVICE_A, revision=2)
        docs_shard = _make_shard(
            remote_folder_id=DOCS_ID, shard_revision=2,
            tail=[shard_entry],
        )
        root = _make_root_with_two_folders(root_tail=[])
        unified = assemble_unified_manifest(root, {DOCS_ID: docs_shard})
        unified["operation_log_tail"][0]["mutated"] = True
        self.assertNotIn("mutated", shard_entry)


if __name__ == "__main__":
    unittest.main()
