"""Source checks for the multi-device Find my Device GTK window (M.7)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _find_device_window_source() -> str:
    source = Path(REPO_ROOT, "desktop/src/windows_find_phone.py").read_text()
    # Stop at show_locate_alert so the slice covers only the find-phone
    # window — keeps these tests insulated from the alert-modal copy.
    start = source.index("def show_find_phone(")
    end = source.index("def show_locate_alert(", start)
    return source[start:end]


class FindDeviceWindowSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full_source = Path(REPO_ROOT, "desktop/src/windows_find_phone.py").read_text()
        cls.source = _find_device_window_source()
        cls.tray_source = Path(REPO_ROOT, "desktop/src/tray.py").read_text()

    def test_window_uses_device_wording(self):
        self.assertIn('title="Find my Device"', self.source)
        # Status copy and start-failure copy say "device".
        self.assertIn('"Device is ringing!"', self.source)
        self.assertIn('"Failed to reach device"', self.source)
        self.assertIn('"Track location without alarm (stolen device)"', self.source)
        # Old phone-only wording is gone from the find-device window.
        self.assertNotIn('"Phone is ringing!"', self.source)
        self.assertNotIn('"Failed to reach phone"', self.source)
        self.assertNotIn('title="Find my Phone"', self.source)

    def test_tray_menu_label_renamed(self):
        self.assertIn('"Find my Device"', self.tray_source)
        self.assertNotIn('"Find my Phone"', self.tray_source)

    def test_wire_payload_keeps_find_phone_for_compat(self):
        # D5: keep the wire shape compatible with Android peers that
        # still send fn=find-phone. UI changes "phone" → "device", but
        # the JSON payload key stays.
        self.assertIn('"fn": "find-phone", "action": "stop"', self.source)
        self.assertIn('"fn": "find-phone",', self.source)

    def test_window_has_device_picker_at_top_of_content(self):
        for text in (
            'device_picker, selected_device, paired_devices = _create_device_picker(',
            'title="Find my Device"',
            'subtitle="Connected device"',
            "device_group.add(device_picker)",
            "content.append(device_group)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_set_ui_locks_picker_during_session(self):
        for text in (
            "device_picker.set_sensitive(sliders_enabled and bool(paired_devices))",
            "start_btn.set_sensitive(",
            "selected_device[0] is not None",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")

    def test_start_resolves_target_from_picker_not_first_pair(self):
        for text in (
            "target = selected_device[0]",
            "target_id = target.device_id",
            "base64.b64decode(target.symmetric_key_b64)",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        self.assertNotIn("config.get_first_paired_device()", self.source)

    def test_active_device_marked_after_command_queued(self):
        for text in (
            "ConnectedDeviceRegistry(config).mark_active(",
            'reason="find_device_start"',
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        # Active is set AFTER the queue confirms — not before, so a
        # send_failed start won't move the active pointer.
        active_idx = self.source.index('reason="find_device_start"')
        send_failed_idx = self.source.index('"Failed to reach device"')
        self.assertGreater(
            active_idx,
            send_failed_idx,
            "mark_active must be guarded by the successful-queue branch",
        )

    def test_sender_poll_only_acks_updates_from_selected_target(self):
        for text in (
            "def decode_target_find_device_update(",
            '(raw.get("sender_id") or "") != target_id',
            "MessageType.FIND_PHONE_LOCATION_UPDATE",
            "decoded = decode_target_find_device_update(m, target_id, symmetric_key)",
            "flushed_count += 1",
        ):
            self.assertIn(text, self.source, msg=f"missing: {text!r}")
        self.assertNotIn("for m in stale:\n                    mid = m.get(\"id\")", self.source)


if __name__ == "__main__":
    unittest.main()
