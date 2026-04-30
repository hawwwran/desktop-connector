"""Source-level checks for server pairing-request cleanup."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class ServerPairingCleanupSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller_source = Path(
            REPO_ROOT,
            "server/src/Controllers/PairingController.php",
        ).read_text()
        cls.repository_source = Path(
            REPO_ROOT,
            "server/src/Repositories/PairingRepository.php",
        ).read_text()

    def test_pairing_request_is_idempotent_for_existing_pairs(self):
        for text in (
            "$pairings->findPairing($desktopId, $ctx->deviceId)",
            "$pairings->deleteRequestsBetweenDevices($desktopId, $ctx->deviceId)",
            "Router::json(['status' => 'ok']);",
        ):
            self.assertIn(text, self.controller_source)

    def test_confirm_cleans_pending_requests_between_pair(self):
        self.assertIn(
            "$pairings->deleteRequestsBetweenDevices($ctx->deviceId, $phoneId)",
            self.controller_source,
        )
        for text in (
            "function deleteRequestsBetweenDevices",
            "DELETE FROM pairing_requests",
            "OR (desktop_id = :b2 AND phone_id = :a2)",
        ):
            self.assertIn(text, self.repository_source)


if __name__ == "__main__":
    unittest.main()
