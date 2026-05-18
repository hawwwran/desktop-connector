"""§5.H3 — source-pin tests for the rotation wizard.

The crypto + HTTP primitives (access_rotation library, rotate_client)
are unit-tested elsewhere; this file pins that the GTK wizard
actually wires the gating (two checkboxes, passphrase re-verify
before rotate, save-kit-before-close, atomic grant-store update)
into visible widgets.
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


class RotateWizardSubprocessSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault_rotate.py").read_text(encoding="utf-8")
        cls.windows_text = (SRC_ROOT / "windows.py").read_text(encoding="utf-8")

    def test_dispatcher_registers_vault_rotate(self) -> None:
        self.assertIn('"vault-rotate"', self.windows_text)
        self.assertIn(
            "from .windows_vault_rotate import show_vault_rotate",
            self.windows_text,
        )
        self.assertIn('args.window == "vault-rotate"', self.windows_text)
        self.assertIn("show_vault_rotate(config_dir)", self.windows_text)

    def test_confirm_continue_requires_both_checkboxes(self) -> None:
        """Continue is disabled until both safety checkboxes are
        ticked. Without this gate the wizard would let a user click
        through without acknowledging the irreversible kit
        invalidation."""
        self.assertIn("CheckButton", self.text)
        # Both checkboxes referenced + both required to enable continue.
        self.assertIn("cb_kits", self.text)
        self.assertIn("cb_save", self.text)
        self.assertIn("cb_kits.get_active() and cb_save.get_active()", self.text)
        self.assertIn(
            "confirm_continue.set_sensitive(False)", self.text,
        )

    def test_verify_kit_before_rotating(self) -> None:
        """Operator must produce kit + passphrase that successfully
        Argon2id-derive the master key BEFORE we POST rotate. This
        ensures the new kit's recovery_secret + envelope_meta match
        what's encrypted on disk."""
        self.assertIn("verify_recovery_kit", self.text)
        self.assertIn("parse_recovery_kit_file", self.text)
        self.assertIn("recovery_envelope_meta_from_json", self.text)

    def test_atomic_local_grant_update_after_rotation(self) -> None:
        """On rotation success: open_default_grant_store + save new
        VaultGrant carrying (vault_id, master_key, new_secret).
        Pre-existing access secret stops working the instant the
        relay returns 200; the local cache MUST flip in the same
        worker before any subsequent op runs."""
        self.assertIn("open_default_grant_store", self.text)
        self.assertIn("VaultGrant.from_bytes", self.text)
        # The master_key is held briefly + zeroized after the swap.
        self.assertIn("master_key", self.text)
        self.assertIn("zero", self.text)

    def test_close_blocked_until_kit_saved(self) -> None:
        """Save-kit page disables Close until the new kit is written;
        close-request handler surfaces a confirmation dialog if the
        operator tries to force-close. Without this guard the user
        could leave the wizard with a rotated relay-side secret but
        no kit on disk — vault becomes permanently unrecoverable."""
        self.assertIn("save_close.set_sensitive(False)", self.text)
        self.assertIn("close-request", self.text)
        self.assertIn("Close without saving the kit?", self.text)
        self.assertIn("kit_saved", self.text)

    def test_new_kit_carries_same_recovery_secret_and_envelope(self) -> None:
        """Post-rotation kit reuses the existing recovery_secret +
        envelope_meta (the passphrase-derived material is unchanged
        by access-secret rotation); only the vault_access_secret
        gets swapped."""
        self.assertIn("write_recovery_kit_file", self.text)
        self.assertIn("recovery_secret=state[", self.text)
        self.assertIn("vault_access_secret=state[\"new_secret\"]", self.text)
        self.assertIn("recovery_envelope_meta=state[", self.text)

    def test_uses_typed_rotate_client(self) -> None:
        self.assertIn(
            "from .vault.grant.rotate_client import", self.text,
        )
        self.assertIn("rotate_access_secret", self.text)

    def test_emits_diagnostics_events(self) -> None:
        self.assertIn("vault.rotate.started", self.text)
        self.assertIn("vault.rotate.server_committed", self.text)
        self.assertIn("vault.rotate.kit_saved", self.text)
        self.assertIn("vault.rotate.kit_save_failed", self.text)


class RecoveryTabWiringSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault" / "tab_recovery.py").read_text(encoding="utf-8")

    def test_update_recovery_button_no_longer_force_disabled(self) -> None:
        """The placeholder ``set_sensitive(False)`` + "not implemented"
        tooltip are gone. The button is now sensitive when a vault is
        loaded."""
        # Old absolute-False call is gone.
        self.assertNotIn(
            'update_recovery_btn.set_sensitive(False)', self.text,
        )
        self.assertNotIn(
            "Recovery-material rotation is not implemented yet", self.text,
        )

    def test_button_spawns_vault_rotate_subprocess(self) -> None:
        self.assertIn('"vault-rotate"', self.text)
        self.assertIn("subprocess.Popen", self.text)
        self.assertIn("--config-dir=", self.text)


if __name__ == "__main__":
    unittest.main()
