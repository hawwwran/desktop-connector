"""Local Vault disconnect behavior."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.vault_local import disconnect_local_vault  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"


class VaultDisconnectTests(unittest.TestCase):
    def test_disconnect_forgets_local_vault_but_preserves_active_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp))
            config._data["vault"] = {
                "active": True,
                "last_known_id": VAULT_ID,
                "cached_local_state": "remove-me",
            }
            config.save()

            deleted: list[tuple[Path, str]] = []
            disconnected = disconnect_local_vault(
                config,
                grant_deleter=lambda config_dir, vault_id: deleted.append((config_dir, vault_id)),
            )

            self.assertEqual(disconnected, VAULT_ID)
            self.assertEqual(deleted, [(Path(tmp), VAULT_ID)])

            reopened = Config(Path(tmp))
            self.assertEqual(reopened._data.get("vault"), {"active": True})
            self.assertTrue(reopened.vault_active)

    def test_disconnect_removes_pending_local_vault_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            for name in ("vault_migration.json", "vault_pending_purges.json"):
                (config_dir / name).write_text("{}", encoding="utf-8")

            config = Config(config_dir)
            config._data["vault"] = {"active": False, "last_known_id": VAULT_ID}
            config.save()

            disconnect_local_vault(config, grant_deleter=lambda _config_dir, _vault_id: None)

            self.assertEqual(Config(config_dir)._data.get("vault"), {"active": False})
            self.assertFalse((config_dir / "vault_migration.json").exists())
            self.assertFalse((config_dir / "vault_pending_purges.json").exists())


if __name__ == "__main__":
    unittest.main()
