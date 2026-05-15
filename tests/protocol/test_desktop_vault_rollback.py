"""F-LT10 — §3.7 manifest rollback detection.

A relay can in principle serve an older manifest revision than the
device has previously seen, hiding recent changes or resurrecting old
state. The defence is a per-vault floor (``vault_manifest_floor``
table in :class:`VaultLocalIndex`): every successful AEAD-verified
decrypt either advances the floor or refuses with
:class:`VaultManifestRollbackError` when the served revision is
strictly less than the persisted floor.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.relay_errors import VaultManifestRollbackError  # noqa: E402
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402
from src.windows_vault.rollback_banner import (  # noqa: E402
    build_rollback_banner_text,
)


MASTER_KEY = bytes.fromhex(
    "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
)


def _vector(name: str) -> dict:
    path = Path(REPO_ROOT, "tests/protocol/vault-v1/manifest_v1.json")
    for case in json.loads(path.read_text(encoding="utf-8")):
        if case["name"] == name:
            return case
    raise KeyError(name)


def _vault_at(case: dict) -> Vault:
    inputs = case["inputs"]
    return Vault(
        vault_id=inputs["vault_id"],
        master_key=MASTER_KEY,
        recovery_secret=None,
        vault_access_secret="unused",
        header_revision=1,
        manifest_revision=int(inputs["revision"]),
        manifest_ciphertext=bytes.fromhex(case["expected"]["envelope_bytes"]),
        crypto=DefaultVaultCrypto,
    )


class ManifestRollbackFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.index = VaultLocalIndex(self._tmp.name)

    def test_floor_starts_at_zero_for_unknown_vault(self) -> None:
        self.assertEqual(self.index.get_manifest_revision_floor("ABCD2345WXYZ"), 0)

    def test_first_decrypt_advances_floor_to_served_revision(self) -> None:
        case = _vector("manifest-v1-t4-remove-remote-folder")  # revision=3
        _vault_at(case).decrypt_manifest(local_index=self.index)
        self.assertEqual(self.index.get_manifest_revision_floor("ABCD2345WXYZ"), 3)

    def test_monotonic_advance_across_two_decrypts(self) -> None:
        v1 = _vector("manifest-v1-genesis-happy-path")  # revision=1
        v3 = _vector("manifest-v1-t4-remove-remote-folder")  # revision=3
        _vault_at(v1).decrypt_manifest(local_index=self.index)
        self.assertEqual(self.index.get_manifest_revision_floor("ABCD2345WXYZ"), 1)
        _vault_at(v3).decrypt_manifest(local_index=self.index)
        self.assertEqual(self.index.get_manifest_revision_floor("ABCD2345WXYZ"), 3)

    def test_rollback_after_advance_raises_typed_error(self) -> None:
        v3 = _vector("manifest-v1-t4-remove-remote-folder")  # revision=3
        v2 = _vector("manifest-v1-t4-add-remote-folder")  # revision=2
        _vault_at(v3).decrypt_manifest(local_index=self.index)
        self.assertEqual(self.index.get_manifest_revision_floor("ABCD2345WXYZ"), 3)

        with self.assertRaises(VaultManifestRollbackError) as ctx:
            _vault_at(v2).decrypt_manifest(local_index=self.index)

        err = ctx.exception
        self.assertEqual(err.vault_id, "ABCD2345WXYZ")
        self.assertEqual(err.served_revision, 2)
        self.assertEqual(err.floor_revision, 3)

    def test_rollback_does_not_advance_or_regress_floor(self) -> None:
        v3 = _vector("manifest-v1-t4-remove-remote-folder")  # revision=3
        v2 = _vector("manifest-v1-t4-add-remote-folder")  # revision=2
        _vault_at(v3).decrypt_manifest(local_index=self.index)

        with self.assertRaises(VaultManifestRollbackError):
            _vault_at(v2).decrypt_manifest(local_index=self.index)

        self.assertEqual(self.index.get_manifest_revision_floor("ABCD2345WXYZ"), 3)

    def test_rollback_does_not_refresh_remote_folders_cache(self) -> None:
        v3 = _vector("manifest-v1-t4-remove-remote-folder")  # revision=3
        v2 = _vector("manifest-v1-t4-add-remote-folder")  # revision=2
        _vault_at(v3).decrypt_manifest(local_index=self.index)
        cache_after_v3 = self.index.list_remote_folders("ABCD2345WXYZ")

        with self.assertRaises(VaultManifestRollbackError):
            _vault_at(v2).decrypt_manifest(local_index=self.index)

        self.assertEqual(self.index.list_remote_folders("ABCD2345WXYZ"), cache_after_v3)

    def test_same_revision_replay_is_accepted_without_bump(self) -> None:
        v3 = _vector("manifest-v1-t4-remove-remote-folder")  # revision=3
        _vault_at(v3).decrypt_manifest(local_index=self.index)
        # A second decrypt at the same revision (e.g. cache reload) must
        # not raise — only a strict downgrade does.
        _vault_at(v3).decrypt_manifest(local_index=self.index)
        self.assertEqual(self.index.get_manifest_revision_floor("ABCD2345WXYZ"), 3)

    def test_decrypt_without_local_index_skips_floor_check(self) -> None:
        v3 = _vector("manifest-v1-t4-remove-remote-folder")
        v2 = _vector("manifest-v1-t4-add-remote-folder")
        _vault_at(v3).decrypt_manifest(local_index=self.index)

        # Diagnostic / probing paths (integrity check) pass no index —
        # they must not be blocked even if the relay served an older
        # revision than the floor.
        manifest = _vault_at(v2).decrypt_manifest()
        self.assertEqual(int(manifest["revision"]), 2)


class ManifestRollbackFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.index = VaultLocalIndex(self._tmp.name)

    def test_no_flag_set_initially(self) -> None:
        self.assertIsNone(self.index.get_manifest_rollback("ABCD2345WXYZ"))

    def test_decrypt_rollback_records_flag_before_raising(self) -> None:
        v3 = _vector("manifest-v1-t4-remove-remote-folder")
        v2 = _vector("manifest-v1-t4-add-remote-folder")
        _vault_at(v3).decrypt_manifest(local_index=self.index)

        with self.assertRaises(VaultManifestRollbackError):
            _vault_at(v2).decrypt_manifest(local_index=self.index)

        record = self.index.get_manifest_rollback("ABCD2345WXYZ")
        self.assertIsNotNone(record)
        self.assertEqual(record["served_revision"], 2)
        self.assertEqual(record["floor_revision"], 3)
        self.assertGreater(record["detected_at"], 0)

    def test_successful_decrypt_clears_latched_flag(self) -> None:
        v3 = _vector("manifest-v1-t4-remove-remote-folder")
        v2 = _vector("manifest-v1-t4-add-remote-folder")
        _vault_at(v3).decrypt_manifest(local_index=self.index)
        with self.assertRaises(VaultManifestRollbackError):
            _vault_at(v2).decrypt_manifest(local_index=self.index)
        self.assertIsNotNone(self.index.get_manifest_rollback("ABCD2345WXYZ"))

        # Relay resumes serving fresh state — same revision counts as
        # "no rollback" and self-heals the latched warning.
        _vault_at(v3).decrypt_manifest(local_index=self.index)
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
