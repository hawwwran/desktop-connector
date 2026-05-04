"""T17.4 — Vault repair: mark broken + plan restore from export."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_repair import (  # noqa: E402
    RepairPlan, RepairResult,
    mark_broken_in_next_revision, plan_restore_from_export,
)


VAULT_ID = "ABCD2345WXYZ"
DOCS = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _entry_with_versions(*versions, path: str) -> dict:
    return {
        "type": "file",
        "path": path,
        "deleted": False,
        "versions": list(versions),
        "latest_version_id": versions[-1]["version_id"] if versions else "",
    }


def _version(version_id: str, *chunk_ids: str) -> dict:
    return {
        "version_id": version_id,
        "logical_size": 16,
        "content_fingerprint": "fp_" + version_id,
        "chunks": [{"chunk_id": cid} for cid in chunk_ids],
    }


def _manifest_with_entries(*entries, revision: int = 5, parent_revision: int = 4) -> dict:
    return {
        "vault_id": VAULT_ID,
        "revision": revision,
        "parent_revision": parent_revision,
        "remote_folders": [
            {
                "remote_folder_id": DOCS,
                "display_name_enc": "Documents",
                "entries": list(entries),
            }
        ],
    }


class MarkBrokenTests(unittest.TestCase):
    def test_no_broken_chunks_yields_empty_plan(self) -> None:
        manifest = _manifest_with_entries(
            _entry_with_versions(
                _version("fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa", "ch_v1_a"),
                path="alpha.txt",
            ),
        )
        result = mark_broken_in_next_revision(
            manifest, broken_chunk_ids=set(),
            author_device_id=AUTHOR,
            repaired_at="2026-05-05T00:00:00.000Z",
        )
        self.assertEqual(result.plans, [])
        # Revision still bumps so the audit trail records the repair attempt.
        self.assertEqual(result.manifest["revision"], 6)

    def test_purge_single_broken_version_keeps_rest(self) -> None:
        # Two versions on the same path; only one references a broken chunk.
        v1 = _version("fv_v1_111111111111111111111111", "ch_v1_a", "ch_v1_broken")
        v2 = _version("fv_v1_222222222222222222222222", "ch_v1_b", "ch_v1_c")
        manifest = _manifest_with_entries(
            _entry_with_versions(v1, v2, path="alpha.txt"),
        )
        result = mark_broken_in_next_revision(
            manifest, broken_chunk_ids={"ch_v1_broken"},
            author_device_id=AUTHOR,
            repaired_at="2026-05-05T00:00:00.000Z",
        )
        self.assertEqual(len(result.plans), 1)
        plan = result.plans[0]
        self.assertEqual(plan.action, "purge_versions")
        self.assertEqual(plan.path, "alpha.txt")
        self.assertEqual(plan.broken_chunk_ids, ("ch_v1_broken",))

        # Manifest now has only v2 on alpha.txt.
        entry = result.manifest["remote_folders"][0]["entries"][0]
        self.assertEqual([v["version_id"] for v in entry["versions"]],
                         ["fv_v1_222222222222222222222222"])
        self.assertEqual(entry["latest_version_id"],
                         "fv_v1_222222222222222222222222")
        self.assertFalse(entry.get("deleted"))

    def test_all_versions_broken_tombstones_entry(self) -> None:
        v1 = _version("fv_v1_111111111111111111111111", "ch_v1_dead")
        v2 = _version("fv_v1_222222222222222222222222", "ch_v1_dead", "ch_v1_a")
        manifest = _manifest_with_entries(
            _entry_with_versions(v1, v2, path="dead.txt"),
        )
        result = mark_broken_in_next_revision(
            manifest, broken_chunk_ids={"ch_v1_dead"},
            author_device_id=AUTHOR,
            repaired_at="2026-05-05T00:00:00.000Z",
        )
        self.assertEqual(len(result.plans), 1)
        self.assertEqual(result.plans[0].action, "tombstone_entry")

        entry = result.manifest["remote_folders"][0]["entries"][0]
        self.assertTrue(entry["deleted"])
        self.assertEqual(entry["deleted_at"], "2026-05-05T00:00:00.000Z")
        self.assertEqual(entry["deleted_by_device_id"], AUTHOR)
        self.assertEqual(entry["repair_reason"], "broken_chunks")
        # All versions retained in op-log (not purged from .versions).
        self.assertEqual(len(entry["versions"]), 2)

    def test_unaffected_entries_pass_through(self) -> None:
        bad = _entry_with_versions(
            _version("fv_v1_111111111111111111111111", "ch_v1_dead"),
            path="bad.txt",
        )
        good = _entry_with_versions(
            _version("fv_v1_222222222222222222222222", "ch_v1_a"),
            path="good.txt",
        )
        manifest = _manifest_with_entries(bad, good)
        result = mark_broken_in_next_revision(
            manifest, broken_chunk_ids={"ch_v1_dead"},
            author_device_id=AUTHOR,
            repaired_at="2026-05-05T00:00:00.000Z",
        )
        # One plan (for bad.txt); good.txt unchanged.
        self.assertEqual(len(result.plans), 1)
        good_entry = next(
            e for e in result.manifest["remote_folders"][0]["entries"]
            if e["path"] == "good.txt"
        )
        self.assertEqual(good_entry["latest_version_id"],
                         "fv_v1_222222222222222222222222")
        self.assertFalse(good_entry.get("deleted"))

    def test_revision_chain_still_links(self) -> None:
        manifest = _manifest_with_entries(
            _entry_with_versions(
                _version("fv_v1_111111111111111111111111", "ch_v1_a"),
                path="alpha.txt",
            ),
            revision=10, parent_revision=9,
        )
        result = mark_broken_in_next_revision(
            manifest, broken_chunk_ids=set(),
            author_device_id=AUTHOR,
            repaired_at="2026-05-05T00:00:00.000Z",
        )
        self.assertEqual(result.manifest["parent_revision"], 10)
        self.assertEqual(result.manifest["revision"], 11)


class PlanRestoreTests(unittest.TestCase):
    def test_plan_restore_returns_one_per_broken_path(self) -> None:
        plans = plan_restore_from_export(broken_paths=[
            (DOCS, "broken-1.txt"),
            (DOCS, "broken-2.txt"),
        ])
        self.assertEqual(len(plans), 2)
        for p in plans:
            self.assertEqual(p.action, "restore_from_export")
            self.assertEqual(p.remote_folder_id, DOCS)

    def test_plan_restore_empty_input_returns_empty(self) -> None:
        self.assertEqual(plan_restore_from_export(broken_paths=[]), [])


if __name__ == "__main__":
    unittest.main()
