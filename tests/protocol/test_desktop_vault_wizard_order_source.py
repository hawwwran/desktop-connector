"""Source pin for the vault-wizard's defer-the-relay-create ordering.

T8-pre safety net: the wizard MUST do local persistence (grant save)
before the relay POST so a local-persistence failure can't leave an
orphaned vault row on the server. This pin breaks loudly if the
order regresses.

Order required:
    1. Vault.prepare_new(...)        # pure crypto, no relay
    2. save_local_vault_grant(...)   # local
    3. vault.publish_initial(relay)  # first relay write
    4. config.save()                 # last_known_id + envelope meta
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class VaultWizardOrderSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(
            REPO_ROOT, "desktop/src/windows_vault.py",
        ).read_text()

    def test_wizard_uses_prepare_plus_publish_split(self) -> None:
        # Old one-shot create_new (which couldn't defer the relay
        # POST) must not be back in the wizard.
        self.assertNotIn("Vault.create_new(", self.source)
        # Both halves of the new flow are present.
        for text in (
            "Vault.prepare_new(",
            "vault.publish_initial(relay)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_wizard_saves_grant_before_publishing(self) -> None:
        save_pos = self.source.index("save_local_vault_grant(config_dir, config, vault)")
        publish_pos = self.source.index("vault.publish_initial(relay)")
        self.assertLess(
            save_pos, publish_pos,
            "save_local_vault_grant must precede publish_initial — "
            "the whole point of the prepare/publish split.",
        )

    def test_wizard_sets_last_known_id_only_after_publish(self) -> None:
        # Phase 4: config.save with last_known_id runs in
        # _commit_after_publish, which is called only on the success
        # path of phase 3.
        commit_pos = self.source.index("def _commit_after_publish(vault)")
        publish_pos = self.source.index("vault.publish_initial(relay)")
        for text in (
            'config._data["vault"]["last_known_id"] = vault.vault_id',
            'config._data["vault"]["recovery_envelope_meta"]',
            "state[\"completed_successfully\"] = True",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
            self.assertGreater(
                self.source.index(text), commit_pos,
                f"{text!r} must live inside _commit_after_publish",
            )
        # Sanity: commit helper itself sits before any publish_initial
        # call site (it's defined before perform_create / on_retry_publish).
        self.assertLess(commit_pos, publish_pos)

    def test_publish_failure_offers_retry_button(self) -> None:
        for text in (
            'retry_publish_btn = Gtk.Button(',
            'label="Retry publish"',
            "retry_publish_btn.set_visible(True)",
            "def on_retry_publish(_btn)",
            "vault.has_pending_publish",
            "retry_publish_btn.connect(\"clicked\", on_retry_publish)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_cancel_after_grant_save_drops_dangling_grant(self) -> None:
        # The cancel cleanup must call delete_local_grant_artifacts
        # exactly when grant_saved is true but published is false.
        for text in (
            "state.get(\"grant_saved\") and not state.get(\"published\")",
            "from .vault_grant import delete_local_grant_artifacts",
            "delete_local_grant_artifacts(Path(config.config_dir), state[\"vault_id\"])",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")


if __name__ == "__main__":
    unittest.main()
