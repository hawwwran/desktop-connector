"""F-Y16 / F-Y28 — NFC normalization across the bindings store + read sites.

Czech / accented filenames round-tripped between macOS NFD and Linux
NFC encode the same character sequence as different bytes. Without
normalization, SQL byte-equality lookups miss; the fingerprint
shortcut bypasses; every two-way pass re-downloads.

These tests pin three contracts:

1. ``vault_bindings.normalize_relative_path`` is a pure helper —
   NFC + ``\\`` → ``/`` + leading-slash strip; same input bytes give
   same output regardless of how the caller encoded the string.
2. ``VaultBindingsStore`` is symmetric across NFD/NFC at every entry
   point (upsert / get / delete on local_entries; enqueue /
   coalesce / first_pending_op on pending_ops).
3. The watcher's ``observe`` and the baseline's ``_plan_baseline``
   produce NFC paths so downstream comparisons (e.g. baseline's
   ``downloaded_set`` membership test) don't miss on encoding drift.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_bindings import (  # noqa: E402
    VaultBindingsStore,
    VaultLocalEntry,
    normalize_relative_path,
)
from src.vault_binding_baseline import _plan_baseline  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_filesystem_watcher import WatcherCoordinator  # noqa: E402


# Czech "č" combining form (NFD) versus precomposed (NFC).
NFC_NAME = unicodedata.normalize("NFC", "Pšeničné/Český čaj.txt")
NFD_NAME = unicodedata.normalize("NFD", NFC_NAME)
assert NFC_NAME != NFD_NAME, "test fixture must produce divergent bytes"


class NormalizeHelperTests(unittest.TestCase):
    def test_idempotent_on_already_nfc(self) -> None:
        self.assertEqual(normalize_relative_path(NFC_NAME), NFC_NAME)

    def test_nfd_collapses_to_nfc(self) -> None:
        self.assertEqual(normalize_relative_path(NFD_NAME), NFC_NAME)

    def test_backslash_to_forward_slash(self) -> None:
        self.assertEqual(
            normalize_relative_path("a\\b\\c.txt"), "a/b/c.txt",
        )

    def test_strips_single_leading_slash(self) -> None:
        self.assertEqual(normalize_relative_path("/foo/bar"), "foo/bar")

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(normalize_relative_path(""), "")


class StoreSymmetryTests(unittest.TestCase):
    """Insert NFD → look up NFC (and vice versa) must succeed."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_nfc_store_"))
        self.index = VaultLocalIndex(self.tmpdir)
        self.store = VaultBindingsStore(self.index.db_path)
        # Seed a binding row so the FK constraint is happy.
        self.binding = self.store.create_binding(
            vault_id="ABCD2345WXYZ",
            remote_folder_id="rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
            local_path=str(self.tmpdir / "bind"),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_local_entry_upsert_nfd_lookup_nfc(self) -> None:
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=self.binding.binding_id,
            relative_path=NFD_NAME,
            content_fingerprint="fp",
            size_bytes=1, mtime_ns=2, last_synced_revision=3,
        ))
        got = self.store.get_local_entry(
            self.binding.binding_id, NFC_NAME,
        )
        self.assertIsNotNone(got)
        self.assertEqual(got.relative_path, NFC_NAME)
        self.assertEqual(got.content_fingerprint, "fp")

    def test_local_entry_upsert_nfc_lookup_nfd(self) -> None:
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=self.binding.binding_id,
            relative_path=NFC_NAME,
            content_fingerprint="fp",
            size_bytes=1, mtime_ns=2, last_synced_revision=3,
        ))
        got = self.store.get_local_entry(
            self.binding.binding_id, NFD_NAME,
        )
        self.assertIsNotNone(got)

    def test_local_entry_upsert_collapses_nfd_and_nfc(self) -> None:
        # A second upsert with the other encoding must update — not
        # insert — so we end up with one row, not two.
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=self.binding.binding_id,
            relative_path=NFD_NAME,
            content_fingerprint="first",
            size_bytes=1, mtime_ns=1, last_synced_revision=1,
        ))
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=self.binding.binding_id,
            relative_path=NFC_NAME,
            content_fingerprint="second",
            size_bytes=2, mtime_ns=2, last_synced_revision=2,
        ))
        rows = self.store.list_local_entries(self.binding.binding_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content_fingerprint, "second")

    def test_local_entry_delete_nfc_after_nfd_insert(self) -> None:
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=self.binding.binding_id,
            relative_path=NFD_NAME,
            content_fingerprint="fp",
            size_bytes=1, mtime_ns=1, last_synced_revision=1,
        ))
        self.assertTrue(
            self.store.delete_local_entry(self.binding.binding_id, NFC_NAME)
        )
        self.assertEqual(
            self.store.list_local_entries(self.binding.binding_id), []
        )

    def test_pending_op_coalesces_across_nfd_and_nfc(self) -> None:
        first = self.store.coalesce_op(
            binding_id=self.binding.binding_id,
            op_type="upload", relative_path=NFD_NAME,
        )
        second = self.store.coalesce_op(
            binding_id=self.binding.binding_id,
            op_type="upload", relative_path=NFC_NAME,
        )
        # Same op_id ⇒ coalesced via NFC-equality.
        self.assertEqual(first.op_id, second.op_id)
        ops = self.store.list_pending_ops(self.binding.binding_id)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].relative_path, NFC_NAME)

    def test_enqueue_pending_op_returns_normalized_path(self) -> None:
        op = self.store.enqueue_pending_op(
            binding_id=self.binding.binding_id,
            op_type="upload", relative_path=NFD_NAME,
        )
        self.assertEqual(op.relative_path, NFC_NAME)


