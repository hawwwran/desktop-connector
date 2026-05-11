"""F-U15: tray's ``_local_vault_exists`` cross-checks the grant store.

The pre-F-U15 heuristic returned True whenever
``config['vault']['last_known_id']`` was non-empty. That admits a
stale-config race: if a grant artifact gets deleted out from under
the config (manual keyring purge, OS-keyring switch, half-published
wizard run), the tray would still show "Open Vault…" and clicking
it leads to a doomed unlock flow. Cross-checking against the grant
store flips the submenu back to Create / Import — the right
recovery affordance.

Tests cover:
- ``last_known_id`` empty → False (legacy behaviour preserved).
- ``last_known_id`` set + grant exists → True.
- ``last_known_id`` set + grant *missing* → False (the F-U15 race).

The dead helper at ``windows_vault.py:_local_vault_exists`` is also
asserted gone — it had no callers and just duplicated the heuristic.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.history import TransferHistory  # noqa: E402
import src.vault.grant.grant as vault_grant  # noqa: E402
from src.vault.grant.grant import (  # noqa: E402
    KeyringUnavailable,
    fallback_grant_path,
)
from src.tray import TrayApp  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"


class TrayLocalVaultExistsTests(unittest.TestCase):
    """Behavioural — instantiate a TrayApp and prod the method."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-tray-local-vault-"))
        self.config = Config(self.tmp)
        # Ensure tests don't touch a real keyring even if the dev box
        # has one configured.
        self._original_open_default = vault_grant.KeyringGrantStore.open_default
        vault_grant.KeyringGrantStore.open_default = classmethod(
            lambda cls: (_ for _ in ()).throw(KeyringUnavailable("test forced"))
        )
        self.tray = TrayApp(
            connection=MagicMock(),
            poller=MagicMock(),
            api=MagicMock(),
            config=self.config,
            crypto=MagicMock(),
            history=TransferHistory(self.tmp),
            save_dir=self.tmp,
            platform=MagicMock(),
        )

    def tearDown(self) -> None:
        vault_grant.KeyringGrantStore.open_default = self._original_open_default
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_last_known_id(self, vault_id: str) -> None:
        self.config._data["vault"] = {"last_known_id": vault_id}
        self.config.save()

    def _drop_file_grant(self, vault_id: str) -> Path:
        path = fallback_grant_path(self.tmp, vault_id)
        path.write_text("opaque envelope", encoding="utf-8")
        return path

    def test_returns_false_when_no_last_known_id(self) -> None:
        # No vault block → False (unchanged from pre-F-U15).
        self.assertFalse(self.tray._local_vault_exists())

    def test_returns_false_when_last_known_id_empty_string(self) -> None:
        self.config._data["vault"] = {"last_known_id": ""}
        self.config.save()
        self.assertFalse(self.tray._local_vault_exists())

    def test_returns_true_when_id_set_and_grant_present(self) -> None:
        self._set_last_known_id(VAULT_ID)
        self._drop_file_grant(VAULT_ID)
        self.assertTrue(self.tray._local_vault_exists())

    def test_f_u15_race_grant_dropped_returns_false(self) -> None:
        """The F-U15 race scenario: ``last_known_id`` lingers in
        config but the grant artifact is gone. The tray must report
        False so the submenu offers Create / Import instead of
        Open / Sync / Settings (which would lead to a doomed unlock)."""
        self._set_last_known_id(VAULT_ID)
        # Don't drop a grant artifact — the F-U15 race.
        self.assertFalse(self.tray._local_vault_exists())

    def test_picks_up_grant_after_drop(self) -> None:
        # Tray refreshes its view of vault existence on every check;
        # dropping a grant file while the tray is running must flip
        # the result without re-instantiation.
        self._set_last_known_id(VAULT_ID)
        self.assertFalse(self.tray._local_vault_exists())
        self._drop_file_grant(VAULT_ID)
        self.assertTrue(self.tray._local_vault_exists())

    def test_picks_up_id_after_wizard_subprocess_writes_it(self) -> None:
        # The wizard runs in a separate subprocess and writes
        # last_known_id to disk. The tray's reload-on-each-check
        # behaviour (preserved through the F-U15 fix) means the
        # tray sees the new id without restart, IF the grant exists.
        self._drop_file_grant(VAULT_ID)
        # Simulate the wizard subprocess writing config.json on disk.
        wizard_config = Config(self.tmp)
        wizard_config._data["vault"] = {"last_known_id": VAULT_ID}
        wizard_config.save()
        # Tray's own Config object should pick this up via reload.
        self.assertTrue(self.tray._local_vault_exists())


class TrayLocalVaultExistsSourcePins(unittest.TestCase):
    """F-U15 source pins — keep the cross-check from getting reverted
    to the bare ``last_known_id`` heuristic."""

    def test_tray_imports_local_vault_grant_exists(self) -> None:
        # tray.py is now a package; the helper lives in tray/vault_submenu.py.
        source = Path(REPO_ROOT, "desktop/src/tray/vault_submenu.py").read_text(encoding="utf-8")
        self.assertIn(
            "from ..vault.grant.grant import local_vault_grant_exists",
            source,
        )

    def test_tray_threads_vault_id_through_helper(self) -> None:
        source = Path(REPO_ROOT, "desktop/src/tray/vault_submenu.py").read_text(encoding="utf-8")
        self.assertIn(
            "return local_vault_grant_exists(self.config.config_dir, vault_id)",
            source,
        )

    def test_dead_local_vault_exists_helper_removed(self) -> None:
        # The duplicate helper at windows_vault.py was unreferenced.
        # Asserting it stays gone keeps a future contributor from
        # re-adding it (and accidentally drifting the heuristic).
        pkg = Path(REPO_ROOT, "desktop/src/windows_vault")
        source = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py"))
        )
        self.assertNotIn("def _local_vault_exists(config) -> bool:", source)


if __name__ == "__main__":
    unittest.main()
