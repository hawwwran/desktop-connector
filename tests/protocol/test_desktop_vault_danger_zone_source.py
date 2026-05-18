"""F-U22 — source-pin tests for the Danger zone tab.

The Danger zone backend (``vault_clear`` + ``vault_purge_schedule``)
is fully unit-tested via ``test_desktop_vault_clear*`` /
``test_desktop_vault_purge_schedule*``. This file pins that the GTK
builder in ``windows_vault.py`` actually wires those backend
functions into a UI surface — F-U22 was specifically about the gap
between "backend exists" and "user can invoke it".

Source-pinning is the right test layer here:

- The UI is GTK-thin (~250 LOC of widget construction + click
  handlers). Driving it via dogtail would require a live vault and
  a Wayland session.
- The risk we want to catch is "someone refactors and removes the
  Clear-folder button without noticing". Substring assertions catch
  that for free; the F-T08 nit (source-pin tests are tautological)
  applies but is acceptable per its own resolution note.
- The behavioural-correctness risks (typed-confirm gate, manifest
  mutation contract, schedule persistence) are unit-tested at the
  function layer.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()


PKG_DIR = Path(
    os.path.dirname(__file__) or "."
).resolve().parent.parent / "desktop" / "src" / "windows_vault"


class DangerZoneRowsPresentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Post-split: the danger zone lives in tab_danger.py; concatenating
        # the whole package keeps any cross-tab assertion intact.
        cls.text = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(PKG_DIR.glob("*.py"))
        )

    def test_clear_folder_section_wired(self) -> None:
        """Clear-folder UI references the backend function + typed gate."""
        self.assertIn("Clear folder", self.text)
        self.assertIn("clear_folder(", self.text)
        self.assertIn("confirm_folder_clear_text_matches", self.text)

    def test_clear_whole_vault_section_wired(self) -> None:
        """Clear-vault UI references the backend function + typed gate."""
        self.assertIn("Clear whole vault", self.text)
        self.assertIn("clear_vault(", self.text)
        # vault-id confirmation is shared with schedule-purge so this
        # function appears at least twice (clear + schedule).
        self.assertGreaterEqual(
            self.text.count("confirm_vault_clear_text_matches"), 2,
        )

    def test_schedule_hard_purge_section_wired(self) -> None:
        """Schedule-purge UI references the persistence helper."""
        self.assertIn("Schedule hard purge", self.text)
        self.assertIn("schedule_purge(", self.text)
        # Cancel-pending surface — clears state when the user changes
        # their mind before the delay elapses.
        self.assertIn("Cancel scheduled purge", self.text)
        self.assertIn("cancel_purge(", self.text)

    def test_destructive_dialogs_use_typed_confirm_gate(self) -> None:
        """Per `feedback_security_ux.md`: confirmation gate is mandatory.

        Each destructive dialog must enable its primary response only
        after the user types the expected name/id (the dispatch lives
        in the ``on_typed`` closures via ``set_response_enabled``).
        """
        # Three destructive responses — one per section.
        self.assertGreaterEqual(
            self.text.count("ResponseAppearance.DESTRUCTIVE"), 4,
            "F-U22 expects at least 4 DESTRUCTIVE responses "
            "(disconnect + clear-folder + clear-vault + schedule-purge)",
        )
        self.assertIn("set_response_enabled", self.text)

    def test_destructive_handlers_gated_by_fresh_unlock(self) -> None:
        """F-LT11 — clear-folder / clear-vault / schedule-purge handlers
        must funnel through ``require_fresh_unlock_or_prompt`` before
        opening the typed-confirm dialog. Source-pinned so a future
        refactor that hoists the dialog construction out of the gate
        is caught here rather than at a live-test pass.
        """
        self.assertIn("require_fresh_unlock_or_prompt", self.text)
        # Three gate sites in tab_danger.py — one per destructive
        # handler (clear-folder, clear-vault, schedule-purge).
        self.assertGreaterEqual(
            self.text.count("require_fresh_unlock_or_prompt"), 3,
            "F-LT11 expects the fresh-unlock gate at each destructive "
            "handler entry in tab_danger.py",
        )

    def test_destructive_handlers_pass_on_cancel(self) -> None:
        """Review §6.H5: each ``require_fresh_unlock_or_prompt`` call
        in the Danger tab must pass an ``on_cancel`` handler that
        writes explicit feedback to the danger status label. Pre-fix
        a cancelled fresh-unlock silently re-enabled the destructive
        button and the user could reasonably think the action had
        proceeded.

        Source-pinned because the cancellation path can only be
        exercised end-to-end with a live AT-SPI driver; the substring
        gate catches regressions during refactors without needing one.
        """
        tab_danger_text = (PKG_DIR / "tab_danger.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            tab_danger_text.count("on_cancel="), 3,
            "Review §6.H5: each of the three destructive handlers in "
            "tab_danger.py (clear-folder, clear-vault, schedule-purge) "
            "must pass on_cancel so the status label says "
            "'… cancelled.' when the user backs out of fresh-unlock.",
        )
        # Each cancelled() local writes to the status label so the
        # user sees what happened rather than a silent re-enable.
        self.assertGreaterEqual(
            tab_danger_text.count("cancelled."), 3,
            "Review §6.H5: cancel feedback strings must reach "
            "_set_danger_status so the user sees explicit cancellation.",
        )


if __name__ == "__main__":
    unittest.main()
