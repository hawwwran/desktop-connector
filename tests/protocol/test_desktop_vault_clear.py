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
                                         author_device_id, deleted_at,
                                         summary_op_log_event=None,
                                         summary_op_log_path=""):
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


class ClearVaultRootOpLogTests(unittest.TestCase):
    """Phase 3 of docs/plans/activity-timeline.md — clear_vault now
    publishes a post-clear root revision carrying a
    ``vault.vault.cleared`` op-log entry. End-to-end test that decrypts
    the post-clear root and asserts the entry is present.
    """

    def test_clear_vault_lands_root_audit_entry(self) -> None:
        from src.vault.ops.clear import clear_vault
        from tests.protocol.test_desktop_vault_delete import (
            _seeded_manifest, _vault as _delete_vault,
        )
        from tests.protocol.test_desktop_vault_upload import (
            FakeUploadRelay, seed_sharded_state,
        )
        from tests.protocol.test_desktop_vault_manifest import DOCS_ID, AUTHOR

        manifest = _seeded_manifest([("alpha.txt", "a"), ("beta.txt", "b")])
        relay = FakeUploadRelay()
        vault = _delete_vault()
        try:
            seed_sharded_state(
                vault, relay,
                vault_id=manifest["vault_id"],
                remote_folders=manifest["remote_folders"],
                created_at=manifest["created_at"],
                author_device_id=manifest["author_device_id"],
            )
            clear_vault(
                vault=vault, relay=relay, author_device_id=AUTHOR,
            )
        finally:
            vault.close()

        # Decrypt the final root manifest and assert the audit row.
        observer = _delete_vault()
        try:
            root = observer.decrypt_root_envelope(relay.root_envelope)
        finally:
            observer.close()
        tail = root.get("operation_log_tail") or []
        vault_cleared = [
            e for e in tail if e.get("type") == "vault.vault.cleared"
        ]
        self.assertEqual(len(vault_cleared), 1)
        entry = vault_cleared[0]
        self.assertEqual(entry["device_id"], AUTHOR)
        self.assertIn("Cleared 2 file(s)", entry.get("summary", ""))
        self.assertEqual(entry["revision"], int(root["root_revision"]))


class ClearVaultAuditPublishFailedTests(unittest.TestCase):
    """When the audit-publish fails (every CAS retry 409s, or any
    other exception fires), ``clear_vault`` must surface the missing
    audit row in its terminal log line — not silently report success
    and confuse a future operator."""

    def test_terminal_log_flags_audit_missing_when_publish_fails(self) -> None:
        import logging
        from unittest import mock
        from src.vault.ops.clear import clear_vault

        class _FakeVault:
            vault_id = "ABCD2345WXYZ"

            def fetch_root_manifest(self, relay):
                return {"remote_folders": []}

        # No folders to clear — the terminal log fires immediately.
        # Mock _publish_root_op_log_entry to simulate exhaustion.
        with mock.patch(
            "src.vault.ops.clear._publish_root_op_log_entry",
            return_value=False,  # simulate "every retry failed"
        ):
            with self.assertLogs("src.vault.ops.clear", level=logging.INFO) as cap:
                clear_vault(
                    vault=_FakeVault(), relay=object(),
                    author_device_id="dev",
                )
        joined = "\n".join(cap.output)
        self.assertIn("vault.vault.cleared", joined)
        self.assertIn("audit_row=missing", joined)

    def test_terminal_log_flags_audit_landed_when_publish_succeeds(self) -> None:
        import logging
        from unittest import mock
        from src.vault.ops.clear import clear_vault

        class _FakeVault:
            vault_id = "ABCD2345WXYZ"

            def fetch_root_manifest(self, relay):
                return {"remote_folders": []}

        with mock.patch(
            "src.vault.ops.clear._publish_root_op_log_entry",
            return_value=True,
        ):
            with self.assertLogs("src.vault.ops.clear", level=logging.INFO) as cap:
                clear_vault(
                    vault=_FakeVault(), relay=object(),
                    author_device_id="dev",
                )
        joined = "\n".join(cap.output)
        self.assertIn("audit_row=landed", joined)


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
