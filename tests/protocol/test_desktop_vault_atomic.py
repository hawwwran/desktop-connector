"""T11.1 — Atomic-write helper + temp-file GC sweep."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_atomic import (  # noqa: E402
    DEFAULT_MAX_AGE_S,
    TEMP_SUFFIX,
    atomic_write_chunks,
    atomic_write_file,
    sweep_orphan_temp_files,
)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_atomic_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_atomic_write_creates_destination_with_bytes(self) -> None:
        dest = self.tmpdir / "alpha.txt"
        atomic_write_file(dest, b"hello world")
        self.assertEqual(dest.read_bytes(), b"hello world")

    def test_atomic_write_creates_missing_parent_directories(self) -> None:
        dest = self.tmpdir / "nested" / "deep" / "file.bin"
        atomic_write_file(dest, b"\x00\x01")
        self.assertTrue(dest.exists())

    def test_atomic_write_chunks_streams_iterable(self) -> None:
        dest = self.tmpdir / "stream.txt"
        written = atomic_write_chunks(dest, (b"foo", b"bar", b"baz"))
        self.assertEqual(written, 9)
        self.assertEqual(dest.read_bytes(), b"foobarbaz")

    def test_failure_during_write_leaves_no_temp_or_dest(self) -> None:
        dest = self.tmpdir / "boom.txt"
        # Pre-populate destination with the OLD content so we can verify
        # that on a write failure, the OLD content is preserved (atomic
        # rename never happens).
        dest.write_bytes(b"old content")

        def chunks():
            yield b"first"
            raise RuntimeError("simulated mid-write failure")

        with self.assertRaises(RuntimeError):
            atomic_write_chunks(dest, chunks())

        # Old content survives — no half-written file.
        self.assertEqual(dest.read_bytes(), b"old content")
        # No temp file lingering in the parent.
        leftovers = [p.name for p in self.tmpdir.iterdir()
                     if TEMP_SUFFIX in p.name]
        self.assertEqual(leftovers, [])

    def test_existing_file_is_replaced_atomically(self) -> None:
        dest = self.tmpdir / "swap.txt"
        dest.write_bytes(b"v1")
        atomic_write_file(dest, b"v2")
        self.assertEqual(dest.read_bytes(), b"v2")


class SweepOrphanTempFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_atomic_sweep_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_temp_file(self, name: str, *, age_seconds: float = 0) -> Path:
        path = self.tmpdir / name
        path.write_bytes(b"")
        if age_seconds:
            mtime = time.time() - age_seconds
            os.utime(path, (mtime, mtime))
        return path

    def test_sweeps_old_temp_files_only(self) -> None:
        old = self._make_temp_file(
            "report.pdf.dc-temp-deadbeefdeadbeef",
            age_seconds=DEFAULT_MAX_AGE_S + 60,
        )
        recent = self._make_temp_file(
            "in-flight.bin.dc-temp-feedface",
            age_seconds=60,
        )
        plain = self.tmpdir / "report.pdf"
        plain.write_bytes(b"actual file")

        removed = sweep_orphan_temp_files(self.tmpdir)
        self.assertEqual([p.name for p in removed], [old.name])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(plain.exists())  # never touched

    def test_walks_subdirectories(self) -> None:
        nested_dir = self.tmpdir / "a" / "b"
        nested_dir.mkdir(parents=True)
        old = nested_dir / "thing.txt.dc-temp-aaaaaa"
        old.write_bytes(b"")
        os.utime(old, (time.time() - DEFAULT_MAX_AGE_S - 30,) * 2)

        removed = sweep_orphan_temp_files(self.tmpdir)
        self.assertEqual(len(removed), 1)
        self.assertFalse(old.exists())

    def test_does_not_touch_unrelated_temp_files(self) -> None:
        # System "temp"-looking names that aren't ours.
        for name in (".tmpfile", "thing.tmp", "foo.dc-temp", "x.dc-temp-XYZ"):
            (self.tmpdir / name).write_bytes(b"")
            os.utime(
                self.tmpdir / name,
                (time.time() - DEFAULT_MAX_AGE_S - 60,) * 2,
            )

        removed = sweep_orphan_temp_files(self.tmpdir)
        self.assertEqual(removed, [])
        for name in (".tmpfile", "thing.tmp", "foo.dc-temp", "x.dc-temp-XYZ"):
            self.assertTrue((self.tmpdir / name).exists())

    def test_missing_root_returns_empty_list_without_raising(self) -> None:
        bogus = self.tmpdir / "does-not-exist"
        removed = sweep_orphan_temp_files(bogus)
        self.assertEqual(removed, [])

    def test_max_age_seconds_is_configurable(self) -> None:
        recent = self._make_temp_file(
            "fresh.txt.dc-temp-cafebabe", age_seconds=60,
        )
        # With a 30-second cap, the 60s-old file is now "old" and gets cleaned.
        removed = sweep_orphan_temp_files(self.tmpdir, max_age_seconds=30)
        self.assertEqual([p.name for p in removed], [recent.name])

    def test_now_override_is_used_for_deterministic_tests(self) -> None:
        path = self._make_temp_file("x.dc-temp-12345678", age_seconds=0)
        # File is "now"; pretending now is 1h in the future means the file is 1h old.
        removed = sweep_orphan_temp_files(
            self.tmpdir, max_age_seconds=30 * 60, now=time.time() + 3600,
        )
        self.assertEqual([p.name for p in removed], [path.name])


class CrashSimulationTests(unittest.TestCase):
    """Verify the §gaps §11 invariant: never partial.

    We simulate a crash by writing a temp file directly (skipping the
    rename) and then verify that the destination file's old contents
    are preserved + sweep_orphan_temp_files cleans up later.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_atomic_crash_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_temp_file_orphan_is_collected_after_24h(self) -> None:
        # Imagine: process wrote tmp + fsync'd, then crashed before rename.
        dest = self.tmpdir / "alpha.txt"
        dest.write_bytes(b"old version")
        orphan = self.tmpdir / "alpha.txt.dc-temp-deadbeef"
        orphan.write_bytes(b"new version was about to land here")
        os.utime(orphan, (time.time() - 24 * 3600 - 60,) * 2)

        # Sweep on next "startup":
        removed = sweep_orphan_temp_files(self.tmpdir)
        self.assertEqual([p.name for p in removed], [orphan.name])
        # Original file untouched:
        self.assertEqual(dest.read_bytes(), b"old version")


if __name__ == "__main__":
    unittest.main()