class _FakeStore:
    """Mirror of the watcher-test fixture in ``test_desktop_vault_filesystem_watcher.py``."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []

    def coalesce_op(self, *, binding_id: str, op_type: str, relative_path: str, now: int | None = None):
        self.enqueued.append((op_type, relative_path))


class _FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class WatcherNFCTests(unittest.TestCase):
    """Watcher's ``observe`` enqueues NFC paths regardless of input."""

    def test_observe_with_nfd_then_tick_enqueues_nfc(self) -> None:
        clock = _FakeClock(0.0)
        store = _FakeStore()
        coord = WatcherCoordinator(
            binding_id="rb_v1_a", local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            stat_provider=lambda _p: (10, 100),
        )

        coord.observe(NFD_NAME, kind="modified")
        # First tick: stability gate records (size, mtime, now=0).
        clock.advance(0.6)
        self.assertEqual(coord.tick(), 0)
        # Second tick after the 3 s window with the same stat → ready.
        clock.advance(3.5)
        self.assertEqual(coord.tick(), 1)

        self.assertEqual(len(store.enqueued), 1)
        op_type, path = store.enqueued[0]
        self.assertEqual(op_type, "upload")
        self.assertEqual(path, NFC_NAME)

    def test_observe_collapses_nfd_and_nfc_into_one(self) -> None:
        clock = _FakeClock(0.0)
        store = _FakeStore()
        coord = WatcherCoordinator(
            binding_id="rb_v1_a", local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            stat_provider=lambda _p: (10, 100),
        )

        coord.observe(NFD_NAME, kind="modified")
        coord.observe(NFC_NAME, kind="modified")
        # Both should have collapsed to a single _PendingPath keyed on
        # the NFC form.
        self.assertEqual(coord.pending_paths(), [NFC_NAME])
        clock.advance(0.6)
        coord.tick()
        clock.advance(3.5)
        self.assertEqual(coord.tick(), 1)
        self.assertEqual(store.enqueued, [("upload", NFC_NAME)])


class BaselineNFCTests(unittest.TestCase):
    """``_plan_baseline`` returns NFC-normalized paths."""

    def test_plan_baseline_nfd_entry_yields_nfc_path(self) -> None:
        folder = {
            "remote_folder_id": "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
            "entries": [
                {
                    "type": "file",
                    "deleted": False,
                    "path": NFD_NAME,
                    "latest_version_id": "fv_v1_a",
                    "versions": [{"version_id": "fv_v1_a"}],
                }
            ],
        }
        plan = _plan_baseline(folder)
        self.assertEqual(len(plan), 1)
        relative, _entry = plan[0]
        self.assertEqual(relative, NFC_NAME)


if __name__ == "__main__":
    unittest.main()
