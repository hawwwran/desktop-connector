"""§6.H2 — source-pin tests for the Devices tab.

Mirrors ``test_desktop_vault_danger_zone_source.py`` for the new
``tab_devices.py`` module. The behavioural risks (typed client
parsing, server-side admin gate) are unit-tested at function /
controller layer; what this file pins is "the GTK builder actually
wires the locked copy + the admin-role gate + the typed client into
visible widgets."
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()


PKG_DIR = Path(
    os.path.dirname(__file__) or "."
).resolve().parent.parent / "desktop" / "src" / "windows_vault"


# Spec §14 locked wording. Pinned VERBATIM — the architecture doc
# §3.3 / §14 row guarantees the user is told that already-downloaded
# plaintext on the revoked device cannot be erased. A future copy
# rewrite that drops the second sentence would silently make the UI
# less honest about revocation's scope.
LOCKED_REVOKE_COPY = (
    "Revoking this device prevents future Vault access. "
    "It cannot erase data already copied to that device."
)


class DevicesTabSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tab_text = (PKG_DIR / "tab_devices.py").read_text(encoding="utf-8")
        cls.main_text = (PKG_DIR / "main_window.py").read_text(encoding="utf-8")

    def test_locked_revoke_copy_matches_spec_3_3_verbatim(self) -> None:
        """The §14 locked copy must appear unchanged. A future copy
        edit that drops or rephrases the data-already-copied caveat
        would weaken the user's understanding of what revoke does.

        Imported as a constant rather than substring-matched on
        source text so an implementation that splits the literal
        across continuation lines still pins the runtime value.
        """
        from src.windows_vault.tab_devices import REVOKE_LOCKED_COPY
        self.assertEqual(REVOKE_LOCKED_COPY, LOCKED_REVOKE_COPY)

    def test_revoke_flow_wires_fresh_unlock_then_admin_check(self) -> None:
        """Double-gate per ``tab_danger.py`` pattern: fresh-unlock
        first, then admin role verified via the relay's caller_role
        before the type-to-confirm dialog opens."""
        self.assertIn("require_fresh_unlock_or_prompt", self.tab_text)
        self.assertIn("caller_role", self.tab_text)
        self.assertIn("get_header", self.tab_text)

    def test_revoke_flow_wires_typed_confirm(self) -> None:
        """Revoke dialog reuses the typed-confirm pattern from
        ``confirm_vault_clear_text_matches`` — same UX shape as
        Clear vault / Schedule purge."""
        self.assertIn("confirm_vault_clear_text_matches", self.tab_text)

    def test_revoke_flow_uses_typed_client(self) -> None:
        """The tab dispatches via :mod:`vault.grant.client`, not by
        constructing raw HTTP requests inline."""
        self.assertIn("from ..vault.grant.client import", self.tab_text)
        self.assertIn("revoke_device_grant", self.tab_text)
        self.assertIn("list_device_grants", self.tab_text)

    def test_revoke_button_pre_disables_for_caller_row(self) -> None:
        """Self-revoke is blocked server-side, but the UI also
        pre-disables the Revoke button on the caller's row to prevent
        an obvious foot-gun click and surface the recommended action
        (Disconnect this device)."""
        self.assertIn("is_caller", self.tab_text)
        self.assertIn("Disconnect this device", self.tab_text)

    def test_tab_polls_on_visibility(self) -> None:
        """Reactive refresh on tab map + every 30 s while visible;
        the timer is cleared on unmap so a hidden tab doesn't keep
        hitting the relay."""
        self.assertIn('connect("map"', self.tab_text)
        self.assertIn('connect("unmap"', self.tab_text)
        self.assertIn("timeout_add_seconds", self.tab_text)
        self.assertIn("POLL_INTERVAL_S", self.tab_text)

    def test_revoke_emits_diagnostics_event(self) -> None:
        """Audit trail per architecture doc §14: every revoke emits
        ``vault.device.revoked`` so the operator can correlate the
        in-app action with the server-side log entry."""
        self.assertIn("vault.device.revoked", self.tab_text)

    def test_typed_errors_surface_inline(self) -> None:
        """The three known revoke failure modes each have a tailored
        UI message — generic ``humanize`` fall-through is reserved for
        unexpected exceptions."""
        self.assertIn("CannotRevokeSelfError", self.tab_text)
        self.assertIn("DeviceGrantNotFoundError", self.tab_text)
        self.assertIn("DeviceGrantsAuthError", self.tab_text)


class MainWindowSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (PKG_DIR / "main_window.py").read_text(encoding="utf-8")

    def test_main_window_wires_real_devices_tab(self) -> None:
        """The placeholder loop in ``main_window.py`` no longer
        creates a placeholder for ``devices``; instead the real
        ``build_devices_tab`` is wired in."""
        self.assertIn("from .tab_devices import build_devices_tab", self.text)
        self.assertIn(
            'add_tab("devices", "Devices", build_devices_tab(ctx, win))',
            self.text,
        )

    def test_placeholder_loop_no_longer_includes_devices(self) -> None:
        """A regression guard: the previous placeholder loop included
        ``("devices", "Devices")``; with the real tab landed, that
        entry must be gone so we don't create two tabs with the same
        name (which would crash ``Gtk.Stack``)."""
        self.assertNotIn('("devices", "Devices")', self.text)


if __name__ == "__main__":
    unittest.main()
