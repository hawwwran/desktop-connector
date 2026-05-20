"""Source pin: the Folders tab's manifest fetch must be lazy.

Pre-fix the Folders tab fired ``refresh_folders_usage_async`` at the
end of ``build_vault_folders_tab`` — i.e. *during widget construction*,
not on tab-visible. Vault Settings' main_window builds every tab
upfront, so opening Vault Settings (even with the Devices tab
selected) would immediately fire the manifest fetch in the background.

That fetch makes ``1 + N`` auth-billed calls (root + one shard per
folder); paired with the vault browser's own ``fetch_unified_manifest``
inside a single 60-second window it routinely tripped the server-side
``vaultAuthLimit`` rate limit (default cap 10 attempts pre-2026-05-20,
even after the bump to 120 a future heavier workload could re-trip).

The fix wires the fetch behind the ``map`` signal so it only fires
when the tab actually becomes visible, and gates with a flag so
tab re-shows don't re-burn the budget. This file pins the resulting
source shape — a behavioural test would need a live GTK4 main loop.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class VaultFoldersLazyUsageSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tab_source = Path(
            REPO_ROOT, "desktop/src/vault_folders/tab.py",
        ).read_text(encoding="utf-8")

    def test_usage_fetch_is_not_called_eagerly_during_build(self) -> None:
        # Anti-regression: the pre-fix line was a bare
        # ``refresh_folders_usage_async()`` call at module scope inside
        # ``build_vault_folders_tab``. That exact shape must not return.
        self.assertNotIn(
            "\n    refresh_folders_usage_async()\n    return split",
            self.tab_source,
            msg="Folders tab must NOT fire the manifest fetch eagerly "
                "during widget build — see "
                "test_desktop_vault_folders_lazy_usage docstring.",
        )

    def test_usage_fetch_is_deferred_to_map_signal(self) -> None:
        # The replacement wires the fetch behind a ``map`` handler
        # gated by an idempotency flag so tab re-shows don't re-fetch.
        for marker in (
            'split.connect("map"',
            "usage_load_state",
            'usage_load_state["loaded"]',
            "refresh_folders_usage_async()",
        ):
            self.assertIn(
                marker, self.tab_source,
                msg=f"Folders tab must wire {marker!r} to lazily "
                    "fetch usage on first tab map.",
            )


if __name__ == "__main__":
    unittest.main()
