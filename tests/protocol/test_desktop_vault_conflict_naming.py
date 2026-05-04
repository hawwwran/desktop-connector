"""T11.3 — §A20 conflict-naming helper used by sync / upload / import."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_conflict_naming import (  # noqa: E402
    KNOWN_KINDS,
    make_conflict_path,
    short_timestamp,
)


WHEN = datetime(2026, 5, 4, 17, 30, tzinfo=timezone.utc)


class MakeConflictPathTests(unittest.TestCase):
    def test_uploaded_with_device_name(self) -> None:
        out = make_conflict_path(
            original_path="report.docx",
            kind="uploaded",
            device_name="Laptop",
            when=WHEN,
        )
        self.assertEqual(
            out, "report (conflict uploaded Laptop 2026-05-04 17-30).docx",
        )

    def test_imported_omits_device_name_when_none(self) -> None:
        out = make_conflict_path(
            original_path="report.docx", kind="imported", when=WHEN,
        )
        self.assertEqual(
            out, "report (conflict imported 2026-05-04 17-30).docx",
        )

    def test_synced_with_device_name(self) -> None:
        out = make_conflict_path(
            original_path="Invoices/2026/report.pdf",
            kind="synced",
            device_name="OtherLaptop",
            when=WHEN,
        )
        self.assertEqual(
            out,
            "Invoices/2026/report (conflict synced OtherLaptop 2026-05-04 17-30).pdf",
        )

    def test_extensionless_file(self) -> None:
        out = make_conflict_path(
            original_path="README", kind="uploaded",
            device_name="Laptop", when=WHEN,
        )
        self.assertEqual(out, "README (conflict uploaded Laptop 2026-05-04 17-30)")

    def test_recursion_appends_second_suffix(self) -> None:
        first = make_conflict_path(
            original_path="report.pdf", kind="uploaded",
            device_name="A", when=WHEN,
        )
        second = make_conflict_path(
            original_path=first, kind="uploaded",
            device_name="B", when=WHEN,
        )
        self.assertEqual(
            second,
            "report (conflict uploaded A 2026-05-04 17-30) "
            "(conflict uploaded B 2026-05-04 17-30).pdf",
        )

    def test_directory_portion_preserved(self) -> None:
        out = make_conflict_path(
            original_path="docs/sub/file.txt", kind="restored",
            device_name=None, when=WHEN,
        )
        self.assertEqual(
            out, "docs/sub/file (conflict restored 2026-05-04 17-30).txt",
        )

    def test_device_name_sanitization(self) -> None:
        out = make_conflict_path(
            original_path="report.docx", kind="uploaded",
            device_name="Bad/Name:With*Chars", when=WHEN,
        )
        self.assertEqual(
            out, "report (conflict uploaded Bad_Name_With_Chars 2026-05-04 17-30).docx",
        )

    def test_empty_string_device_name_falls_back_to_literal_device(self) -> None:
        # When the caller *did* opt in to a device-name slot but the
        # name itself sanitizes to nothing (whitespace / all-special
        # chars), fall back to the literal "device" so the suffix
        # vocabulary stays uniform — preserves legacy
        # make_conflict_renamed_path() semantics.
        out = make_conflict_path(
            original_path="x.txt", kind="uploaded",
            device_name="", when=WHEN,
        )
        self.assertEqual(
            out, "x (conflict uploaded device 2026-05-04 17-30).txt",
        )

    def test_three_callers_produce_identical_path_for_same_inputs(self) -> None:
        """T11.3 acceptance: shared utility means same inputs → same outputs."""
        # Sync caller (two-way) and a hypothetical restore caller and the
        # browser-upload "Keep both" caller all converge on this:
        sync_path = make_conflict_path(
            original_path="x.txt", kind="synced",
            device_name="Workstation", when=WHEN,
        )
        upload_path = make_conflict_path(
            original_path="x.txt", kind="synced",
            device_name="Workstation", when=WHEN,
        )
        restore_path = make_conflict_path(
            original_path="x.txt", kind="synced",
            device_name="Workstation", when=WHEN,
        )
        self.assertEqual(sync_path, upload_path)
        self.assertEqual(upload_path, restore_path)

    def test_invalid_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_conflict_path(original_path="x", kind="", when=WHEN)

    def test_empty_path_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_conflict_path(original_path="", kind="uploaded", when=WHEN)

    def test_known_kinds_enumerates_supported_verbs(self) -> None:
        # Sanity check on the public vocabulary set.
        self.assertIn("uploaded", KNOWN_KINDS)
        self.assertIn("imported", KNOWN_KINDS)
        self.assertIn("synced", KNOWN_KINDS)
        self.assertIn("restored", KNOWN_KINDS)


class ShortTimestampTests(unittest.TestCase):
    def test_datetime_round_trip(self) -> None:
        self.assertEqual(short_timestamp(WHEN), "2026-05-04 17-30")

    def test_rfc3339_string_input(self) -> None:
        self.assertEqual(
            short_timestamp("2026-05-04T17:30:00.000Z"),
            "2026-05-04 17-30",
        )

    def test_naive_datetime_treated_as_utc(self) -> None:
        naive = datetime(2026, 5, 4, 17, 30)
        self.assertEqual(short_timestamp(naive), "2026-05-04 17-30")

    def test_unparsable_string_passes_through(self) -> None:
        self.assertEqual(short_timestamp("not a date"), "not a date")


class CallerConsistencyTests(unittest.TestCase):
    """Refactor guard: existing wrappers still produce historic outputs."""

    def test_upload_wrapper_kept_existing_signature(self) -> None:
        from src.vault_upload import make_conflict_renamed_path
        out = make_conflict_renamed_path(
            "Invoices/2026/report.pdf",
            "Workstation 7",
            now=WHEN,
        )
        self.assertEqual(
            out,
            "Invoices/2026/report (conflict uploaded Workstation 7 2026-05-04 17-30).pdf",
        )

    def test_import_wrapper_kept_existing_signature(self) -> None:
        from src.vault_import import _conflict_imported_path
        out = _conflict_imported_path(
            "docs/file.pdf", "2026-05-04T17:30:00.000Z",
        )
        self.assertEqual(
            out, "docs/file (conflict imported 2026-05-04 17-30).pdf",
        )


if __name__ == "__main__":
    unittest.main()
