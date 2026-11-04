# Architecture Decisions

This file is the long-term tracker for **non-trivial architectural
decisions** in Desktop Connector. When a choice would otherwise force a
future maintainer to re-derive the reasoning from code or commit
history — protocol shape, persistence strategy, security boundary,
cross-runtime contract, irreversible data-format pick, dependency
adoption / removal — write it down here.

This complements (not replaces) the existing surfaces:

- `CLAUDE.md` → the "Key design decisions" section captures the legacy
  / cross-cutting decisions in narrative form. Going forward, *new*
  decisions land **here** as discrete dated entries; CLAUDE.md gets a
  one-liner pointer when relevant.
- `docs/plans/` → working notes for in-flight features; ephemeral.
- `docs/protocol/` → the wire spec; describes what is, not why.
- `docs/diagnostics.events.md` → emitted-event catalog.

## When to add an entry

Yes:

- Choosing a wire format, encryption mode, or AAD shape.
- Settling on a state machine (e.g. transfer phases, streaming abort
  transitions).
- Picking between two non-obvious implementations and rejecting the
  other on durable grounds.
- Adding / removing a dependency or build tool.
- Changing a security boundary or threat-model assumption.
- Adopting a backwards-incompatible behaviour gate.

No (skip the entry, just commit normally):

- Bug fixes that restore intended behaviour.
- Single-call refactors / renames / cleanups.
- UI copy or styling tweaks.
- Test reorganisation.
- Anything fully captured by a clear commit message + the code it
  changes.

## Entry format

Each entry is a level-3 heading dated `YYYY-MM-DD`. The body is short —
prose, not bullets — and answers four questions:

1. **What** did we decide.
2. **Why** that and not something else (the rejected alternatives).
3. **Where** the decision is anchored in code (paths, key functions).
4. **Status** — `accepted` / `superseded by <date>` / `deprecated`.

Newer entries go on top so the latest decisions are visible without
scrolling.

### Template — copy this for new entries

```markdown
### YYYY-MM-DD — Short title (verb-led, ≤ 70 chars)

**Status:** accepted.

**Context.** What problem we're solving and why now.

**Decision.** What we chose. One paragraph max.

**Alternatives.** What we rejected and why — the bit future-you will
want when arguing whether to flip the decision.

**Anchor.** Files / functions / commits that implement it.
```

---

## Entries

### 2026-05-13 — Android delivery tracker gives up on absent rows + 12h orphan sweep

**Status:** accepted.

**Context.** `android_logs_9.txt` (2026-05-13) showed `u0a454` at 118 mAh
`mobile_radio:fgs` in a 2h 29m on-battery window — ~745 mAh / 10h
equivalent, ~10× the radio-tail-cost target. The plan's
`bf83c67`-round acceptance criteria for cancellation + skip streaks +
`poll_timeout_3000ms` were all met. The cost was hiding in 187
`delivery.tracker.skipped` events over a 3h post-clear AppLog window
that contained **zero** Android-side `transfer.upload.completed`
events. Phantom outgoing transfers from earlier test sessions
(streaming `android_logs.txt` uploads in rounds `_4`/`_5`/`_6`) sat
in Room with `delivered=0` AND active status, keeping
`getActiveDeliveryIds()` non-empty every screen-on second.

Two leaks in `PollService.runDeliveryPoll`: (1) `val s = byId[tid] ?:
continue` silently bypassed the 2-min stall safeguard when the
server's `/sent-status` no longer returned the row — evidence is the
total absence of `delivery.tracker.stall` across all 9 captured log
sessions. (2) The streaming "deliberately don't add to
`trackerGaveUp`" branch kept polling forever for streaming rows
whose `UploadWorker` was long dead.

**Decision.** Two complementary fixes, both built on pure decision
helpers in `service/DeliveryTrackerDecisions.kt` so the policy is
JVM-unit-testable.

*Fix A (runtime).* `runDeliveryPoll` now consults
`trackerAbsentDecision(prevTimestampMs, nowMs, stallTimeoutMs)` when
`/sent-status` omits a tracked tid. First absent observation seeds a
new `trackerAbsentSince` map (separate from `trackerLastProgress` so
flicker doesn't corrupt the present-row clock); subsequent ticks
within `DELIVERY_STALL_TIMEOUT_MS` (2 min) keep waiting; past the
window the tid joins in-memory `trackerGaveUp` and
`clearDeliveryProgress` zeroes the deliveryChunks/Total fields. The
present-row path resets the absent clock on observation so a
recovered row doesn't trip a false give-up.

*Fix B (startup).* New `sweepOrphanOutgoingTransfers` coroutine runs
once on `PollService.onCreate` after a 5 s settle delay. It queries
`getStaleUndeliveredOutgoing(ageThreshold)` for outgoing rows older
than `ORPHAN_SWEEP_AGE_SECONDS` (12 h) with `delivered=0` and an
active status. One `/sent-status` call decides per-row via
`orphanSweepAction(localStatus, presentInSentStatus)`: present →
leave; absent + `COMPLETE`/`SENDING` → `markDelivered` (server
pruned a finished transfer); absent + `UPLOADING`/`WAITING_STREAM` →
`markAborted reason="tracking_expired"` (server's 24h
`INCOMPLETE_EXPIRY` pruned a never-finished upload). Network/auth
failures are non-fatal; next service restart retries. 12 h was
chosen to sit above the server's longest non-delivery expiry (24h
`INCOMPLETE_EXPIRY`) by half, giving the server ample time to
resolve the row either way before our sweep decides.

**Alternatives.** (a) Persistent give-up column in Room — rejected;
in-memory `trackerGaveUp` was already the pattern for classic stall
and a new column would need a migration for negligible benefit
(Fix B already drains persistent orphans). (b) Mark all
absent-and-old rows `delivered=1` indiscriminately — rejected; a
streaming row stuck in `UPLOADING` clearly never delivered and
claiming otherwise misleads the user. (c) Server-side
"acked-and-deleted" stub returned in `/sent-status` so the client
can positively confirm delivery without inference — rejected for v1
as a bigger surface change; the local heuristic is good enough and
the server endpoint stays unchanged. (d) Per-tick `markDelivered`
inside the stall branch when status is `COMPLETE`/`SENDING` —
rejected; couples runtime tracker with the assumption-based policy
that belongs at startup boundary, not every 500 ms.

**Anchor.** `android/app/src/main/kotlin/com/desktopconnector/service/DeliveryTrackerDecisions.kt`
(pure helpers); `service/PollService.kt::runDeliveryPoll` (Fix A
wire-in at the `byId[tid] ?: continue` site, ~line 1242);
`service/PollService.kt::sweepOrphanOutgoingTransfers` (Fix B
coroutine launched from `onCreate`);
`data/QueuedTransfer.kt::getStaleUndeliveredOutgoing` (DAO query
backing the sweep);
`android/app/src/test/kotlin/com/desktopconnector/service/DeliveryTrackerDecisionsTest.kt`
(11 cases covering both helpers); `docs/plans/android-radio-tail-cost.md`
"What `_9.txt` showed" + "Changes deployed (2026-05-13)" sections.

---

_(add new decisions above this section header)_
