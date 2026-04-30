"""Source checks for the multi-device pairing GTK window (M.5)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _pairing_window_source() -> str:
    source = Path(REPO_ROOT, "desktop/src/windows.py").read_text()
    start = source.index("def show_pairing(")
    end = source.index("# ─── Find My Device Window", start)
    return source[start:end]


class PairingWindowSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _pairing_window_source()

    def test_window_uses_device_wording(self):
        self.assertIn('title="Pair with Device"', self.source)
        self.assertIn(
            'label="Scan this QR code with your device"', self.source
        )
        self.assertIn(
            'label="Waiting for device to scan..."', self.source
        )
        self.assertIn('"Device connected:', self.source)
        self.assertNotIn("Pair with Phone", self.source)
        self.assertNotIn("Waiting for phone to scan", self.source)

    def test_naming_step_is_present(self):
        for text in (
            'stack.add_named(qr_box, "qr")',
            'stack.add_named(naming_box, "naming")',
            'label="Name this device"',
            "Adw.EntryRow(title=\"Name\")",
            'stack.set_visible_child_name("naming")',
            "registry.next_default_name()",
            "registry.validate_unique_name(name_row.get_text())",
            "DuplicateDeviceNameError",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_save_persists_chosen_name_and_marks_active(self):
        for text in (
            "config.add_paired_device(",
            "name=normalized,",
            "api.confirm_pairing(info[\"phone_id\"])",
            "registry.mark_active(info[\"phone_id\"], reason=\"paired\")",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        # Legacy hard-coded "Phone-XXXXXXXX" name must be gone.
        self.assertNotIn("Phone-{info['phone_id'][:8]}", self.source)

    def test_qr_page_offers_pair_desktop_switch(self):
        # M.11: from the QR (phone) page the user can switch into
        # desktop-pair mode without leaving the window.
        for text in (
            'label="Pair desktop instead"',
            'stack.set_visible_child_name("desktop")',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_desktop_mode_has_all_four_pairing_key_buttons(self):
        # M.11: desktop-mode hub exposes Show / Export / Enter / Import
        # plus a "Pair phone instead" swap-back button. Each row is an
        # Adw.ActionRow tagged activatable=True so a single click fires
        # `activated`.
        for text in (
            'stack.add_named(desktop_box, "desktop")',
            'label="Pair phone instead"',
            'title="Show pairing key"',
            'title="Export pairing key"',
            'title="Enter pairing key"',
            'title="Import pairing key"',
            'show_key_row.connect("activated"',
            'export_key_row.connect("activated"',
            'enter_key_row.connect("activated"',
            'import_key_row.connect("activated"',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_desktop_mode_keeps_a_status_label_for_incoming_pair(self):
        # The desktop hub watches the same poll loop the QR page does;
        # an incoming pair request shows up as a status update + an
        # auto-switch back to QR for the verification step.
        for text in (
            'desktop_status = Gtk.Label',
            "Incoming pair request",
            'stack.set_visible_child_name("qr")',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_poll_ignores_requests_from_already_paired_devices(self):
        # Stale unclaimed relay rows should not resurrect the previous
        # verification code when the pairing window is opened again.
        for text in (
            "paired_ids = set(config.paired_devices.keys())",
            'if req["phone_id"] in paired_ids:',
            "pairing.request.ignored_already_paired",
            "continue",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_joiner_page_uses_pairing_key_helpers(self):
        for text in (
            'stack.add_named(join_box, "join")',
            "begin_joiner_session(text, surface=",
            "decode_pairing_key(text)",
            "validate_for_join(key, config=config, crypto=crypto)",
            "begin_join(",
            "send_pairing_request=api.send_pairing_request",
            "joiner_handshake[0] = handshake",
            'stack.set_visible_child_name("join")',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_save_branches_on_role_for_joiner_vs_inviter(self):
        # The naming-step save handler picks the right persistence
        # path based on which side of the pair we're on.
        for text in (
            'role[0] == "joiner"',
            "complete_join(",
            "on_synced=lambda: sync_file_manager_targets(config)",
            "config.add_paired_device(",
            "api.confirm_pairing(info[\"phone_id\"])",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_export_writes_dcpair_with_restrictive_perms(self):
        # The pairing key on disk is identity material — chmod 0o600.
        for text in (
            'dialogs.save_file(',
            'file_types=(("Pairing key", "*.dcpair"),),',
            "_os.chmod(tmp, 0o600)",
            "tmp.replace(chosen)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")


if __name__ == "__main__":
    unittest.main()
