"""T14.1 + T14.2 — Clear-folder + Clear-vault danger flows.

The bulk-tombstone mechanics are exercised by
``test_desktop_vault_delete.py`` (the orchestrator calls
``delete_folder_contents`` under the hood); this file pins the
confirm-text guards that live in front of those operations.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.ops.clear import (  # noqa: E402
    confirm_folder_clear_text_matches,
    confirm_vault_clear_text_matches,
)


class ClearVaultLoopUntilStableTests(unittest.TestCase):
    """Review §4.H3: ``clear_vault`` must re-fetch the root between
    passes so a folder added by a concurrent device mid-clear still
    gets tombstoned. Pre-fix the up-front single fetch left such
    folders live and the audit event under-reported."""

    def test_clear_vault_picks_up_folder_added_during_clear(self) -> None:
        from src.vault.ops.clear import clear_vault

        class _FakeVault:
            def __init__(self, fetches):
                self._fetches = list(fetches)
                self.vault_id = "ABCD2345WXYZ"

            def fetch_root_manifest(self, relay):
                if not self._fetches:
                    return {"remote_folders": []}
                return self._fetches.pop(0)

        cleared = []

        def fake_delete_folder_contents(*, vault, relay, manifest,
                                         remote_folder_id, path_prefix,
                                         author_device_id, deleted_at):
            cleared.append(remote_folder_id)
            return ({}, [f"{remote_folder_id}/file-{i}.txt" for i in range(3)])

        # Pass 1: root has folders A + B. Pass 2: root has A + B + C
        # (C was added by a concurrent device while we were clearing
        # A and B). Pass 3 reads stable → exits.
        fakes = [
            {"remote_folders": [
                {"remote_folder_id": "rf_A"},
                {"remote_folder_id": "rf_B"},
            ]},
            {"remote_folders": [
                {"remote_folder_id": "rf_A"},
                {"remote_folder_id": "rf_B"},
                {"remote_folder_id": "rf_C"},
            ]},
            {"remote_folders": [
                {"remote_folder_id": "rf_A"},
                {"remote_folder_id": "rf_B"},
                {"remote_folder_id": "rf_C"},
            ]},
        ]
        vault = _FakeVault(fakes)
        with mock.patch(
            "src.vault.ops.clear.delete_folder_contents",
            side_effect=fake_delete_folder_contents,
        ):
            total = clear_vault(
                vault=vault, relay=object(),
                author_device_id="dev",
                deleted_at="2026-05-17T00:00:00.000Z",
            )

        # All three folders cleared exactly once each (idempotency on
        # already-cleared folders via the seen_folders set).
        self.assertEqual(sorted(cleared), ["rf_A", "rf_B", "rf_C"])
        self.assertEqual(total, 9)  # 3 files × 3 folders


class ConfirmTextTests(unittest.TestCase):
    def test_folder_name_match_is_exact_case(self) -> None:
        self.assertTrue(confirm_folder_clear_text_matches("Documents", "Documents"))
        self.assertFalse(confirm_folder_clear_text_matches("documents", "Documents"))
        self.assertTrue(confirm_folder_clear_text_matches("  Documents  ", "Documents"))

    def test_vault_id_match_is_case_insensitive(self) -> None:
        self.assertTrue(confirm_vault_clear_text_matches("abcd-2345-wxyz", "ABCD-2345-WXYZ"))
        self.assertTrue(confirm_vault_clear_text_matches(" ABCD-2345-WXYZ ", "ABCD-2345-WXYZ"))
        self.assertFalse(confirm_vault_clear_text_matches("WXYZ-2345-ABCD", "ABCD-2345-WXYZ"))

    def test_non_string_inputs_return_false(self) -> None:
        self.assertFalse(confirm_folder_clear_text_matches(None, "x"))  # type: ignore[arg-type]
        self.assertFalse(confirm_folder_clear_text_matches("x", None))  # type: ignore[arg-type]
        self.assertFalse(confirm_vault_clear_text_matches("x", None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
