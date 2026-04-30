"""Diagnostics-event source pinning for desktop-to-desktop pairing (M.11).

Source-only tests: the GTK pairing window logs a small set of canonical
event names defined in `docs/diagnostics.events.md`, and never logs the
raw pairing key, decoded contents, symkey, or verification code. Pin
both the positive (event names appear) and the negative (key/code
strings never appear as log args) contracts.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


def _windows_source() -> str:
    return Path(REPO_ROOT, "desktop/src/windows.py").read_text()


def _pairing_key_source() -> str:
    return Path(REPO_ROOT, "desktop/src/pairing_key.py").read_text()


_EXPECTED_EVENTS = (
    "pairing.key.shown",
    "pairing.key.exported",
    "pairing.key.import_parse_failed",
    "pairing.key.import_self_pair_refused",
    "pairing.key.import_relay_mismatched",
    "pairing.key.import_already_paired_refused",
    "pairing.key.import_request_failed",
    "pairing.request.sent_as_joiner",
)


class PairingKeyEventsSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _windows_source()
        cls.codec_source = _pairing_key_source()

    def test_all_canonical_events_emitted(self):
        for event in _EXPECTED_EVENTS:
            self.assertIn(
                event, self.source,
                msg=f"missing event vocabulary: {event!r}",
            )

    def test_no_raw_payload_in_log_arguments(self):
        # Negative-grep contract: log statements in the pairing flow
        # must not feed the encoded `text` or the decoded handshake
        # contents into a logging format string. We pin the
        # most likely accidental patterns; anyone adding new logging
        # touching pairing should keep this contract.
        forbidden_patterns = (
            re.compile(r"log\.(info|warning|debug|error|exception)\([^)]*%s[^)]*text\b"),
            re.compile(r"log\.(info|warning|debug|error|exception)\([^)]*%s[^)]*shared_key"),
            re.compile(r"log\.(info|warning|debug|error|exception)\([^)]*%s[^)]*verification_code"),
        )
        for pattern in forbidden_patterns:
            self.assertIsNone(
                pattern.search(self.source),
                msg=f"raw pairing material in log arg: {pattern.pattern}",
            )
            self.assertIsNone(
                pattern.search(self.codec_source),
                msg=f"raw pairing material in log arg: {pattern.pattern}",
            )

    def test_relay_mismatch_logs_hostname_only(self):
        # The relay-mismatch branch derives `local_host`/`remote_host`
        # from urlsplit().netloc before logging. Pin the call site so
        # a regression that drops the netloc reduction is caught.
        self.assertIn("urlsplit(exc.local).netloc", self.source)
        self.assertIn("urlsplit(exc.remote).netloc", self.source)
        self.assertIn(
            "pairing.key.import_relay_mismatched local=%s remote=%s",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
