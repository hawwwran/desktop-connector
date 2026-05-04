"""T17.1 — Activity tab data layer (op-log + audit-events timeline)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_activity import (  # noqa: E402
    ACTIVITY_KIND_PREFIXES, ActivityRow,
    filter_timeline, merge_timeline,
    normalize_audit_event, normalize_op_log_entry,
)


class NormalizeAuditEventTests(unittest.TestCase):
    def test_known_event_normalises(self) -> None:
        row = {
            "event_type": "vault.upload.completed",
            "created_at": 1_000_000,
            "device_id": "abcd1234abcd1234abcd1234abcd1234",
            "revision": 7,
            "details": {"size": 2048},
        }
        out = normalize_audit_event(row)
        self.assertIsNotNone(out)
        self.assertEqual(out.event_type, "vault.upload.completed")
        self.assertEqual(out.timestamp_epoch, 1_000_000)
        self.assertEqual(out.revision, 7)
        self.assertEqual(out.extra, {"size": 2048})

    def test_unknown_prefix_dropped(self) -> None:
        row = {
            "event_type": "transfer.init.accepted",
            "created_at": 1, "device_id": "x" * 32,
        }
        self.assertIsNone(normalize_audit_event(row))

    def test_empty_event_type_dropped(self) -> None:
        self.assertIsNone(normalize_audit_event({"event_type": ""}))


class NormalizeOpLogTests(unittest.TestCase):
    def test_op_log_entry_carries_path_and_device_name(self) -> None:
        entry = {
            "ts": 1_000_500,
            "type": "vault.upload.new_version",
            "path": "Documents/notes.txt",
            "device_id": "abcd1234abcd1234abcd1234abcd1234",
            "device_name": "Office Laptop",
            "summary": "uploaded notes.txt (12 KB)",
            "revision": 9,
        }
        row = normalize_op_log_entry(entry)
        self.assertIsNotNone(row)
        self.assertEqual(row.display_path, "Documents/notes.txt")
        self.assertEqual(row.device_name, "Office Laptop")
        self.assertIn("notes.txt", row.summary)


class MergeTimelineTests(unittest.TestCase):
    def test_merges_and_sorts_newest_first(self) -> None:
        audit = [
            {"event_type": "vault.upload.completed",
             "created_at": 1_000_000, "device_id": "a"*32},
            {"event_type": "vault.delete.tombstoned",
             "created_at": 1_001_000, "device_id": "a"*32},
        ]
        op_log = [
            {"ts": 1_000_500, "type": "vault.upload.completed",
             "path": "x.txt", "device_id": "a"*32,
             "summary": "richer summary"},
        ]
        rows = merge_timeline(audit_rows=audit, op_log_entries=op_log)
        # Newest first.
        self.assertEqual(rows[0].timestamp_epoch, 1_001_000)
        # 3 rows BEFORE de-dup since the keys are all distinct, but no
        # overlap occurs here so we keep all three.
        self.assertEqual(len(rows), 3)

    def test_dedup_prefers_richer_plaintext(self) -> None:
        # Same (ts, event_type, device_id, path) → de-duplicated; the
        # richer one (summary populated) wins.
        audit = [
            {"event_type": "vault.upload.completed",
             "created_at": 1_000_000,
             "device_id": "a"*32},
        ]
        op_log = [
            {"ts": 1_000_000, "type": "vault.upload.completed",
             "path": "", "device_id": "a"*32,
             "summary": "uploaded a 12 KB file"},
        ]
        rows = merge_timeline(audit_rows=audit, op_log_entries=op_log)
        # Both have identical key → 1 row.
        self.assertEqual(len(rows), 1)
        self.assertIn("12 KB", rows[0].summary)

    def test_empty_inputs_return_empty(self) -> None:
        self.assertEqual(merge_timeline(), [])
        self.assertEqual(merge_timeline(audit_rows=[], op_log_entries=[]), [])


class FilterTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            ActivityRow(timestamp_epoch=1, event_type="vault.upload.completed",
                        display_path="Documents/notes.txt", summary="uploaded notes.txt"),
            ActivityRow(timestamp_epoch=2, event_type="vault.delete.tombstoned",
                        display_path="Photos/old.jpg", summary="tombstoned old.jpg"),
            ActivityRow(timestamp_epoch=3, event_type="vault.purge.scheduled",
                        display_path="", summary="scheduled hard purge in 24h"),
            ActivityRow(timestamp_epoch=4, event_type="vault.grant.approved",
                        display_path="", summary="approved Office Laptop"),
        ]

    def test_kind_filter_keeps_matching_rows(self) -> None:
        out = filter_timeline(self.rows, kind_categories={"vault.upload"})
        self.assertEqual([r.event_type for r in out], ["vault.upload.completed"])

    def test_kind_filter_multiple_categories(self) -> None:
        out = filter_timeline(self.rows, kind_categories={"vault.upload", "vault.purge"})
        self.assertEqual(len(out), 2)

    def test_filename_search_matches_display_path_and_summary(self) -> None:
        out = filter_timeline(self.rows, filename_search="notes")
        self.assertEqual([r.event_type for r in out], ["vault.upload.completed"])
        out = filter_timeline(self.rows, filename_search="laptop")
        self.assertEqual([r.event_type for r in out], ["vault.grant.approved"])

    def test_filename_search_is_case_insensitive(self) -> None:
        out = filter_timeline(self.rows, filename_search="NOTES")
        self.assertEqual(len(out), 1)

    def test_combined_filter(self) -> None:
        out = filter_timeline(
            self.rows,
            kind_categories={"vault.upload"},
            filename_search="notes",
        )
        self.assertEqual(len(out), 1)
        out = filter_timeline(
            self.rows,
            kind_categories={"vault.upload"},
            filename_search="old.jpg",  # different category
        )
        self.assertEqual(out, [])


class CategoryCoverageTests(unittest.TestCase):
    def test_acceptance_event_kinds_are_all_recognised(self) -> None:
        # The T17.1 acceptance lists: create, upload, delete, restore,
        # clear, device grant, revocation, migration, eviction, purge.
        # Each must hit ACTIVITY_KIND_PREFIXES.
        for prefix in [
            "vault.create.", "vault.upload.", "vault.delete.",
            "vault.restore.", "vault.folder.",  # folder.cleared etc.
            "vault.vault.",  # vault.cleared
            "vault.grant.", "vault.revoke.",
            "vault.migration.", "vault.eviction.", "vault.purge.",
        ]:
            self.assertTrue(
                any(prefix.startswith(p) or p.startswith(prefix) or p == prefix.rstrip(".")
                    for p in ACTIVITY_KIND_PREFIXES),
                f"prefix {prefix!r} not recognised",
            )


if __name__ == "__main__":
    unittest.main()
