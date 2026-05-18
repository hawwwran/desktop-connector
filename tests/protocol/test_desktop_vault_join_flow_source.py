"""§5.C2 — source-pin tests for the QR-grant claimant + admin flows.

The crypto primitives (qr.py, wrap.py, join_client) are unit-tested
elsewhere; this file pins that the GTK glue actually wires the typed
client + the verification-code derivation + the per-flow gating into
visible widgets.

Two surfaces:

- ``desktop/src/windows_vault_join.py`` — claimant subprocess
  (paste URL + verify code + poll for approval + unwrap + save grant).
- ``desktop/src/windows_vault/grant_device_dialog.py`` — admin
  in-process dialog (mint join-request + render QR + poll for claim
  + show code + role picker + wrap + approve).
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


class ClaimantSubprocessSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault_join.py").read_text(encoding="utf-8")

    def test_dispatcher_registers_vault_join(self) -> None:
        """The windows.py dispatcher must list ``vault-join`` in its
        ``choices`` arg + dispatch table so ``python3 -m src.windows
        vault-join`` reaches :func:`show_vault_join`."""
        windows_text = (SRC_ROOT / "windows.py").read_text(encoding="utf-8")
        self.assertIn('"vault-join"', windows_text)
        self.assertIn("from .windows_vault_join import show_vault_join", windows_text)
        self.assertIn('args.window == "vault-join"', windows_text)
        self.assertIn("show_vault_join(config_dir)", windows_text)

    def test_uses_typed_join_client_not_raw_http(self) -> None:
        """The subprocess uses :mod:`vault.grant.join_client` typed
        wrappers, not raw HTTP, so error mapping stays consistent."""
        self.assertIn("from .vault.grant.join_client import", self.text)
        self.assertIn("claim_join_request", self.text)
        self.assertIn("get_join_request", self.text)

    def test_parses_url_via_qr_primitive(self) -> None:
        """``parse_join_url`` validates scheme + structure + expiry up
        front so an invalid URL is rejected before any HTTP call."""
        self.assertIn("parse_join_url", self.text)
        self.assertIn("VaultGrantQRError", self.text)
        self.assertIn("is_expired", self.text)

    def test_derives_verification_code_locally(self) -> None:
        """Both sides compute the 6-digit code from the X25519 shared
        secret — the URL never carries it. Pinning the use of the
        derive_* primitives ensures a future refactor can't accidentally
        skip the verification step."""
        self.assertIn("derive_shared_secret", self.text)
        self.assertIn("derive_verification_code", self.text)

    def test_unwraps_grant_with_aead_then_saves_locally(self) -> None:
        """Approval path: AEAD-unwrap via ``unwrap_grant_for_claimant``
        with ``expected_vault_id`` + ``expected_claimant_device_id``
        pins so a wrap can't be replayed onto a different device; on
        success, save the grant + persist ``last_known_id``."""
        self.assertIn("unwrap_grant_for_claimant", self.text)
        self.assertIn("expected_vault_id=", self.text)
        self.assertIn("expected_claimant_device_id=", self.text)
        self.assertIn("last_known_id", self.text)

    def test_handles_rejection_and_expiry_states(self) -> None:
        """Both terminal failure states (rejected / expired) surface a
        specific user message rather than silently looping forever."""
        self.assertIn('"rejected"', self.text)
        self.assertIn('"expired"', self.text)

    def test_emits_diagnostics_events(self) -> None:
        """Audit trail for the claimant flow."""
        self.assertIn("vault.grant.claim_sent", self.text)
        self.assertIn("vault.grant.unwrap_succeeded", self.text)

    def test_cancel_scrubs_private_key(self) -> None:
        """The ephemeral X25519 private key is best-effort zeroed when
        the operator closes the wizard."""
        self.assertIn("claimant_priv", self.text)
        # Close handler walks the key bytes and zeroes them.
        self.assertIn("close-request", self.text)


class AdminDialogSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (SRC_ROOT / "windows_vault" / "grant_device_dialog.py").read_text(encoding="utf-8")
        cls.tab_text = (SRC_ROOT / "windows_vault" / "tab_devices.py").read_text(encoding="utf-8")

    def test_tab_devices_offers_grant_button(self) -> None:
        """The Devices tab now has a "Grant a new device…" button that
        opens the dialog."""
        self.assertIn("Grant a new device", self.tab_text)
        self.assertIn(
            "from .grant_device_dialog import build_grant_device_dialog",
            self.tab_text,
        )
        self.assertIn("build_grant_device_dialog(", self.tab_text)

    def test_dialog_mints_ephemeral_keypair_per_grant(self) -> None:
        """Each grant attempt uses a fresh X25519 keypair — never
        reused across calls. ``secrets.token_bytes(32)`` + the standard
        base-point multiplication."""
        self.assertIn("token_bytes", self.text)
        self.assertIn("crypto_scalarmult_base", self.text)

    def test_dialog_creates_then_polls_join_request(self) -> None:
        """Flow: ``create_join_request`` → render QR → poll
        ``get_join_request`` until ``state=claimed``."""
        self.assertIn("create_join_request", self.text)
        self.assertIn("get_join_request", self.text)
        self.assertIn('fetched.state == "claimed"', self.text)

    def test_dialog_renders_qr_and_url(self) -> None:
        """Both the QR code and the URL itself must be visible — the
        plaintext URL is the fallback when QR scanning isn't available
        on the claimant device."""
        self.assertIn("make_join_url", self.text)
        self.assertIn("qrcode", self.text)
        self.assertIn("url_view", self.text)

    def test_dialog_shows_verification_code_from_shared_secret(self) -> None:
        """Verification code derived locally from the X25519 shared
        secret with the claimant's pubkey — no relay round-trip."""
        self.assertIn("derive_shared_secret", self.text)
        self.assertIn("derive_verification_code", self.text)

    def test_dialog_role_picker_pins_v1_role_set(self) -> None:
        """Spec §3.3 role set is pinned — drift here changes the
        protocol surface the admin can grant."""
        for role in ("read-only", "browse-upload", "sync", "admin"):
            with self.subTest(role=role):
                self.assertIn(f'"{role}"', self.text)

    def test_dialog_approval_wraps_then_posts(self) -> None:
        """Approve path: ``wrap_grant_for_claimant`` produces the AEAD
        envelope, then ``approve_join_request`` ships it. The role +
        wrapped envelope must be threaded through together (server
        idempotency checks both)."""
        self.assertIn("wrap_grant_for_claimant", self.text)
        self.assertIn("approve_join_request", self.text)
        self.assertIn("approved_role=", self.text)
        self.assertIn("wrapped_vault_grant=", self.text)

    def test_dialog_cancel_deletes_pending_join_request(self) -> None:
        """Abandoned join-requests sit in the per-vault budget (cap 5);
        the dialog explicitly DELETEs on cancel so a flaky workflow
        doesn't pile up rows on the relay."""
        self.assertIn("reject_join_request", self.text)
        self.assertIn("_delete_join_request_best_effort", self.text)

    def test_dialog_emits_diagnostics_events(self) -> None:
        self.assertIn("vault.grant.join_request_created", self.text)
        self.assertIn("vault.grant.approved", self.text)


class TraySubmenuSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tray_text = (SRC_ROOT / "tray" / "vault_submenu.py").read_text(encoding="utf-8")
        cls.ui_state_text = (SRC_ROOT / "vault" / "ui" / "ui_state.py").read_text(encoding="utf-8")

    def test_join_vault_token_in_no_vault_branch(self) -> None:
        """The ``join_vault`` token must appear in the entries list
        returned for the no-local-vault case, alongside ``create_vault``
        and ``import_vault``."""
        self.assertIn('"join_vault"', self.ui_state_text)
        # Branch where vault doesn't exist locally.
        self.assertIn(
            '["create_vault", "import_vault", "join_vault"]',
            self.ui_state_text,
        )

    def test_tray_menu_item_wires_subprocess(self) -> None:
        """The tray submenu's "Add this device to a vault…" entry must
        spawn the ``vault-join`` subprocess."""
        self.assertIn("Add this device to a vault…", self.tray_text)
        self.assertIn("vault-join", self.tray_text)
        self.assertIn("_spawn_vault_join", self.tray_text)


if __name__ == "__main__":
    unittest.main()
