"""Source-level checks for Android pairing state cleanup."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class AndroidPairingStateSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.view_model_source = Path(
            REPO_ROOT,
            "android/app/src/main/kotlin/com/desktopconnector/ui/pairing/PairingViewModel.kt",
        ).read_text()
        cls.screen_source = Path(
            REPO_ROOT,
            "android/app/src/main/kotlin/com/desktopconnector/ui/pairing/PairingScreen.kt",
        ).read_text()

    def test_commit_name_clears_transient_pairing_material_on_complete(self):
        self.assertIn(
            "_state.value = PairingState(stage = PairingStage.COMPLETE)",
            self.view_model_source,
        )
        self.assertNotIn(
            "_state.value = current.copy(stage = PairingStage.COMPLETE)",
            self.view_model_source,
        )

    def test_verification_code_only_renders_during_verifying_stage(self):
        self.assertIn(
            "stage == PairingStage.VERIFYING && verificationCode != null",
            self.screen_source,
        )
        self.assertNotIn(
            "} else if (verificationCode != null) {",
            self.screen_source,
        )


if __name__ == "__main__":
    unittest.main()
