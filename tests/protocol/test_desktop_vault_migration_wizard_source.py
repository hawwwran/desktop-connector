"""§5.C1 — source-pin tests for the migration wizard.

The engine (`run_migration` + state machine + verify) is unit-tested
elsewhere; this file pins that the new GTK subprocess actually wires
the engine's primitives into visible widgets (preflight → confirm →
progress → done) and that the Settings tab's "Migrate to another
relay…" button opens the wizard via `vault-migration`.
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


class MigrationWizardSubprocessSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault_migration.py").read_text(encoding="utf-8")
        cls.windows_text = (SRC_ROOT / "windows.py").read_text(encoding="utf-8")

    def test_dispatcher_registers_vault_migration(self) -> None:
        """``vault-migration`` must appear in the dispatcher's choices
        list AND the dispatch table."""
        self.assertIn('"vault-migration"', self.windows_text)
        self.assertIn(
            "from .windows_vault_migration import show_vault_migration",
            self.windows_text,
        )
        self.assertIn('args.window == "vault-migration"', self.windows_text)
        self.assertIn("show_vault_migration(config_dir)", self.windows_text)

    def test_uses_run_migration_with_progress_callback(self) -> None:
        """The wizard drives the engine via ``run_migration`` with a
        live progress callback, not by re-implementing the state
        machine inline."""
        self.assertIn("from .vault.migration.runner import", self.text)
        self.assertIn("run_migration", self.text)
        self.assertIn("MigrationProgress", self.text)
        self.assertIn("progress=_on_progress", self.text)

    def test_uses_migration_preflight_helper(self) -> None:
        """Confirm-page chunk/byte summary comes from the
        ``migration_preflight`` helper landed alongside the wizard."""
        self.assertIn("migration_preflight", self.text)
        self.assertIn("MigrationInventory", self.text)

    def test_clears_previous_relay_url_before_start(self) -> None:
        """§5.M6 fix: the wizard calls ``clear_previous_relay`` so an
        A → B → C migration records ``previous = B``, not the stale A."""
        self.assertIn("from .vault.migration.state import", self.text)
        self.assertIn("clear_previous_relay", self.text)

    def test_writes_previous_relay_to_config_post_commit(self) -> None:
        """``on_committed`` callback updates ``config.server_url`` +
        ``vault_previous_relay_url`` + ``vault_previous_relay_expires_at``
        so the switch-back surface knows about the migration."""
        self.assertIn("vault_previous_relay_url", self.text)
        self.assertIn("vault_previous_relay_expires_at", self.text)
        self.assertIn("on_committed", self.text)

    def test_surfaces_verify_mismatches_inline(self) -> None:
        """Verify failure stops the wizard at an error page and lists
        the offending mismatches — does NOT auto-commit."""
        self.assertIn("verify.matches", self.text)
        self.assertIn("verify.mismatches", self.text)

    def test_handles_edited_vault_limitation(self) -> None:
        """§5.M2: vaults with shard_revision > 1 hit a known idempotency
        gap; preflight surfaces ``has_edited_shards`` so the wizard
        warns the operator before the destructive commit."""
        self.assertIn("has_edited_shards", self.text)
        self.assertIn("§5.M2", self.text)

    def test_target_url_validated_before_continue(self) -> None:
        """Target URL must be HTTP(S) with a host, and must differ
        from the current source. The wizard validates this before any
        network call."""
        self.assertIn("urlparse", self.text)
        self.assertIn("must differ from the current source relay", self.text)


class MigrationTabWiringSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault" / "tab_migration.py").read_text(encoding="utf-8")

    def test_migrate_button_no_longer_disabled(self) -> None:
        """The placeholder ``set_sensitive(False)`` is gone now that
        the wizard ships. Regression guard against re-disabling the
        button if a refactor accidentally re-introduces the old
        placeholder copy."""
        self.assertNotIn("migrate_btn.set_sensitive(False)", self.text)

    def test_migrate_button_spawns_vault_migration_subprocess(self) -> None:
        """Click handler invokes ``python -m src.windows vault-migration``
        with the active config_dir."""
        self.assertIn('"vault-migration"', self.text)
        self.assertIn("subprocess.Popen", self.text)
        self.assertIn("--config-dir=", self.text)

    def test_switch_back_surface_preserved(self) -> None:
        """The post-commit switch-back UI (read previous_relay_url +
        flip server_url back) must keep working alongside the new
        wizard launcher."""
        self.assertIn("can_switch_back", self.text)
        self.assertIn("vault_previous_relay_url", self.text)
        self.assertIn("Switch back to previous relay", self.text)


if __name__ == "__main__":
    unittest.main()
