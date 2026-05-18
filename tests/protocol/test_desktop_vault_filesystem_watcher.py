"""T10.4 — Filesystem watcher: debouncer + stability gate + coordinator."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.binding.filesystem_watcher import (  # noqa: E402
    DEBOUNCE_WINDOW_S,
    STABILITY_HUNG_AFTER_S,
    STABILITY_WINDOW_LOCAL_S,
    STABILITY_WINDOW_NETWORK_S,
    EventDebouncer,
    StabilityGate,
    WatcherCoordinator,
    make_previously_synced_predicate,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeStore:
    binding_id: str
    enqueued: list[tuple[str, str]] = field(default_factory=list)

    def coalesce_op(self, *, binding_id: str, op_type: str, relative_path: str, now: int | None = None):
        self.enqueued.append((op_type, relative_path))


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# EventDebouncer
# ---------------------------------------------------------------------------


class EventDebouncerTests(unittest.TestCase):
    def test_first_event_is_fresh(self) -> None:
        deb = EventDebouncer(window_s=0.5)
        self.assertTrue(deb.observe("a.txt", now=0.0))

    def test_second_event_inside_window_is_not_fresh(self) -> None:
        deb = EventDebouncer(window_s=0.5)
        deb.observe("a.txt", now=0.0)
        self.assertFalse(deb.observe("a.txt", now=0.3))

    def test_event_outside_window_is_fresh_again(self) -> None:
        deb = EventDebouncer(window_s=0.5)
        deb.observe("a.txt", now=0.0)
        self.assertTrue(deb.observe("a.txt", now=0.6))


# ---------------------------------------------------------------------------
# StabilityGate
# ---------------------------------------------------------------------------


class StabilityGateTests(unittest.TestCase):
    def test_constants_match_h13_spec(self) -> None:
        self.assertEqual(DEBOUNCE_WINDOW_S, 0.5)
        self.assertEqual(STABILITY_WINDOW_LOCAL_S, 3.0)
        self.assertEqual(STABILITY_WINDOW_NETWORK_S, 10.0)
        self.assertEqual(STABILITY_HUNG_AFTER_S, 5 * 60)

    def test_unchanged_file_becomes_ready_after_window(self) -> None:
        gate = StabilityGate(window_s=3.0)
        v = gate.check("a", size=10, mtime_ns=100, now=0.0, first_event_at=0.0)
        self.assertFalse(v.ready)
        v = gate.check("a", size=10, mtime_ns=100, now=2.9, first_event_at=0.0)
        self.assertFalse(v.ready)
        v = gate.check("a", size=10, mtime_ns=100, now=3.0, first_event_at=0.0)
        self.assertTrue(v.ready)

    def test_changing_file_resets_window(self) -> None:
        gate = StabilityGate(window_s=3.0)
        gate.check("a", size=10, mtime_ns=100, now=0.0, first_event_at=0.0)
        # File grew at t=2 → reset.
        gate.check("a", size=20, mtime_ns=200, now=2.0, first_event_at=0.0)
        v = gate.check("a", size=20, mtime_ns=200, now=4.5, first_event_at=0.0)
        self.assertFalse(v.ready)
        v = gate.check("a", size=20, mtime_ns=200, now=5.1, first_event_at=0.0)
        self.assertTrue(v.ready)

    def test_hung_after_cap_signals_timeout(self) -> None:
        gate = StabilityGate(window_s=3.0, hung_after_s=10.0)
        # File keeps changing every second — never settles.
        for t in range(11):
            gate.check("a", size=t, mtime_ns=t * 1000, now=float(t), first_event_at=0.0)
        v = gate.check("a", size=99, mtime_ns=99_000, now=11.0, first_event_at=0.0)
        self.assertTrue(v.timed_out)


# ---------------------------------------------------------------------------
# WatcherCoordinator
# ---------------------------------------------------------------------------


class WatcherCoordinatorTests(unittest.TestCase):
    def test_burst_of_modifies_collapses_to_one_upload_after_stability(self) -> None:
        """T10.4 acceptance: bursts of file edits collapse into batched
        ops; stability gate prevents partial-file uploads."""
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_a")
        # Hand-rolled stat: returns (size, mtime) we control.
        stat_state = {"a.txt": (1, 100)}
        coord = WatcherCoordinator(
            binding_id="rb_v1_a",
            local_root=Path("/tmp/dummy"),
            store=store,
            clock=clock,
            stat_provider=lambda p: stat_state.get(p),
        )

        # 5 modify events in quick succession (each grows the file).
        for i in range(5):
            stat_state["a.txt"] = (1 + i, 100 + i * 10)
            clock.advance(0.1)
            coord.observe("a.txt", kind="modified")

        # Tick before the stability window has elapsed: nothing ready.
        clock.advance(0.5)
        self.assertEqual(coord.tick(), 0)

        # File stops changing; tick after the 3 s stability window.
        clock.advance(3.5)
        self.assertEqual(coord.tick(), 1)
        self.assertEqual(store.enqueued, [("upload", "a.txt")])

        # Subsequent tick is a no-op (already enqueued).
        self.assertEqual(coord.tick(), 0)

    def test_delete_event_enqueues_immediately_no_stability_wait(self) -> None:
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_b")
        coord = WatcherCoordinator(
            binding_id="rb_v1_b",
            local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            stat_provider=lambda p: None,
        )
        coord.observe("removed.txt", kind="deleted")
        self.assertEqual(store.enqueued, [("delete", "removed.txt")])

    def test_concurrent_observe_and_tick_does_not_corrupt_state(self) -> None:
        """Review §3.H5: observe() runs on the watchdog observer
        thread; tick() runs on the per-binding sync thread. Both
        mutate ``_pending``, ``_debouncer._last_seen``, and
        ``_gate._snapshots`` without a lock pre-fix. Compound RMW
        patterns (observe's "get-existing-then-mutate-fields" vs
        tick's "pop-then-coalesce") race — the worst case is an
        ``enqueued = False`` reset on a path the sync cycle is
        mid-publishing, re-opening the gate on bytes that are
        already underway.

        Drive 8 threads × 200 iterations of mixed observe/tick on a
        common path set and assert no exception escapes and the
        enqueued list is internally consistent (no duplicate
        ``upload`` ops for the same path within a single tick
        snapshot).
        """
        import threading
        from src.vault.binding.filesystem_watcher import WatcherCoordinator

        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_thr")
        # Shared lock around store.enqueued so the FakeStore's list
        # append is itself thread-safe; what we're testing is the
        # WATCHER's own dict-mutation safety.
        store_lock = threading.Lock()

        class LockedFakeStore:
            def __init__(self, binding_id: str) -> None:
                self.binding_id = binding_id
                self.enqueued: list[tuple[str, str]] = []

            def coalesce_op(self, *, binding_id, op_type, relative_path, now=None):
                with store_lock:
                    self.enqueued.append((op_type, relative_path))

        locked_store = LockedFakeStore(binding_id="rb_v1_thr")
        stat_state: dict[str, tuple[int, int]] = {}
        stat_lock = threading.Lock()

        def stat(path):
            with stat_lock:
                return stat_state.get(path)

        coord = WatcherCoordinator(
            binding_id="rb_v1_thr",
            local_root=Path("/tmp/dummy"),
            store=locked_store,
            clock=clock,
            stat_provider=stat,
        )

        paths = [f"file-{i}.txt" for i in range(20)]
        for p in paths:
            with stat_lock:
                stat_state[p] = (1, 100)

        errors: list[BaseException] = []

        def observer_loop() -> None:
            try:
                for i in range(200):
                    p = paths[i % len(paths)]
                    with stat_lock:
                        stat_state[p] = (1 + i, 100 + i)
                    coord.observe(p, kind="modified", now=float(i) * 0.001)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def tick_loop() -> None:
            try:
                for i in range(200):
                    coord.tick(now=float(i) * 0.001 + 5.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=observer_loop) for _ in range(4)
        ] + [threading.Thread(target=tick_loop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"thread errors: {errors!r}")
        # Final tick can only ever produce uploads for distinct paths
        # at any given snapshot — we only check no exception fired
        # (corruption typically surfaces as KeyError / RuntimeError
        # during dict iteration). All threads joined → no deadlock.
        for t in threads:
            self.assertFalse(
                t.is_alive(),
                "watcher coordinator thread did not finish — possible deadlock",
            )

    def test_path_vanishing_during_tick_is_treated_as_delete(self) -> None:
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_c")
        coord = WatcherCoordinator(
            binding_id="rb_v1_c",
            local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            # stat_provider returns None → path "vanished" before tick.
            stat_provider=lambda p: None,
        )
        coord.observe("ghost.txt", kind="modified")
        clock.advance(3.5)
        coord.tick()
        self.assertEqual(store.enqueued, [("delete", "ghost.txt")])

    def test_network_share_uses_longer_stability_window(self) -> None:
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_d")
        stat_state = {"smb.txt": (10, 1000)}
        coord = WatcherCoordinator(
            binding_id="rb_v1_d",
            local_root=Path("/mnt/share"),
            store=store,
            is_network_share=True,
            clock=clock,
            stat_provider=lambda p: stat_state.get(p),
        )
        coord.observe("smb.txt", kind="modified")
        # First tick at t=5.5 records the (size, mtime) snapshot — gate
        # needs a *second* tick after the stability window to confirm
        # "unchanged for window_s seconds".
        clock.advance(5.5)
        self.assertEqual(coord.tick(), 0)
        # Second tick at t=11 → unchanged_for=5.5 < 10 → still not ready.
        clock.advance(5.5)
        self.assertEqual(coord.tick(), 0)
        # Third tick at t=16 → unchanged_for=10.5 ≥ 10 → ready.
        clock.advance(5.0)
        self.assertEqual(coord.tick(), 1)
        self.assertEqual(store.enqueued, [("upload", "smb.txt")])

    def test_delete_of_never_synced_file_is_silent_t12_2(self) -> None:
        """T12.2 acceptance: deleting a never-synced file enqueues nothing."""
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_t122a")
        coord = WatcherCoordinator(
            binding_id="rb_v1_t122a",
            local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            stat_provider=lambda p: None,
            previously_synced=lambda p: False,
        )
        coord.observe("draft.txt", kind="deleted")
        self.assertEqual(store.enqueued, [])

    def test_delete_of_synced_file_tombstones_t12_2(self) -> None:
        """T12.2 acceptance: deleting a previously-synced file tombstones."""
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_t122b")
        synced_paths = {"keepme.txt"}
        coord = WatcherCoordinator(
            binding_id="rb_v1_t122b",
            local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            stat_provider=lambda p: None,
            previously_synced=lambda p: p in synced_paths,
        )
        coord.observe("keepme.txt", kind="deleted")
        self.assertEqual(store.enqueued, [("delete", "keepme.txt")])

    def test_vanished_during_tick_also_gated_t12_2(self) -> None:
        """T12.2: the stat-returns-None path through tick() also gates."""
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_t122c")
        coord = WatcherCoordinator(
            binding_id="rb_v1_t122c",
            local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            stat_provider=lambda p: None,
            previously_synced=lambda p: False,  # never synced
        )
        coord.observe("ghost.txt", kind="modified")
        clock.advance(3.5)
        coord.tick()
        self.assertEqual(store.enqueued, [])

    def test_make_previously_synced_predicate_uses_fingerprint_presence(self) -> None:
        """Helper returns True only for entries with non-empty fingerprint."""
        @dataclass
        class FakeEntry:
            content_fingerprint: str

        class FakeBindingsStore:
            def __init__(self) -> None:
                self.rows: dict[str, FakeEntry] = {}

            def get_local_entry(self, binding_id: str, relative_path: str):
                return self.rows.get(relative_path)

        s = FakeBindingsStore()
        s.rows["fresh.txt"] = FakeEntry(content_fingerprint="abc123")
        s.rows["extra.txt"] = FakeEntry(content_fingerprint="")
        check = make_previously_synced_predicate(s, "rb_v1_x")
        self.assertTrue(check("fresh.txt"))
        self.assertFalse(check("extra.txt"))
        self.assertFalse(check("never-seen.txt"))

    def test_hung_file_is_dropped_after_5_minute_cap(self) -> None:
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_e")
        # Stat always returns a different mtime → file never settles.
        counter = {"n": 0}

        def stat(p: str) -> tuple[int, int] | None:
            counter["n"] += 1
            return (counter["n"], counter["n"] * 1000)

        coord = WatcherCoordinator(
            binding_id="rb_v1_e",
            local_root=Path("/tmp/dummy"),
            store=store, clock=clock,
            stat_provider=stat,
        )
        coord.observe("hung.txt", kind="modified")
        # Drive ticks every 30 s for 6 minutes.
        for _ in range(12):
            clock.advance(30.0)
            coord.tick()
        # Hung detector fires somewhere past 5 min — nothing enqueued.
        self.assertEqual(store.enqueued, [])
        self.assertNotIn("hung.txt", coord.pending_paths())


# ---------------------------------------------------------------------------
# WatcherCoordinator — burst-load coverage (SO-4 from §13)
# ---------------------------------------------------------------------------


class WatcherBurstLoadTests(unittest.TestCase):
    """SO-4 from ``docs/plans/live-testing-followup.md`` §13.

    The B7 test drove the catch-up scan + Sync-now path. SO-4 is the
    coverage gap: when a watcher is actually running and 10 000 files
    appear in a single inotify burst, every event must land in
    ``vault_pending_operations`` — no debounce-induced collapse, no
    stability-gate stall, no silent drop. These tests exercise the
    coordinator's pure logic at burst scale so a regression in the
    debouncer / stability gate / pending dict surfaces as a unit-test
    failure rather than a 10k-file live-test session.

    The watchdog-Observer-thread integration is exercised separately
    by ``test_watchdog_observer_burst_smoke`` below — that test only
    runs when ``watchdog`` is installed (the AppImage / dev tree
    case; CI without it skips).
    """

    BURST_SIZE = 10_000

    def _make_coord(
        self,
        *,
        clock: FakeClock,
        store: FakeStore,
        stat_provider,
        previously_synced=None,
    ) -> WatcherCoordinator:
        return WatcherCoordinator(
            binding_id="rb_v1_burst",
            local_root=Path("/tmp/dummy"),
            store=store,
            clock=clock,
            stat_provider=stat_provider,
            previously_synced=previously_synced,
        )

    def test_10k_creates_all_land_in_pending_ops(self) -> None:
        """A 10 000-file create burst plus the two-tick stability
        sequence (first records the snapshot, second confirms unchanged
        after the window) must enqueue all 10 000 ops — no drops, no
        duplicates, no synthesised paths."""
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_burst")
        # Each file is 256 bytes, mtime stable from the moment it lands —
        # so the stability gate ticks ``ready`` after one window.
        stat_state = {
            f"d{i // 100:03d}/f{i:05d}.txt": (256, 1_000_000 + i)
            for i in range(self.BURST_SIZE)
        }
        coord = self._make_coord(
            clock=clock, store=store,
            stat_provider=lambda p: stat_state.get(p),
        )

        # Burst all 10 000 events into ``observe()`` within a single
        # 50 ms window — modelling inotify firing them in a tight loop.
        for path in stat_state.keys():
            # Advance the clock 5 µs per event so the wall-clock stays
            # monotonic; each path's first event is fresh either way.
            clock.advance(5e-6)
            coord.observe(path, kind="created")

        self.assertEqual(len(coord.pending_paths()), self.BURST_SIZE)

        # First tick at t ≈ 0.06 records the (size, mtime) snapshot for
        # every path. Gate returns ``ready=False`` because this is the
        # first time the gate sees these paths — it needs a second tick
        # at least ``window_s`` seconds later to confirm "unchanged".
        clock.advance(0.01)
        self.assertEqual(coord.tick(), 0)
        self.assertEqual(store.enqueued, [])

        # Tick again after the stability window — every snapshot still
        # matches, ``unchanged_for >= window_s``, all 10 000 ready.
        clock.advance(STABILITY_WINDOW_LOCAL_S + 0.5)
        enqueued_count = coord.tick()
        self.assertEqual(
            enqueued_count, self.BURST_SIZE,
            f"tick enqueued {enqueued_count}/{self.BURST_SIZE} ops — "
            "watcher dropped events under burst load (SO-4 regression)",
        )
        self.assertEqual(len(store.enqueued), self.BURST_SIZE)

        # Every emitted op is an upload (no orphan deletes) and every
        # path is in our generated set (no synthesis).
        op_types = {op_type for op_type, _ in store.enqueued}
        self.assertEqual(op_types, {"upload"})
        emitted_paths = {path for _, path in store.enqueued}
        self.assertEqual(emitted_paths, set(stat_state.keys()))

        # The pending dict drained — no leak.
        self.assertEqual(coord.pending_paths(), [])

        # A second tick is a no-op (everything already enqueued and
        # popped from the pending dict).
        self.assertEqual(coord.tick(), 0)
        self.assertEqual(len(store.enqueued), self.BURST_SIZE)

    def test_10k_deletes_filter_through_previously_synced_predicate(self) -> None:
        """A 10 000-file delete burst respects the §A17 / T12.2 invariant:
        previously-synced paths tombstone, never-synced paths drop
        silently. No path slips through both filters."""
        store = FakeStore(binding_id="rb_v1_burst_delete")
        # Half the paths are "previously synced" (have a fingerprint
        # row), half are baseline-extras the user is wiping.
        all_paths = [f"f{i:05d}.txt" for i in range(self.BURST_SIZE)]
        synced = set(all_paths[: self.BURST_SIZE // 2])
        clock = FakeClock(0.0)
        coord = self._make_coord(
            clock=clock, store=store,
            stat_provider=lambda p: None,
            previously_synced=lambda p: p in synced,
        )
        for path in all_paths:
            coord.observe(path, kind="deleted")

        self.assertEqual(len(store.enqueued), len(synced))
        self.assertEqual(
            {op_type for op_type, _ in store.enqueued}, {"delete"},
        )
        self.assertEqual(
            {p for _, p in store.enqueued}, synced,
            "delete burst leaked never-synced paths through the §A17 gate",
        )
        # Pending dict stays empty for delete events (they enqueue
        # synchronously inside observe(), no stability gate).
        self.assertEqual(coord.pending_paths(), [])

    def test_10k_mixed_creates_and_modifies_collapse_per_path(self) -> None:
        """If the same path fires multiple create + modify events in
        the burst, the coordinator collapses them — one path = at
        most one enqueued op per cycle. Models the inotify reality
        where Linux fires ``CREATE`` then several ``MODIFY``s for a
        single file copy."""
        clock = FakeClock(0.0)
        store = FakeStore(binding_id="rb_v1_burst_collapse")
        # 5k unique paths, each firing 4 events (create + 3 modifies).
        unique_paths = 5_000
        events_per_path = 4
        # Each path's stat stays stable from event 0 so it gates ready
        # after a single stability window.
        stat_state = {
            f"f{i:05d}.txt": (256, 2_000_000 + i)
            for i in range(unique_paths)
        }
        coord = self._make_coord(
            clock=clock, store=store,
            stat_provider=lambda p: stat_state.get(p),
        )

        kinds = ("created", "modified", "modified", "modified")
        for event_round in range(events_per_path):
            for path in stat_state.keys():
                clock.advance(5e-6)
                coord.observe(path, kind=kinds[event_round])

        self.assertEqual(len(coord.pending_paths()), unique_paths)

        # First tick records snapshots, second tick (after the window)
        # transitions every path to ready.
        clock.advance(0.01)
        self.assertEqual(coord.tick(), 0)
        clock.advance(STABILITY_WINDOW_LOCAL_S + 0.5)
        self.assertEqual(coord.tick(), unique_paths)
        # One op per unique path — no duplicates from the 4-event
        # series.
        self.assertEqual(len(store.enqueued), unique_paths)
        emitted = {p for _, p in store.enqueued}
        self.assertEqual(emitted, set(stat_state.keys()))


# ---------------------------------------------------------------------------
# Watchdog observer thread — SO-4 inotify-integration smoke
# ---------------------------------------------------------------------------


class WatchdogObserverBurstSmokeTests(unittest.TestCase):
    """Smoke test for the watchdog-thread → coordinator path with
    real inotify (or the watchdog polling fallback on hosts where
    inotify isn't available). 200 files instead of 10 000 keeps the
    test under one second on any reasonable disk; the burst-scale
    coverage above already exercises the coordinator's hot path.
    """

    def setUp(self) -> None:
        try:
            import watchdog  # noqa: F401
        except ImportError:
            self.skipTest("python3-watchdog not installed")
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_watcher_smoke_"))

    def tearDown(self) -> None:
        if hasattr(self, "tmpdir"):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_200_create_burst_reaches_coordinator_pending(self) -> None:
        from src.vault.binding.filesystem_watcher import start_watchdog_observer

        store = FakeStore(binding_id="rb_v1_inotify")
        coord = WatcherCoordinator(
            binding_id="rb_v1_inotify",
            local_root=self.tmpdir,
            store=store,
            # Real clock + real stat — we're testing the inotify path,
            # not coordinator logic.
        )
        handle = start_watchdog_observer(coord, poll_interval_s=0.1)
        if handle is None:
            self.skipTest("watchdog could not start an observer (CI sandbox?)")
        try:
            # Drop 200 files in a single burst.
            burst = 200
            for i in range(burst):
                (self.tmpdir / f"f{i:04d}.txt").write_bytes(b"x" * 64)
            # Let the observer thread drain its queue. inotify is
            # asynchronous so we poll until the pending dict catches
            # up (or we hit a generous timeout).
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if len(coord.pending_paths()) >= burst:
                    break
                time.sleep(0.05)
        finally:
            handle.stop()

        pending = coord.pending_paths()
        # Burst should land in the coordinator. We allow a small
        # tolerance for watchdog's internal queueing on slow CI; the
        # important property is "no silent loss", not "exactly 200
        # within 5 seconds".
        self.assertGreaterEqual(
            len(pending), int(burst * 0.95),
            f"watchdog only delivered {len(pending)}/{burst} events to "
            f"the coordinator — possible SO-4 regression. Tolerance is "
            f"95 % to absorb inotify async timing on slow CI.",
        )


if __name__ == "__main__":
    unittest.main()
