"""Producer side of the vault op-log — Phase 1 of the Activity-timeline plan.

The consumer side (``state/activity.py``) is the wire-format owner; this
test file asserts that ``op_log.build_op_log_entry`` produces entries
the consumer parses round-trip, and that ``append_op_log_entries`` keeps
the manifest tail bounded with observable truncation.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.state.activity import normalize_op_log_entry  # noqa: E402
from src.vault.state.op_log import (  # noqa: E402
    MAX_OP_LOG_TAIL,
    append_op_log_entries,
    build_op_log_entry,
)


DEVICE_A = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
DEVICE_B = "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7"


class BuildOpLogEntryTests(unittest.TestCase):
    def test_minimal_required_fields(self) -> None:
        before = int(time.time())
        entry = build_op_log_entry(
            event_type="vault.upload.completed",
            device_id=DEVICE_A,
            revision=3,
        )
        after = int(time.time())
        self.assertEqual(entry["type"], "vault.upload.completed")
        self.assertEqual(entry["device_id"], DEVICE_A)
        self.assertEqual(entry["revision"], 3)
        self.assertGreaterEqual(entry["ts"], before)
        self.assertLessEqual(entry["ts"], after)
        # Optional fields are absent rather than empty strings — keeps
        # the encrypted manifest's JSON minimal.
        self.assertNotIn("path", entry)
        self.assertNotIn("device_name", entry)
        self.assertNotIn("summary", entry)

    def test_optional_fields_included_when_set(self) -> None:
        entry = build_op_log_entry(
            event_type="vault.delete.completed",
            device_id=DEVICE_A,
            revision=5,
            path="Documents/old.txt",
            device_name="Laptop-1",
            summary="Tombstoned",
        )
        self.assertEqual(entry["path"], "Documents/old.txt")
        self.assertEqual(entry["device_name"], "Laptop-1")
        self.assertEqual(entry["summary"], "Tombstoned")

    def test_explicit_ts_overrides_clock(self) -> None:
        entry = build_op_log_entry(
            event_type="vault.restore.completed",
            device_id=DEVICE_A,
            revision=7,
            ts=1_700_000_000,
        )
        self.assertEqual(entry["ts"], 1_700_000_000)

    def test_extras_are_merged_alongside_reserved_fields(self) -> None:
        entry = build_op_log_entry(
            event_type="vault.restore.completed",
            device_id=DEVICE_A,
            revision=11,
            extra={"source_version_id": "vfa1b2c3"},
        )
        self.assertEqual(entry["source_version_id"], "vfa1b2c3")

    def test_extras_collision_raises(self) -> None:
        for reserved in ("ts", "type", "path", "device_id",
                         "device_name", "summary", "revision"):
            with self.subTest(field=reserved):
                with self.assertRaises(ValueError):
                    build_op_log_entry(
                        event_type="vault.upload.completed",
                        device_id=DEVICE_A,
                        revision=1,
                        extra={reserved: "x"},
                    )

    def test_empty_event_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_op_log_entry(event_type="", device_id=DEVICE_A, revision=1)

    def test_round_trips_through_normalize_op_log_entry(self) -> None:
        # The consumer side is the wire-format owner; assert build_op_log_entry's
        # output parses back via normalize_op_log_entry with no field loss.
        entry = build_op_log_entry(
            event_type="vault.upload.completed",
            device_id=DEVICE_A,
            revision=12,
            path="Photos/IMG_0001.jpg",
            device_name="Laptop-1",
            summary="Uploaded version 1",
            ts=1_700_000_000,
            extra={"size": 4096},
        )
        row = normalize_op_log_entry(entry)
        self.assertIsNotNone(row)
        assert row is not None  # for type narrowing
        self.assertEqual(row.event_type, "vault.upload.completed")
        self.assertEqual(row.timestamp_epoch, 1_700_000_000)
        self.assertEqual(row.device_id, DEVICE_A)
        self.assertEqual(row.revision, 12)
        self.assertEqual(row.display_path, "Photos/IMG_0001.jpg")
        self.assertEqual(row.device_name, "Laptop-1")
        self.assertEqual(row.summary, "Uploaded version 1")
        self.assertEqual(row.extra, {"size": 4096})


class AppendOpLogEntriesTests(unittest.TestCase):
    def _make(self, ts: int, event_type: str = "vault.upload.completed") -> dict:
        return build_op_log_entry(
            event_type=event_type, device_id=DEVICE_A, revision=ts, ts=ts,
        )

    def test_none_inputs_yield_empty_list(self) -> None:
        self.assertEqual(append_op_log_entries(None, None), [])

    def test_empty_inputs_yield_empty_list(self) -> None:
        self.assertEqual(append_op_log_entries([], []), [])

    def test_appends_in_input_order(self) -> None:
        prior = [self._make(1), self._make(2)]
        new = [self._make(3), self._make(4)]
        out = append_op_log_entries(prior, new)
        self.assertEqual([e["ts"] for e in out], [1, 2, 3, 4])

    def test_inputs_not_mutated(self) -> None:
        prior = [self._make(1)]
        new = [self._make(2)]
        out = append_op_log_entries(prior, new)
        out.append(self._make(99))
        # Originals untouched.
        self.assertEqual([e["ts"] for e in prior], [1])
        self.assertEqual([e["ts"] for e in new], [2])

    def test_drops_oldest_when_over_cap(self) -> None:
        prior = [self._make(i) for i in range(150)]
        new = [self._make(1000 + i) for i in range(100)]
        out = append_op_log_entries(prior, new)
        self.assertEqual(len(out), MAX_OP_LOG_TAIL)
        # Newest 200 entries survive: ts 50..149 from prior + 1000..1099 from new.
        self.assertEqual(out[0]["ts"], 50)
        self.assertEqual(out[-1]["ts"], 1099)

    def test_truncation_emits_info_log(self) -> None:
        prior = [self._make(i) for i in range(MAX_OP_LOG_TAIL)]
        new = [self._make(1000 + i) for i in range(5)]
        with self.assertLogs(
            "src.vault.state.op_log", level=logging.INFO,
        ) as captured:
            append_op_log_entries(prior, new)
        joined = "\n".join(captured.output)
        self.assertIn("vault.activity.tail_truncated_evicted_oldest", joined)
        self.assertIn("count=5", joined)

    def test_no_truncation_log_when_under_cap(self) -> None:
        prior = [self._make(1)]
        new = [self._make(2)]
        logger = logging.getLogger("src.vault.state.op_log")
        # assertNoLogs would be cleaner but is 3.10+; tolerate either.
        with self.assertLogs(logger, level=logging.INFO) as captured:
            # Force the assertLogs context to emit at least once or it errors.
            logger.info("probe.start")
            append_op_log_entries(prior, new)
            logger.info("probe.end")
        joined = "\n".join(captured.output)
        self.assertNotIn("tail_truncated", joined)

    def test_custom_max_tail_kwarg(self) -> None:
        prior = [self._make(i) for i in range(3)]
        new = [self._make(100 + i) for i in range(3)]
        out = append_op_log_entries(prior, new, max_tail=4)
        self.assertEqual(len(out), 4)
        # Newest 4: ts 2 from prior + 100, 101, 102 from new.
        self.assertEqual([e["ts"] for e in out], [2, 100, 101, 102])

    def test_max_tail_zero_returns_empty(self) -> None:
        prior = [self._make(1)]
        new = [self._make(2)]
        self.assertEqual(append_op_log_entries(prior, new, max_tail=0), [])

    def test_negative_max_tail_raises(self) -> None:
        with self.assertRaises(ValueError):
            append_op_log_entries([], [], max_tail=-1)


class MaxTailBudgetTests(unittest.TestCase):
    def test_cap_at_least_four_publish_batches(self) -> None:
        # Cap is documented as ``>= 4 * PUBLISH_BATCH_SIZE`` so a single
        # full batch lands without immediately evicting the prior batch.
        # If PUBLISH_BATCH_SIZE changes, revisit MAX_OP_LOG_TAIL.
        from src.vault.binding.sync import PUBLISH_BATCH_SIZE
        self.assertGreaterEqual(MAX_OP_LOG_TAIL, 4 * PUBLISH_BATCH_SIZE)


if __name__ == "__main__":
    unittest.main()
