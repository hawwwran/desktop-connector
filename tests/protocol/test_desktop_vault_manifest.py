"""T4.1 — Vault manifest plaintext schema helpers."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
    normalize_manifest_plaintext,
    rename_remote_folder,
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

    def test_rename_remote_folder_only_changes_display_name(self) -> None:
        docs = make_remote_folder(
            remote_folder_id=DOCS_ID,
            display_name_enc="Documents",
            created_at="2026-05-03T13:00:00.000Z",
            created_by_device_id=AUTHOR,
            ignore_patterns=[".git/", "node_modules/", "*.tmp"],
        )
        photos = make_remote_folder(
            remote_folder_id=PHOTOS_ID,
            display_name_enc="Photos",
            created_at="2026-05-03T13:01:00.000Z",
            created_by_device_id=AUTHOR,
            retention_policy={"keep_deleted_days": 60, "keep_versions": 20},
            ignore_patterns=["*.tmp"],
        )
        base = make_manifest(
            vault_id=VAULT_ID,
            revision=4,
            parent_revision=3,
            created_at="2026-05-03T13:05:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[docs, photos],
        )

        renamed = rename_remote_folder(base, DOCS_ID, "Notes")

        # Only display_name_enc on the targeted folder changed.
        self.assertEqual(renamed["remote_folders"][0]["display_name_enc"], "Notes")
        # The untouched folder is byte-equal.
        self.assertEqual(renamed["remote_folders"][1], base["remote_folders"][1])
        # Sibling fields on the renamed folder are unchanged (per §D6).
        renamed_docs = renamed["remote_folders"][0]
        for field in (
            "remote_folder_id",
            "created_at",
            "created_by_device_id",
            "retention_policy",
            "ignore_patterns",
            "state",
        ):
            self.assertEqual(renamed_docs[field], docs[field], msg=f"field changed: {field}")
        # Source manifest wasn't mutated.
        self.assertEqual(base["remote_folders"][0]["display_name_enc"], "Documents")

    def test_rename_remote_folder_normalizes_nfc(self) -> None:
        # NFD-decomposed "Café" must persist as NFC.
        docs = make_remote_folder(
            remote_folder_id=DOCS_ID,
            display_name_enc="Documents",
            created_at="2026-05-03T13:00:00.000Z",
            created_by_device_id=AUTHOR,
        )
        base = make_manifest(
            vault_id=VAULT_ID, revision=2, parent_revision=1,
            created_at="2026-05-03T13:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[docs],
        )

        nfd = "Café"   # 'e' + combining acute
        nfc = "Café"

        renamed = rename_remote_folder(base, DOCS_ID, nfd)

        self.assertEqual(renamed["remote_folders"][0]["display_name_enc"], nfc)

    def test_rename_remote_folder_rejects_blank_name(self) -> None:
        docs = make_remote_folder(
            remote_folder_id=DOCS_ID,
            display_name_enc="Documents",
            created_at="2026-05-03T13:00:00.000Z",
            created_by_device_id=AUTHOR,
        )
        base = make_manifest(
            vault_id=VAULT_ID, revision=2, parent_revision=1,
            created_at="2026-05-03T13:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[docs],
        )

        with self.assertRaises(ValueError):
            rename_remote_folder(base, DOCS_ID, "   ")

    def test_rename_remote_folder_unknown_id_raises(self) -> None:
        base = make_manifest(
            vault_id=VAULT_ID, revision=2, parent_revision=1,
            created_at="2026-05-03T13:00:00.000Z",
            author_device_id=AUTHOR,
        )

        with self.assertRaises(ValueError):
            rename_remote_folder(base, DOCS_ID, "Notes")


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
        return make_manifest(
            vault_id=VAULT_ID,
            revision=revision,
            parent_revision=parent_revision,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[],
        )

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

    @staticmethod
    def _version(
        *,
        version_id: str,
        modified_at: str,
        author: str = AUTHOR,
        chunk: str = "00" * 32,
        size: int = 100,
        sha: str = "aa" * 32,
        fp: str = "cf-1",
    ) -> dict:
        return {
            "version_id": version_id,
            "modified_at": modified_at,
            "created_at": modified_at,
            "author_device_id": author,
            "chunks": [{"chunk_id": chunk, "ciphertext_size": size}],
            "logical_size": size,
            "plaintext_sha256": sha,
            "content_fingerprint": fp,
        }

    def test_merge_with_remote_head_preserves_tombstone_latest_version_id(self) -> None:
        """F-D07: when the server head has ``deleted=True`` for an entry,
        ``merge_with_remote_head`` keeps the server's ``latest_version_id``
        even if the local side appended new versions. Re-resolving here
        would point eviction's preserve-latest pass at freshly-uploaded
        chunks belonging to a tombstoned entry — those chunks would
        survive forever despite no UI being able to reach them.
        """
        from src.vault.manifest import (
            add_or_append_file_version, merge_with_remote_head,
            tombstone_file_entry,
        )

        # Step 1: parent has one entry with v1.
        parent = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Docs",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                ),
            ],
        )
        parent_with_v1 = add_or_append_file_version(
            parent,
            remote_folder_id=DOCS_ID, path="report.txt",
            version=self._version(
                version_id="fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                modified_at="2026-05-04T12:00:00.000Z",
            ),
        )
        parent_with_v1["revision"] = 2
        parent_with_v1["parent_revision"] = 1

        # Step 2: server tombstoned the entry → server head has
        # ``deleted=True``, ``latest_version_id`` may still be v1's id
        # (preserved as the "pre-tombstone latest").
        server_head = tombstone_file_entry(
            parent_with_v1, remote_folder_id=DOCS_ID, path="report.txt",
            deleted_at="2026-05-04T13:00:00.000Z",
            author_device_id=AUTHOR,
        )
        server_head["revision"] = 3
        server_head["parent_revision"] = 2
        server_head["created_at"] = "2026-05-04T13:00:00.000Z"
        server_head["author_device_id"] = AUTHOR

        # Snapshot the server's pre-merge fields for the assertion.
        server_entry = server_head["remote_folders"][0]["entries"][0]
        self.assertTrue(server_entry["deleted"])
        server_latest_id_before_merge = server_entry["latest_version_id"]

        # Step 3: locally we appended v2 (raced the server).
        local_attempt = add_or_append_file_version(
            parent_with_v1,
            remote_folder_id=DOCS_ID, path="report.txt",
            version=self._version(
                version_id="fv_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
                modified_at="2026-05-04T12:30:00.000Z",
                chunk="11" * 32, size=200, sha="bb" * 32, fp="cf-2",
            ),
        )
        local_attempt["revision"] = 3
        local_attempt["parent_revision"] = 2

        # Step 4: merge.
        merged = merge_with_remote_head(
            parent=parent_with_v1,
            local_attempt=local_attempt,
            server_head=server_head,
            author_device_id=AUTHOR,
        )

        merged_entry = merged["remote_folders"][0]["entries"][0]
        # Tombstone preserved (§D4 row 3).
        self.assertTrue(merged_entry["deleted"])
        # F-D07: latest_version_id stayed at the server's pre-merge
        # value — NOT re-resolved to v2 just because v2 was appended.
        self.assertEqual(
            merged_entry["latest_version_id"],
            server_latest_id_before_merge,
            "tombstoned entry's latest_version_id must not be re-resolved",
        )
        # Local v2 is still archived as restorable history.
        version_ids = {
            v["version_id"] for v in merged_entry["versions"]
        }
        self.assertEqual(len(version_ids), 2)

    def test_merge_with_remote_head_resolves_latest_for_live_entry(self) -> None:
        """F-D07 negative case: when the entry is still live (no
        tombstone), ``merge_with_remote_head`` continues to re-resolve
        ``latest_version_id`` per §D4. We pin this so the F-D07 fix
        doesn't accidentally suppress the live-merge path.
        """
        from src.vault.manifest import (
            add_or_append_file_version, merge_with_remote_head,
        )

        parent = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Docs",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                ),
            ],
        )
        # Both sides start with v1.
        parent_with_v1 = add_or_append_file_version(
            parent,
            remote_folder_id=DOCS_ID, path="active.txt",
            version=self._version(
                version_id="fv_v1_cccccccccccccccccccccccc",
                modified_at="2026-05-04T12:00:00.000Z",
            ),
        )
        parent_with_v1["revision"] = 2
        parent_with_v1["parent_revision"] = 1

        # Server appends v_server with a strictly-later modified_at;
        # local appends v_local with an older modified_at. Per §D4
        # tie-break by (modified_at, sha256(author)) the merged
        # latest_version_id should be v_server's.
        server_head = add_or_append_file_version(
            parent_with_v1,
            remote_folder_id=DOCS_ID, path="active.txt",
            version=self._version(
                version_id="fv_v1_dddddddddddddddddddddddd",
                modified_at="2026-05-04T15:00:00.000Z",
                chunk="ee" * 32, size=300, sha="cc" * 32, fp="cf-server",
            ),
        )
        server_head["revision"] = 3
        server_head["parent_revision"] = 2

        local_attempt = add_or_append_file_version(
            parent_with_v1,
            remote_folder_id=DOCS_ID, path="active.txt",
            version=self._version(
                version_id="fv_v1_eeeeeeeeeeeeeeeeeeeeeeee",
                modified_at="2026-05-04T13:00:00.000Z",
                chunk="ff" * 32, size=200, sha="dd" * 32, fp="cf-local",
            ),
        )
        local_attempt["revision"] = 3
        local_attempt["parent_revision"] = 2

        merged = merge_with_remote_head(
            parent=parent_with_v1,
            local_attempt=local_attempt,
            server_head=server_head,
            author_device_id=AUTHOR,
        )
        merged_entry = merged["remote_folders"][0]["entries"][0]
        self.assertFalse(merged_entry.get("deleted", False))
        # Find the latest version's modified_at — should be the
        # server's freshest one.
        latest_id = merged_entry["latest_version_id"]
        latest = next(
            v for v in merged_entry["versions"]
            if v["version_id"] == latest_id
        )
        self.assertEqual(latest["modified_at"], "2026-05-04T15:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
