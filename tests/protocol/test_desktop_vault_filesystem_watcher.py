"""T10.4 — Filesystem watcher: debouncer + stability gate + coordinator."""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_filesystem_watcher import (  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
