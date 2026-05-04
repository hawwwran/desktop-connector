"""T12.3 — Ransomware / mass-change detector for binding sync."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_ransomware_detector import (  # noqa: E402
    ACTION_KEEP_PAUSED, ACTION_RESUME, ACTION_REVIEW, ACTION_ROLLBACK,
    BANNER_ACTIONS, BANNER_BODY, BANNER_TITLE,
    DetectorThresholds, DetectorVerdict,
    MAX_EVENTS_PER_WINDOW, RENAME_RATIO_MIN_EVENTS, RENAME_RATIO_THRESHOLD,
    RansomwareDetector, WINDOW_SECONDS,
    banner_action_for,
)


class ConstantsTests(unittest.TestCase):
    def test_a15_thresholds_match_t0_spec(self) -> None:
        self.assertEqual(MAX_EVENTS_PER_WINDOW, 200)
        self.assertEqual(WINDOW_SECONDS, 300.0)
        self.assertEqual(RENAME_RATIO_THRESHOLD, 0.5)
        self.assertGreater(RENAME_RATIO_MIN_EVENTS, 0)

    def test_banner_text_matches_3_15_verbatim(self) -> None:
        # §3.15 verbatim — these strings are user-facing copy and they
        # must match the spec on the docstring level too.
        self.assertEqual(BANNER_TITLE, "Suspicious mass change detected")
        self.assertEqual(
            BANNER_BODY,
            "Vault sync has been paused for this folder. Review changes before uploading.",
        )

    def test_action_vocabulary_matches_t0_decision_text(self) -> None:
        # §T0 decisions enumerate exactly four user actions.
        self.assertEqual(set(BANNER_ACTIONS), {
            "Review changes", "Rollback to previous version",
            "Resume sync", "Keep paused",
        })
        # Constants and tuple stay in sync.
        self.assertIn(ACTION_REVIEW, BANNER_ACTIONS)
        self.assertIn(ACTION_ROLLBACK, BANNER_ACTIONS)
        self.assertIn(ACTION_RESUME, BANNER_ACTIONS)
        self.assertIn(ACTION_KEEP_PAUSED, BANNER_ACTIONS)

    def test_banner_action_for_rejects_unknown(self) -> None:
        self.assertEqual(banner_action_for(ACTION_REVIEW), ACTION_REVIEW)
        with self.assertRaises(ValueError):
            banner_action_for("Continue anyway")


class DetectorBehaviorTests(unittest.TestCase):
    def test_under_threshold_does_not_trip(self) -> None:
        det = RansomwareDetector(binding_id="rb_v1_a")
        for i in range(10):
            v = det.record(kind="modify", path=f"f{i}", now=float(i))
            self.assertFalse(v.tripped)

    def test_200_modifies_in_5_minutes_trips(self) -> None:
        det = RansomwareDetector(binding_id="rb_v1_b")
        verdict: DetectorVerdict | None = None
        for i in range(MAX_EVENTS_PER_WINDOW):
            verdict = det.record(kind="modify", path=f"f{i}", now=float(i))
        self.assertIsNotNone(verdict)
        assert verdict is not None  # mypy
        self.assertTrue(verdict.tripped)
        self.assertEqual(verdict.total_events, MAX_EVENTS_PER_WINDOW)
        self.assertIn("too_many_changes", verdict.reason)

    def test_events_outside_window_do_not_count(self) -> None:
        det = RansomwareDetector(binding_id="rb_v1_c")
        # Burst at t=0 — fills nearly to the threshold.
        for i in range(MAX_EVENTS_PER_WINDOW - 1):
            det.record(kind="modify", path=f"f{i}", now=0.0)
        # Wait past the 5-minute window before the next event.
        v = det.record(
            kind="modify", path="latest", now=WINDOW_SECONDS + 1.0,
        )
        # Old burst evicted; only "latest" remains.
        self.assertFalse(v.tripped)
        self.assertEqual(v.total_events, 1)

    def test_high_rename_ratio_trips(self) -> None:
        det = RansomwareDetector(binding_id="rb_v1_d")
        # 21 events: 11 renames + 10 modifies → 11/21 ≈ 0.52, above threshold
        # of 0.5, and total ≥ rename_ratio_min_events.
        for i in range(11):
            det.record(kind="rename", path=f"r{i}", now=float(i))
        for i in range(10):
            v = det.record(kind="modify", path=f"m{i}", now=11.0 + i)
        self.assertTrue(v.tripped)
        self.assertIn("rename_ratio_high", v.reason)

    def test_reset_clears_window(self) -> None:
        det = RansomwareDetector(binding_id="rb_v1_e")
        for i in range(MAX_EVENTS_PER_WINDOW):
            det.record(kind="modify", path=f"f{i}", now=float(i))
        self.assertTrue(det.verdict(now=float(MAX_EVENTS_PER_WINDOW)).tripped)
        det.reset()
        self.assertEqual(det.event_count(), 0)
        self.assertFalse(det.verdict(now=0.0).tripped)

    def test_unknown_kind_raises(self) -> None:
        det = RansomwareDetector(binding_id="rb_v1_f")
        with self.assertRaises(ValueError):
            det.record(kind="ignite", path="x.txt", now=0.0)  # type: ignore[arg-type]

    def test_custom_thresholds_lower_bar(self) -> None:
        det = RansomwareDetector(
            binding_id="rb_v1_g",
            thresholds=DetectorThresholds(
                window_seconds=60.0, max_events=5,
                rename_ratio_threshold=0.5, rename_ratio_min_events=4,
            ),
        )
        for i in range(5):
            v = det.record(kind="modify", path=f"f{i}", now=float(i))
        self.assertTrue(v.tripped)


class IntegrationWithBindingStoreTests(unittest.TestCase):
    """End-to-end: 200 modifies → store.update_binding_state(state='paused')."""

    def test_threshold_trip_flips_binding_to_paused(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path
        from src.vault_bindings import VaultBindingsStore
        from src.vault_cache import VaultLocalIndex

        tmpdir = Path(tempfile.mkdtemp(prefix="vault_ransom_int_"))
        try:
            saved_xdg = os.environ.get("XDG_CACHE_HOME")
            os.environ["XDG_CACHE_HOME"] = str(tmpdir / "xdg_cache")
            try:
                index = VaultLocalIndex(tmpdir / "config")
                store = VaultBindingsStore(index.db_path)
                binding = store.create_binding(
                    vault_id="ABCD2345WXYZ",
                    remote_folder_id="rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                    local_path=str(tmpdir / "binding"),
                )
                store.update_binding_state(
                    binding.binding_id, state="bound",
                    sync_mode="two-way",
                )
                detector = RansomwareDetector(binding_id=binding.binding_id)

                # Drive 200 modify events at t = 0..199.
                tripped = False
                for i in range(MAX_EVENTS_PER_WINDOW):
                    v = detector.record(
                        kind="modify", path=f"f{i:03d}", now=float(i),
                    )
                    if v.tripped and not tripped:
                        # First time we see a trip → flip binding.
                        store.update_binding_state(
                            binding.binding_id, state="paused",
                        )
                        tripped = True

                self.assertTrue(tripped)
                rebound = store.get_binding(binding.binding_id)
                self.assertEqual(rebound.state, "paused")
                # sync_mode preserved (§A12: pause keeps mode for resume).
                self.assertEqual(rebound.sync_mode, "two-way")
            finally:
                if saved_xdg is None:
                    os.environ.pop("XDG_CACHE_HOME", None)
                else:
                    os.environ["XDG_CACHE_HOME"] = saved_xdg
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
