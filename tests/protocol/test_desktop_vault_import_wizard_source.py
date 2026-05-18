"""T8.5 source pins for the Vault import wizard.

Source-pin file (one of five). See
``test_desktop_vault_browser_source`` for the policy: these greppers
catch UI-string regressions only — import-flow correctness is
covered by ``test_desktop_vault_import`` + the ``vault_import``
unit tests, not here.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class VaultImportWizardSourceTests(unittest.TestCase):
    def test_dispatcher_registers_vault_import_route(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows.py").read_text(encoding="utf-8")
        self.assertIn("from .windows_vault_import import show_vault_import", source)
        self.assertIn('"vault-import"', source)
        # F-U14: dispatcher threads ``vault_id_override`` so the merge
        # target is pinned at subprocess-spawn time instead of resolved
        # off whatever ``last_known_id`` happens to be on disk now.
        self.assertIn(
            "show_vault_import(config_dir, vault_id_override=vault_id_override)",
            source,
        )

    def test_wizard_wires_runner_and_progress_pages(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/windows_vault_import.py").read_text(
            encoding="utf-8"
        )
        for text in (
            "from .vault.import_.runner import open_bundle_for_preview",
            "from .vault.import_.runner import run_import",
            'stack.add_named(pick_box, "pick")',
            'stack.add_named(preview_box, "preview")',
            'stack.add_named(progress_box, "progress")',
            'stack.add_named(summary_box, "summary")',
            "Bundle preview",
            "Import refused",
            "ImportMergeResolution",
        ):
            with self.subTest(text=text):
                self.assertIn(text, source)

    def test_passphrase_best_effort_wipe_on_terminal_paths(self) -> None:
        """Review §5.M1 — the wizard must drop its passphrase reference
        on every terminal path (cancel, fail, succeed, cancelled).
        Python ``str`` immutability rules out true zeroing; the
        ``_wipe_passphrase`` helper drops the dict reference + clears
        the visible entry buffer.

        Source-pin: the helper exists, the cancel handler calls it,
        and every async worker terminal handler (cancelled, fail,
        succeed) does too. Behavioural coverage would require driving
        the wizard via AT-SPI, which the rest of this file deliberately
        avoids.
        """
        source = Path(REPO_ROOT, "desktop/src/windows_vault_import.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_wipe_passphrase", source)
        # At least four call sites: cancel() + cancelled() + fail() + succeed().
        self.assertGreaterEqual(
            source.count("_wipe_passphrase()"), 4,
            "Review §5.M1: _wipe_passphrase must be invoked from "
            "every terminal path (cancel, cancelled, fail, succeed)",
        )

    def test_merge_commit_handler_gated_by_fresh_unlock(self) -> None:
        """F-LT11 — the import-merge commit handler must funnel through
        ``require_fresh_unlock_or_prompt`` before kicking off the
        chunk-upload + manifest-publish worker (mirrors the destructive
        handlers in ``tab_danger.py``). Source-pinned because the
        commit handler is GTK-thin and a refactor that lifts the
        worker out of the gate would silently break the §3.9
        defence.
        """
        source = Path(REPO_ROOT, "desktop/src/windows_vault_import.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("require_fresh_unlock_or_prompt", source)
        self.assertIn("merge bundle into active vault", source)


if __name__ == "__main__":
    unittest.main()
