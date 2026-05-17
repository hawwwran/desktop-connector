"""F-LT10 — §3.7 manifest rollback detection.

A relay can in principle serve an older manifest revision than the
device has previously seen, hiding recent changes or resurrecting old
state. The defence is a per-vault floor (``vault_manifest_floor``
table in :class:`VaultLocalIndex`): every successful AEAD-verified
decrypt either advances the floor or refuses with
:class:`VaultManifestRollbackError` when the served revision is
strictly less than the persisted floor.

The legacy decrypt_manifest tests (which seeded state from the
pre-sharding ``manifest_v1.json`` vector) were removed in Phase H
step 7f. The remaining tests exercise the ``VaultLocalIndex`` floor /
rollback-flag bookkeeping directly — the AEAD decrypt is now
performed against shard envelopes, which carry per-folder
``shard_revision`` rather than the legacy single-envelope
``revision``; floor enforcement for the sharded path lives in
``Vault.fetch_root_manifest`` / ``fetch_folder_shard`` and is
covered by ``test_desktop_vault_manifest_sharded`` +
``test_desktop_vault_integrity``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.state.local_index import VaultLocalIndex  # noqa: E402
from src.windows_vault.rollback_banner import (  # noqa: E402
    build_rollback_banner_text,
)


class ManifestRollbackFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.index = VaultLocalIndex(self._tmp.name)

    def test_no_flag_set_initially(self) -> None:
        self.assertIsNone(self.index.get_manifest_rollback("ABCD2345WXYZ"))

    def test_explicit_clear_drops_flag(self) -> None:
        self.index.record_manifest_rollback(
            "V1", served_revision=2, floor_revision=5,
        )
        self.assertIsNotNone(self.index.get_manifest_rollback("V1"))
        self.index.clear_manifest_rollback("V1")
        self.assertIsNone(self.index.get_manifest_rollback("V1"))

    def test_record_is_idempotent_with_latest_pair_winning(self) -> None:
        self.index.record_manifest_rollback(
            "V1", served_revision=2, floor_revision=5,
        )
        self.index.record_manifest_rollback(
            "V1", served_revision=3, floor_revision=7,
        )
        record = self.index.get_manifest_rollback("V1")
        self.assertEqual(record["served_revision"], 3)
        self.assertEqual(record["floor_revision"], 7)

    def test_flag_persists_across_index_reopen(self) -> None:
        self.index.record_manifest_rollback(
            "V1", served_revision=2, floor_revision=5,
        )
        reopened = VaultLocalIndex(self._tmp.name)
        record = reopened.get_manifest_rollback("V1")
        self.assertEqual(record["served_revision"], 2)
        self.assertEqual(record["floor_revision"], 5)


class RollbackBannerTextTests(unittest.TestCase):
    def test_banner_text_includes_both_revisions(self) -> None:
        text = build_rollback_banner_text(served_revision=2, floor_revision=5)
        self.assertIn("revision 2", text)
        self.assertIn("revision 5", text)

    def test_banner_text_mentions_fresh_device_caveat(self) -> None:
        text = build_rollback_banner_text(served_revision=2, floor_revision=5)
        self.assertIn("brand-new device", text)

    def test_banner_text_offers_remediation_actions(self) -> None:
        text = build_rollback_banner_text(served_revision=2, floor_revision=5)
        self.assertIn("integrity check", text)
        self.assertIn("export", text)


class ManifestRevisionFloorPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.index = VaultLocalIndex(self._tmp.name)

    def test_bump_returns_true_on_advance_false_on_noop(self) -> None:
        self.assertTrue(self.index.bump_manifest_revision_floor("V1", 5))
        self.assertFalse(self.index.bump_manifest_revision_floor("V1", 5))
        self.assertFalse(self.index.bump_manifest_revision_floor("V1", 3))
        self.assertTrue(self.index.bump_manifest_revision_floor("V1", 6))
        self.assertEqual(self.index.get_manifest_revision_floor("V1"), 6)

    def test_floor_is_per_vault(self) -> None:
        self.index.bump_manifest_revision_floor("V1", 10)
        self.index.bump_manifest_revision_floor("V2", 4)
        self.assertEqual(self.index.get_manifest_revision_floor("V1"), 10)
        self.assertEqual(self.index.get_manifest_revision_floor("V2"), 4)

    def test_floor_persists_across_index_reopen(self) -> None:
        self.index.bump_manifest_revision_floor("V1", 7)
        reopened = VaultLocalIndex(self._tmp.name)
        self.assertEqual(reopened.get_manifest_revision_floor("V1"), 7)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
