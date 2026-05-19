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
        # Post-split: the vault settings window lives in a package; tests
        # that grep the historical monolith now grep the concatenation
        # of every sibling module.
        pkg = SRC_ROOT / "windows_vault"
        cls.windows_vault = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py"))
        )
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
        # Phase 3 truncation observability (D4): when the rendered tail is
        # at the cap the status label surfaces it.
        self.assertIn("MAX_OP_LOG_TAIL", self.windows_vault)
        self.assertIn("showing most recent", self.windows_vault)

    # ------------------------------------------------------------------
    # Activity-timeline plan (docs/plans/activity-timeline.md):
    # producer-side anchor. Phase 1's wiring tests grep the consumer
    # side; this catches a regression that drops the *producer* side
    # — every shard-publishing site (binding sync, single-file upload,
    # folder upload, ops/delete, ops/eviction, ops/clear) must build
    # entries via ``build_op_log_entry``.
    # ------------------------------------------------------------------
    def test_producer_side_wires_op_log_append(self) -> None:
        # Every shard-publishing site lands entries on the next
        # manifest publish either by calling ``append_op_log_entries``
        # directly OR by delegating to the shared
        # ``publish_root_audit_entry`` helper added in Phase 3.1's
        # consolidation (single source of truth for the four
        # best-effort root-audit publish flows).
        producer_files = [
            SRC_ROOT / "vault" / "binding" / "sync.py",
            SRC_ROOT / "vault" / "upload" / "single_file.py",
            SRC_ROOT / "vault" / "upload" / "folder.py",
            SRC_ROOT / "vault" / "ops" / "delete.py",
            SRC_ROOT / "vault" / "ops" / "eviction.py",
            SRC_ROOT / "vault" / "ops" / "clear.py",
        ]
        for path in producer_files:
            with self.subTest(path=str(path.relative_to(SRC_ROOT))):
                source = path.read_text(encoding="utf-8")
                wires_directly = "append_op_log_entries" in source
                wires_via_helper = "publish_root_audit_entry" in source
                self.assertTrue(
                    wires_directly or wires_via_helper,
                    f"{path.name} must wire either "
                    "append_op_log_entries directly or the shared "
                    "publish_root_audit_entry helper — "
                    "Phase 2/3 of docs/plans/activity-timeline.md",
                )

    def test_unified_merge_includes_shard_tails(self) -> None:
        # Phase 1 D2: assemble_unified_manifest must merge shard tails
        # — without this the Activity tab never sees shard-scoped
        # events even if producers wire them.
        manifest_src = (SRC_ROOT / "vault" / "manifest.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("merged_tail", manifest_src)
        self.assertIn("_op_log_sort_key", manifest_src)

    # ------------------------------------------------------------------
    # F-501.5: Export reminder banner in Recovery tab
    # ------------------------------------------------------------------
    def test_recovery_tab_wires_export_reminder(self) -> None:
        self.assertIn("should_show_export_reminder", self.windows_vault)
        # Dismiss button persists last_dismissed_at.
        self.assertIn(
            "vault_export_reminder_last_dismissed_at", self.windows_vault,
        )

    # ------------------------------------------------------------------
    # Phase 3.1 Wire 4: the three GTK4 callers must invoke the grant
    # audit helper after their respective grant ops. Source-pinned
    # because driving these UIs against the live D-Bus needs a paired
    # vault + admin device, which the unit suite can't easily set up.
    # ------------------------------------------------------------------
    def test_grant_lifecycle_audit_wired_in_gtk_callers(self) -> None:
        callers = {
            "grant_device_dialog (approve)": (
                SRC_ROOT / "windows_vault" / "grant_device_dialog.py",
                "vault.grant.created",
            ),
            "tab_devices (revoke)": (
                SRC_ROOT / "windows_vault" / "tab_devices.py",
                "vault.revoke.completed",
            ),
            "windows_vault_rotate (rotate)": (
                SRC_ROOT / "windows_vault_rotate.py",
                "vault.rotation.completed",
            ),
        }
        for label, (path, event_type) in callers.items():
            with self.subTest(caller=label):
                source = path.read_text(encoding="utf-8")
                self.assertIn(
                    "publish_grant_lifecycle_audit", source,
                    f"{label}: must call publish_grant_lifecycle_audit "
                    "after the grant op (Phase 3.1 Wire 4)",
                )
                self.assertIn(
                    event_type, source,
                    f"{label}: must reference its event_type "
                    f"({event_type!r}) when invoking the audit helper",
                )


if __name__ == "__main__":
    unittest.main()
