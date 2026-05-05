"""F-501 — source-pin tests that the T17 diagnostics surface is wired.

Each T17 module has its own behavioural test suite already; this file
catches the specific F-501 regression: "module exists but UI doesn't
expose it." The risk we want to defend against is someone refactoring
the placeholder loop and silently re-introducing a dead Activity or
Maintenance tab.

Source-pin assertions are tautological by their nature (F-T08 nit) —
acceptable here because the actual user-visible verification needs
a live Wayland session + a real vault, which the AT-SPI scaffolding
on this machine can't easily set up without live data.
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


class T17DiagnosticsWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.windows_vault = (SRC_ROOT / "windows_vault.py").read_text(encoding="utf-8")
        cls.windows = (SRC_ROOT / "windows.py").read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # F-501.1: vault.log handler attaches on subprocess startup
    # ------------------------------------------------------------------
    def test_vault_subprocess_attaches_log_handler(self) -> None:
        self.assertIn("attach_vault_log_handler", self.windows)
        # Gate is "vault-*" prefix so non-vault windows don't write to vault.log.
        self.assertIn('args.window.startswith("vault-")', self.windows)

    # ------------------------------------------------------------------
    # F-501.2 + F-501.3: Maintenance tab → debug bundle + integrity check
    # ------------------------------------------------------------------
    def test_maintenance_tab_replaces_placeholder(self) -> None:
        # Locate the placeholder list and assert "maintenance" is not
        # in it. (The string ``("maintenance",`` also appears in the
        # ``add_tab`` call for the real tab — substring search alone
        # would false-positive.)
        import re
        m = re.search(
            r"# Other tabs are empty placeholders.*?\]:",
            self.windows_vault, re.DOTALL,
        )
        self.assertIsNotNone(
            m, "F-501: placeholder loop preamble missing"
        )
        self.assertNotIn(
            '"maintenance"', m.group(0),
            "F-501: Maintenance must be a real tab, not a placeholder",
        )

    def test_maintenance_tab_wires_debug_bundle(self) -> None:
        self.assertIn("write_debug_bundle", self.windows_vault)
        self.assertIn("DebugBundleError", self.windows_vault)
        # The button + worker thread exist.
        self.assertIn("Download debug bundle", self.windows_vault)

    def test_maintenance_tab_wires_integrity_check(self) -> None:
        self.assertIn("run_quick_check", self.windows_vault)
        self.assertIn("run_full_check", self.windows_vault)
        # Both Quick + Full buttons.
        self.assertIn('label="Quick check"', self.windows_vault)
        self.assertIn('label="Full check"', self.windows_vault)
        # Issue list rendering.
        self.assertIn("_format_integrity_report", self.windows_vault)

    # ------------------------------------------------------------------
    # F-501.4: Activity tab → merge_timeline + filter
    # ------------------------------------------------------------------
    def test_activity_tab_replaces_placeholder(self) -> None:
        import re
        m = re.search(
            r"# Other tabs are empty placeholders.*?\]:",
            self.windows_vault, re.DOTALL,
        )
        self.assertIsNotNone(
            m, "F-501: placeholder loop preamble missing"
        )
        self.assertNotIn(
            '"activity"', m.group(0),
            "F-501: Activity must be a real tab, not a placeholder",
        )

    def test_activity_tab_wires_merge_timeline(self) -> None:
        self.assertIn("merge_timeline", self.windows_vault)
        self.assertIn("filter_timeline", self.windows_vault)
        # Operation log feed.
        self.assertIn('"operation_log_tail"', self.windows_vault)
        # Filter widgets.
        self.assertIn("Gtk.SearchEntry", self.windows_vault)

    # ------------------------------------------------------------------
    # F-501.5: Export reminder banner in Recovery tab
    # ------------------------------------------------------------------
    def test_recovery_tab_wires_export_reminder(self) -> None:
        self.assertIn("should_show_export_reminder", self.windows_vault)
        # Dismiss button persists last_dismissed_at.
        self.assertIn(
            "vault_export_reminder_last_dismissed_at", self.windows_vault,
        )


if __name__ == "__main__":
    unittest.main()
