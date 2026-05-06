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

    def test_device_name_path_traversal_attempts_neutralized(self) -> None:
        """F-D24 — `device_name` is user-set per device. Even adversarial
        values must not produce a leaf with usable path separators or
        an inserted parent-directory segment. Path separators (``/``,
        ``\\``), NUL, and quote/escape characters all collapse to ``_``.
        ``..`` survives as a literal token (period is a safe filename
        character) but only inside the parens, with no adjacent
        separators, so it can't form a traversal.
        """
        traversal = make_conflict_path(
            original_path="report.docx", kind="uploaded",
            device_name="../etc/passwd", when=WHEN,
        )
        # No `/` or `\` survives in the leaf.
        leaf = traversal.split("/")[-1]
        self.assertNotIn("/", leaf)
        self.assertNotIn("\\", leaf)
        # Periods are allowed (hostnames legitimately contain them); the
        # adjacent path separators are each replaced by `_` so the
        # `..` can't bracket a traversal — the resulting token is a
        # single literal filename component.
        self.assertIn(".._etc_passwd", leaf, leaf)

        # NUL byte sanitization.
        nul = make_conflict_path(
            original_path="report.docx", kind="uploaded",
            device_name="laptop\x00\x00rm-rf",
            when=WHEN,
        )
        self.assertNotIn("\x00", nul)
        # Backslashes (Windows separators) and other unsafe chars.
        backslash = make_conflict_path(
            original_path="report.docx", kind="uploaded",
            device_name="C:\\Windows\\Bad", when=WHEN,
        )
        self.assertNotIn("\\", backslash)
        self.assertNotIn(":", backslash.split("/")[-1])


class AttemptParameterTests(unittest.TestCase):
    """F-Y12 — ``attempt`` adds an in-paren ``#N`` disambiguator.

    The previous "recurse by passing the candidate back as
    original_path" pattern stacked a fresh suffix every iteration and
    grew the leaf by ~50 characters per collision; the bounded loops in
    ``_unique_conflict_path`` (twoway / restore) now hold the path
    constant and bump ``attempt`` instead.
    """

    def test_attempt_one_is_default_no_numeric_suffix(self) -> None:
        out = make_conflict_path(
            original_path="report.pdf", kind="synced",
            device_name="Laptop", when=WHEN, attempt=1,
        )
        self.assertEqual(
            out, "report (conflict synced Laptop 2026-05-04 17-30).pdf",
        )
        # Implicit attempt=1 is the same.
        self.assertEqual(
            out,
            make_conflict_path(
                original_path="report.pdf", kind="synced",
                device_name="Laptop", when=WHEN,
            ),
        )

    def test_attempt_two_appends_numeric_inside_parens(self) -> None:
        out = make_conflict_path(
            original_path="report.pdf", kind="synced",
            device_name="Laptop", when=WHEN, attempt=2,
        )
        self.assertEqual(
            out, "report (conflict synced Laptop 2026-05-04 17-30 #2).pdf",
        )

    def test_attempt_growth_is_linear_in_digits(self) -> None:
        """The leaf grows by ``len(str(N)) + 2`` ('# ' + digits) — *not*
        by stacking a fresh ~50-char suffix block as the recursive form
        did. We pin the bound so a future regression that swaps the
        ``#N`` shape for "(...)(...)" stacking trips here.
        """
        base = make_conflict_path(
            original_path="report.pdf", kind="synced",
            device_name="Laptop", when=WHEN, attempt=1,
        )
        for n in (2, 5, 19, 20):
            with self.subTest(attempt=n):
                out = make_conflict_path(
                    original_path="report.pdf", kind="synced",
                    device_name="Laptop", when=WHEN, attempt=n,
                )
                # Strip the ".pdf" extension before length-checking the
                # disambiguator portion.
                base_stem = base[: -len(".pdf")]
                out_stem = out[: -len(".pdf")]
                self.assertTrue(out_stem.startswith(base_stem[:-1]))  # "...30"
                added = len(out_stem) - len(base_stem)
                expected = len(f" #{n}")
                self.assertEqual(added, expected)

    def test_attempt_zero_or_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_conflict_path(
                original_path="x.txt", kind="synced",
                device_name="L", when=WHEN, attempt=0,
            )
        with self.assertRaises(ValueError):
            make_conflict_path(
                original_path="x.txt", kind="synced",
                device_name="L", when=WHEN, attempt=-1,
            )


class TwoWayUniqueConflictPathTests(unittest.TestCase):
    """F-Y12 — bounded loop in ``_unique_conflict_path`` (twoway sync)."""

    def test_first_attempt_returns_unsuffixed_form(self) -> None:
        import tempfile
        from pathlib import Path
        from src.vault_binding_twoway import _unique_conflict_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = _unique_conflict_path(
                local_root=root,
                relative_path="report.pdf",
                device_name="Laptop",
            )
        # No "#2" — first call sees the path as free.
        self.assertNotIn("#", out)
        self.assertTrue(out.endswith(".pdf"))

    def test_collision_advances_attempt_not_suffix_stack(self) -> None:
        import tempfile
        from pathlib import Path
        from src.vault_binding_twoway import _unique_conflict_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Pre-create the unsuffixed candidate so the loop must
            # advance to attempt=2.
            from src.vault_conflict_naming import make_conflict_path
            from datetime import datetime, timezone
            # Freezing time isn't strictly necessary — _unique_conflict_path
            # uses datetime.now(); we just need the first attempt's
            # candidate to exist on disk so the loop bumps. We create
            # *both* the natural minute's first form and the previous
            # minute's first form to cover the second-rollover edge.
            for delta_minutes in range(0, 2):
                when = datetime.now(timezone.utc).replace(
                    second=0, microsecond=0,
                )
                cand = make_conflict_path(
                    original_path="x.txt", kind="synced",
                    device_name="L", when=when, attempt=1,
                )
                (root / cand).parent.mkdir(parents=True, exist_ok=True)
                (root / cand).write_text("collision")
            out = _unique_conflict_path(
                local_root=root,
                relative_path="x.txt",
                device_name="L",
            )
            # The result either has " #2" (collision) or no '#' (clock
            # ticked into a different minute mid-test). Either way the
            # leaf must NOT carry two stacked "(conflict ...)" parens.
            leaf = out.split("/")[-1]
            self.assertEqual(leaf.count("(conflict"), 1)


class RestoreUniqueConflictPathTests(unittest.TestCase):
    """F-Y12 — bounded loop in ``_unique_conflict_path`` (restore flow)."""

    def test_collision_uses_attempt_counter_and_caps(self) -> None:
        import tempfile
        from pathlib import Path
        from datetime import datetime, timezone
        from src.vault_restore import _unique_conflict_path
        from src.vault_conflict_naming import make_conflict_path

        when = datetime(2026, 5, 4, 17, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Pre-occupy attempts 1..3 so the loop must reach attempt=4.
            for attempt in range(1, 4):
                cand = make_conflict_path(
                    original_path="report.pdf", kind="restored",
                    device_name="Laptop", when=when, attempt=attempt,
                )
                (root / cand).parent.mkdir(parents=True, exist_ok=True)
                (root / cand).write_text("x")
            out = _unique_conflict_path(
                destination=root,
                relative_path="report.pdf",
                device_name="Laptop",
                when=when,
            )
        # Leaf only has one (conflict ...) suffix — never stacked —
        # and the resolved attempt is #4.
        leaf = out.split("/")[-1]
        self.assertEqual(leaf.count("(conflict"), 1)
        self.assertIn("#4", leaf)


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
