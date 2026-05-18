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


if __name__ == "__main__":
    unittest.main()
