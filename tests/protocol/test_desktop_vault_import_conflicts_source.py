"""§5.H2 — source-pin tests for the per-folder conflict page.

The data layer (`find_conflict_batches`, `ImportMergeResolution`,
`merge_import_into`) is unit-tested elsewhere; this file pins that
the import wizard actually wires the page between Preview and
Progress, builds one card per `FolderConflictBatch`, gates Continue
on every folder having a choice, threads the resolution into
`run_import`, and that the "Apply to remaining" button only fills
in *still-undecided* folders below.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()


SRC_ROOT = Path(
    os.path.dirname(__file__) or "."
).resolve().parent.parent / "desktop" / "src"


class ImportWizardConflictPageSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault_import.py").read_text(encoding="utf-8")

    def test_imports_find_conflict_batches(self) -> None:
        """The wizard reaches into ``vault.import_.bundle`` for the
        library, not a wizard-local re-implementation."""
        self.assertIn("find_conflict_batches", self.text)
        self.assertIn("FolderConflictBatch", self.text)

    def test_conflicts_page_inserted_in_stack(self) -> None:
        """A new ``"conflicts"`` page must be in the stack between
        the existing ``preview`` and ``progress`` pages."""
        preview_pos = self.text.find('stack.add_named(preview_box, "preview")')
        conflicts_pos = self.text.find('stack.add_named(conflicts_box, "conflicts")')
        progress_pos = self.text.find('stack.add_named(progress_box, "progress")')
        self.assertGreater(conflicts_pos, preview_pos)
        self.assertGreater(progress_pos, conflicts_pos)

    def test_preview_worker_computes_conflicts(self) -> None:
        """The preview-load worker calls ``find_conflict_batches``
        against the active vs bundle manifest and stashes the result
        in state for the routing decision."""
        self.assertIn(
            "find_conflict_batches(\n",
            self.text,
            "find_conflict_batches must be called as a function inside the preview worker",
        )
        self.assertIn('state["conflicts"]', self.text)

    def test_import_button_routes_through_conflicts_when_non_empty(self) -> None:
        """``on_import`` checks ``state['conflicts']`` and routes to
        the conflict page when non-empty; otherwise jumps straight to
        the fresh-unlock + import gate."""
        self.assertIn('if state.get("conflicts"):', self.text)
        self.assertIn('go_to("conflicts")', self.text)
        # Still falls through to fresh-unlock when no conflicts.
        self.assertIn("require_fresh_unlock_or_prompt", self.text)

    def test_continue_disabled_until_every_folder_chosen(self) -> None:
        """Continue stays disabled until every batch in
        ``state['conflicts']`` has a matching entry in
        ``state['resolution']``."""
        self.assertIn("conflicts_continue_btn.set_sensitive(False)", self.text)
        self.assertIn(
            "all(b.remote_folder_id in state[\"resolution\"]",
            self.text,
        )

    def test_three_radio_modes_present_per_card(self) -> None:
        """Each card must surface all three §D9 modes (rename default,
        overwrite, skip) — the radio group is the spec contract."""
        for mode in ("rename", "overwrite", "skip"):
            with self.subTest(mode=mode):
                self.assertIn(f'"{mode}"', self.text)
        # Skip warns about whole-folder semantics per spec §17.
        self.assertIn("entire folder", self.text)
        self.assertIn("spec §17", self.text)

    def test_apply_to_remaining_skips_already_decided_folders(self) -> None:
        """The "Apply to remaining" affordance only writes into
        folders the operator hasn't already explicitly picked. That
        prevents an unintended bulk-overwrite of earlier deliberate
        choices when the operator clicks the button on a later card."""
        self.assertIn("Apply this choice to remaining folders", self.text)
        # The guard that protects already-decided folders.
        self.assertIn(
            "Don't overwrite an explicit pick", self.text,
        )

    def test_resolution_threads_into_run_import(self) -> None:
        """The previously hard-coded ``per_folder={}`` is gone; the
        page's output flows into ``ImportMergeResolution`` at the
        actual merge boundary."""
        self.assertNotIn(
            "ImportMergeResolution(per_folder={})", self.text,
            "hard-coded empty resolution must be replaced by state-backed picks",
        )
        self.assertIn(
            'per_folder=dict(state.get("resolution") or {})',
            self.text,
        )

    def test_back_button_returns_to_preview_without_clearing_picks(self) -> None:
        """Back returns to Preview but doesn't wipe ``state['resolution']``;
        the operator can re-open the conflicts page and see their
        prior picks pre-filled (the renderer pre-checks the radio for
        any folder already in resolution)."""
        self.assertIn('go_to("preview")', self.text)
        # Pre-fill logic exists.
        self.assertIn("pre_pick", self.text)
        self.assertIn(
            'pre_pick = state["resolution"].get(batch.remote_folder_id)',
            self.text,
        )


if __name__ == "__main__":
    unittest.main()
