# Phase D — Android streaming client

**Status: DRAFT** — not yet started. Drops in the Android equivalent
of the desktop Phase C streaming client. Server Phase A + B are
landed and deployed at `https://hawwwran.com/SERVICES/desktop-connector/`;
desktop Phase C is landed. Classic transfers still work byte-for-byte.

Companion to `docs/plans/streaming-improvement.md` (protocol + invariants)
and `docs/plans/desktop-streaming-relay-plan.md` (desktop mirror).
Phase D sub-phases intentionally mirror C.1 → C.7 one-to-one so the
review shape, ordering, and acceptance criteria match. Sub-phase
boundaries are where we land commits and stop to confirm the
phone's behaviour over ADB before moving on.

Correctness over speed. Each sub-phase is independently landable. Old
client (classic) + new server keeps working; new client (streaming) +
old server falls back to classic via the existing `/api/health`
capability probe.

---

## Where today's Android code is (relevant entry points)

- `android/app/src/main/kotlin/com/desktopconnector/network/ApiClient.kt`
  - `initTransfer(tid, recipient, meta, chunkCount): InitOutcome` —
    enum `OK | STORAGE_FULL | TOO_LARGE | FAILED`. No `mode` param.
  - `uploadChunk(tid, i, data): JSONObject?` — null on any failure.
  - `downloadChunk(tid, i): ByteArray?` — null on any failure.
  - `ackTransfer(tid): Boolean` — transfer-level only.
  - `cancelTransfer(tid): Boolean` — body-less DELETE.
  - `authObservations: SharedFlow<AuthObservation>` — already used for
    401/403 latching; keep intact.
  - No `ackChunk`, no `abortTransfer(reason)`, no capability probe.
- `android/app/src/main/kotlin/com/desktopconnector/network/UploadWorker.kt`
  - WorkManager worker. `doWork() -> Result`.
  - Classic upload loop: `initTransfer` → per-chunk
    `uploadChunkWithRetry` (5 s cadence, 120 s budget) → progress
    writes via `transferDao.updateProgress(id, uploaded, total)` →
    `COMPLETE` on last chunk.
  - Handles 507 by marking `WAITING` + `Result.retry()` (WorkManager
    re-schedules); gives up after `STORAGE_FULL_MAX_WINDOW_MS` = 30 min.
- `android/app/src/main/kotlin/com/desktopconnector/service/PollService.kt`
  - Foreground service. `handleIncomingTransfer(row, …)` → either
    `handleFnTransfer` (`.fn.*` commands) or `receiveFileTransfer`.
  - `receiveFileTransfer`: `.incoming_<tid>.part` append-write loop,
    3-attempt `downloadAndDecryptChunk` per chunk, transfer-level
    `ackTransfer` at the end. No per-chunk ACK today.
  - Delivery tracker (`deliveryTrackerLoop`) writes
    `deliveryChunks` / `deliveryTotal` to Room on the sender side.
- `android/app/src/main/kotlin/com/desktopconnector/data/QueuedTransfer.kt`
  - Room entity. Existing columns: `chunksUploaded`, `totalChunks`,
    `deliveryChunks`, `deliveryTotal`, `delivered`, `errorMessage`,
    `transferId`, `direction`, `status`, …
  - Enum `TransferStatus`: `QUEUED, PREPARING, WAITING, UPLOADING,
    COMPLETE, FAILED`.
- `android/app/src/main/kotlin/com/desktopconnector/ui/transfer/TransferViewModel.kt`
  - `refreshTransfers()` paints UI from `transferDao.getRecent()` and
    scrubs `WAITING` rows older than 30 min into `FAILED`.
  - `cancelAndDelete(t)` calls `api.cancelTransfer` + cancels
    WorkManager by tag + deletes the row.
  - `isInFlight(t)` decides whether to show the confirmation dialog.
- `android/app/src/main/kotlin/com/desktopconnector/ui/HomeScreen.kt`
  - `TransferItem` composable: status → label + colour mapping.
  - Swipe-to-dismiss confirmation dialog for in-flight rows.
- `android/app/src/main/kotlin/com/desktopconnector/service/FcmService.kt`
  - `onMessageReceived`: handles `type ∈ {fasttrack, ping, <anything
    else> → fcmWakeSignal=true}`. Does NOT handle `stream_ready` or
    `abort` explicitly today — they'd fall into the catch-all.

---

## Non-goals (carried from streaming-improvement.md §9)

- No chunk-parallel upload. Sequential send, sequential receive.
- No streaming for `.fn.*` transfers — guard
  `displayName.startsWith(".fn.")` and force `mode=classic`.
- No "connecting / negotiating" UI micro-state.
- No formal resume UI across app restarts (server-side
  `chunks_downloaded` gives natural resume on restart).

Android-specific non-goals for this phase:

- **No instrumentation / Espresso tests.** We don't have a CI running
  emulators and the test surface is big enough without them. Android
  tests this phase go through the existing desktop `tests/protocol/`
  harness (by driving the deployed server from both sides and
  observing via `adb logcat`) + a new unit-test pass on pure Kotlin
  helpers where the logic is worth pinning.
- **No Room auto-migration.** We write a manual `Migration` subclass
  so we control the ALTER-TABLE shape and can verify it in testing
  before a user upgrades.
- **No changes to the `messaging/` dispatcher.** Streaming wake events
  (`stream_ready`, `abort`) arrive as plain FCM data messages handled
  inside `FcmService.onMessageReceived`, not as `DeviceMessage`s —
  same as the existing `ping` / `fasttrack` wakes.

---

## Sub-phase plan

Each sub-phase has a **what changes**, **why in this order**, and
**acceptance criteria**. Each ends on a buildable `./gradlew
assembleRelease` + a hand-test over ADB against the deployed server.

Every sub-phase writes `adb logcat` evidence to the chat — the user
runs `adb logcat -v time DesktopConnector:D *:W` in a terminal and
pastes the relevant window when I ask. Server-side evidence comes
from the `_devel_` tool (`transfers.php`, `storage.php`, `logs.php`).

### D.1 — Protocol plumbing in `ApiClient.kt` (pure additive)

**What changes:**

1. New sealed class `ChunkUploadResult` (`Ok | StorageFull | Aborted |
   TooEarly(retryAfterMs) | NetworkError | AuthError | ServerError`)
   and mirror `ChunkDownloadResult`. Existing `uploadChunk` /
   `downloadChunk` gain a typed-outcome overload that returns these;
   the bool-returning legacy signatures keep working for classic
   callers until D.4 migrates them.
2. `initTransfer(...)` gains `mode: String = "classic"` and returns
   `InitResult(outcome: InitOutcome, negotiatedMode: String)`.
   Existing callers keep passing no `mode`; `negotiatedMode` stays
   `"classic"` until D.4.
3. New `ackChunk(tid, chunkIndex): Boolean` — POST
   `/api/transfers/{id}/chunks/{i}/ack`, wraps the auth-emit pattern
   already used by `ackTransfer`.
4. New `abortTransfer(tid, reason: String): Boolean` — body-ful
   DELETE `{reason: "..."}`. Keep `cancelTransfer(tid)` as a wrapper
   calling `abortTransfer(tid, "sender_abort")`.
5. New `getCapabilities(): Set<String>` — calls `/api/health`
   unauthenticated, parses `capabilities` array, caches on the
   `ApiClient` instance with a 60 s TTL. Result may contain
   `"stream_v1"`.
6. Thread the `Retry-After` header and `abort_reason` JSON body
   through the typed results so callers can surface them without
   re-reading the response.

**Why first:** pure additive surface, unit-testable against the
deployed `_devel_`-backed server without touching UploadWorker /
PollService. If we got the contract wrong (wrong JSON shape, wrong
header name, wrong hex for `stream_v1`) we catch it here before any
state-machine code depends on it.

**Acceptance:**

- `./gradlew assembleRelease` green. APK installs.
- Existing classic transfers still work (no regressions on the
  legacy code paths — they're byte-for-byte unchanged).
- Hand-test with ADB:
  - `adb shell` into the app, or exercise via a new button (staged
    for D.1 or just log-flush on startup): capability probe comes
    back with `stream_v1` when hitting the deployed server.
  - Init a transfer with `mode="streaming"` from the Android side
    directly → `negotiatedMode == "streaming"`. Confirm server side
    via `_devel_/transfers.php`.
  - GET a chunk that doesn't exist yet → `TooEarly(retryAfterMs)`.
  - DELETE with `reason="sender_abort"` → transfer row flips
    aborted (confirm via `_devel_/transfers.php`).

### D.2 — Room schema + TransferStatus vocabulary (no runtime switch-on yet)

**What changes:**

1. Bump `AppDatabase.version` from N → N+1. Add a `Migration{N}_{N+1}`
   class that runs ALTER-TABLE for the new columns. Each column is
   nullable or has a sensible default so old rows keep working.
2. New columns on `QueuedTransfer`:
   - `mode: String = "classic"`
   - `negotiatedMode: String? = null`
   - `abortReason: String? = null`
   - `failureReason: String? = null` (matches desktop; lets D.5 stamp
     `"quota_timeout"` etc.)
   - `waitingStartedAt: Long? = null` (epoch-ms; mirrors desktop's
     `waiting_started_at` for the 30 min scrub).
3. Extend `TransferStatus` enum with the new values:
   `SENDING`, `WAITING_STREAM`, `DELIVERING`, `ABORTED`. Only the
   constants land; nothing writes them yet.
4. DAO additions (future-proof for D.3 / D.4 — no call sites yet):
   - `markAborted(id, reason)`
   - `markWaitingStream(id, startedAt)`
   - Query for `WAITING_STREAM` rows older than a threshold (used by
     zombie scrub in D.5).
5. `HomeScreen` `TransferItem` gains pass-through branches for the
   new statuses: `SENDING` → blue, `WAITING_STREAM` → yellow,
   `ABORTED` → orange. Branches exist but nothing in the wild
   produces these statuses yet.

**Why second:** wiring the Room schema + enum additions in before any
state-machine code writes them means D.3 / D.4 can't accidentally
write a column / enum value the reader doesn't understand. Migrations
are the riskiest single thing on Android; landing it alone means we
can install-and-upgrade from a paired build and prove migration works
without any other change in scope.

**Acceptance:**

- `./gradlew assembleRelease` green.
- Install-over-upgrade from a paired pre-D.2 build: existing rows
  survive, `mode` / `negotiatedMode` / etc. land with their defaults,
  no crash on first launch.
- Manual smoke: hand-insert a Room row (via an ADB debug shell or a
  one-shot gated code path we remove after this phase) with each new
  status value; confirm the HomeScreen renders a readable row with
  the right colour + label.
- `TransferViewModel.isInFlight()` unchanged behaviour for all
  existing rows; new statuses that we deliberately don't set yet
  have no observable effect.

### D.3 — Recipient streaming receive loop

**What changes:**

1. `PollService.handleIncomingTransfer` reads the new `mode` field
   from the pending-list row (server already sends it per Phase A).
   Branches:
   - `mode == "classic"` → existing `receiveFileTransfer` path.
   - `mode == "streaming"` → new `receiveStreamingTransfer`.
2. `receiveStreamingTransfer`:
   - Open `.incoming_<tid>.part` for append-write (same naming as
     classic — reuses the orphan-part sweep).
   - For `i in 0 until chunkCount`:
     - `apiClient.downloadChunk(tid, i)` typed outcome:
       - `Ok(bytes)` → decrypt → write → fsync every N chunks (or on
         last) → `apiClient.ackChunk(tid, i)`. Room write:
         `transferDao.updateProgress(dbId, i+1, chunkCount)`.
       - `TooEarly(retryMs)` → sleep `retryMs ?: 1_000`, own ramp 1 s
         → 2 s → 4 s → 8 s cap 10 s; reset ramp on any non-`TooEarly`.
       - `Aborted(reason)` → `markAborted(dbId, reason)`, wipe
         `.part`, stop. (Server already wiped blobs.)
       - `NetworkError` → classic 3-attempt 2 s retry (same as today).
         On exhaustion → `apiClient.abortTransfer(tid, "recipient_abort")`
         + `markAborted(dbId, "recipient_abort")` + status `FAILED`
         with `failureReason = "network"`.
   - Per-chunk "no data for N minutes" budget: 5 min of continuous
     `TooEarly` without any advancement →
     `abortTransfer(tid, "recipient_abort")`, status `FAILED`.
   - After the final chunk's `ackChunk`: `File.renameTo` the
     `.part` → final `displayName` under `DesktopConnector/`. **Do
     NOT** send the transfer-level `ackTransfer` — per-chunk ACKs
     already finalised delivery server-side.
3. New helper `downloadChunkStreaming` replaces the classic
   `downloadAndDecryptChunk`'s retry loop for the streaming branch;
   classic helper stays untouched.
4. Poller's orphan-part sweep stays unchanged.
5. `FcmService.onMessageReceived`: add `"stream_ready"` and `"abort"`
   to the `when(type)` — both set `PollService.fcmWakeSignal = true`
   (same behaviour as a classic `transfer_ready` wake). The server's
   current wake shape is intentionally a single "something changed,
   poll" signal; we don't route the wake payload into the dispatcher.

**Why third:** even without a streaming sender on Android, we can
exercise the recipient path end-to-end by driving the _desktop_
streaming sender (Phase C, landed) against the phone. Receiving first
also proves the abort / per-chunk ACK plumbing in the more naturally-
controllable direction (the desktop is scriptable from a shell
session).

**Acceptance:**

- `test_loop.sh` on the desktop (classic) still green — we changed
  nothing on the desktop side.
- `./gradlew assembleRelease` green. APK installs + upgrades.
- Hand-test over ADB:
  - Desktop streaming send → phone receives: `.part` grows
    incrementally (observable via `adb shell ls -l …`), history row
    shows `Downloading X/N` advancing in real time, final file lands
    in `DesktopConnector/`, `_devel_/storage.php` shows blobs being
    deleted after each ACK.
  - Force-abort from sender (desktop sends then aborts): phone row
    flips to `Aborted`, `.part` gone, `adb logcat` shows the abort
    event with the right reason.
  - Recipient-side abort (swipe-delete a downloading row in
    HomeScreen before D.5 wires the UI — call the helper directly
    from a staging button or exercise via `adb`): `DELETE` with
    `reason=recipient_abort` fires, `.part` gone, row `Aborted`.

### D.4a — Sender streaming state machine (no delivery-tracker change)

**Scope is deliberately narrow**: the chunk-by-chunk state machine
inside `UploadWorker`, exercised purely by typed ApiClient outcomes.
No change to the delivery tracker, no new transitions into `SENDING`
yet. The phone can complete a streaming send end-to-end; delivery
observation happens through the *existing* `deliveryChunks` /
`deliveryTotal` tracker path (which already fires on rows that reach
`COMPLETE`) so classic-style "Delivered" rendering keeps working
unchanged. D.4b is what introduces `SENDING` and rewires the tracker.

**What changes:**

1. `UploadWorker.doWork()`:
   - Calls `apiClient.getCapabilities()` before init (results cached
     across invocations; first call warms the cache).
   - `shouldRequestStreaming = "stream_v1" in capabilities &&
     !displayName.startsWith(".fn.") && row.mode != "classic-forced"`
     (the forced override is a belt-and-braces for the rare case
     where a user / admin explicitly disables streaming on one row).
   - Init with `mode = shouldRequestStreaming ? "streaming" : "classic"`.
   - Capture `negotiatedMode` into the Room row at init time.
2. Branch on `negotiatedMode`:
   - `"classic"` → existing `uploadChunkWithRetry` loop unchanged.
   - `"streaming"` → new `uploadStreamLoop`.
3. `uploadStreamLoop` — state machine, sequential chunks:
   - `apiClient.uploadChunk(tid, i, data)` typed outcome:
     - `Ok` → bump `chunksUploaded` via `transferDao.updateProgress`,
       batched to every ~500 ms OR on state transition.
       Row status stays `UPLOADING` throughout (no `SENDING`
       transition in D.4a).
     - `StorageFull(507)` → flip row to `WAITING_STREAM`, stamp
       `waitingStartedAt = SystemClock.elapsedRealtime()`, backoff
       2 → 4 → 8 → 16 → cap 30 s. Total window
       `STORAGE_FULL_MAX_WINDOW_MS` = 30 min. On window expiry →
       `apiClient.abortTransfer(tid, "sender_failed")`, row
       `FAILED` with `failureReason = "quota_timeout"`. On next
       `Ok` inside the window, row flips back to `UPLOADING` and
       `waitingStartedAt` clears.
     - `Aborted(reason)` → stop upload, row `ABORTED` with
       `abortReason = reason ?: "recipient_abort"`.
     - `NetworkError` / other non-fatal → classic 5 s cadence,
       120 s budget. On exhaustion →
       `apiClient.abortTransfer(tid, "sender_failed")`, row
       `FAILED` with `failureReason = "network"`.
   - Final chunk `Ok` → row transitions to `COMPLETE` (same as the
     classic path). The existing delivery tracker picks up from
     there exactly as it does today.
4. WorkManager retry policy: today we return `Result.retry()` on
   `STORAGE_FULL` to let WorkManager reschedule. Streaming stays
   inside `uploadStreamLoop` until the 30 min window expires, so
   WorkManager's backoff is no longer the driver for streaming.
   Document this in the worker's KDoc — classic keeps the old
   behaviour so non-streaming builds are unaffected.
5. Wake-lock policy for streaming: `PARTIAL_WAKE_LOCK` (2 min
   timeout refreshed per chunk upload or per quota retry) + WiFi
   lock, mirroring the receiver's existing policy. Released
   deterministically in `finally` on every terminal transition
   (`COMPLETE` / `FAILED` / `ABORTED`).

**Why fourth-a:** the state machine is a pure function of typed
outcomes — we can unit-test it against a hand-built fake
`ApiClient` (no emulator, no USB phone, `./gradlew test`) and
pin every branch before a real phone ever sees the code. Splitting
this from D.4b means a regression later can be bisected cleanly:
if `uploadStreamLoop`'s terminal states are wrong, the issue is
here; if delivery painting / three-phase transitions are wrong,
the issue is in D.4b.

**Acceptance:**

- `test_loop.sh` (classic) still green on the desktop.
- `./gradlew test` passes the new `UploadStreamLoopTest` JVM unit
  tests (happy path, 507 with recovery, 507 to timeout, 410 abort,
  network exhaustion). These land in this sub-phase, not deferred
  to D.6.
- `./gradlew assembleRelease` green. APK installs.
- Phone → desktop streaming end-to-end works on the connected
  USB phone against the deployed server: a ~20 MB file picks up
  `mode = streaming`, server-side peak on-disk bytes stay within
  a few chunks (checked via `_devel_/storage.php`), desktop
  receives cleanly, `adb logcat` shows the expected event
  vocabulary.
- During the send the phone's history row shows classic
  `Uploading X/N` throughout, then `Sent` → `Delivered` (via the
  existing tracker path). The `Sending X→Y` label is deliberately
  NOT shown yet — that lands in D.4b.
- 507 injection (tight `storageQuotaMB` on the deployed server
  OR the hermetic harness): phone flips to `WAITING_STREAM` and
  recovers when space frees. UI in D.4a shows the row coloured
  yellow via the D.2 pass-through branch; it won't say "X→Y"
  yet, just the raw counter.
- 410 mid-stream (recipient aborts from the desktop side): phone
  row flips to `Aborted` with `abortReason = "recipient_abort"`.
- Network exhaustion: phone row flips to `FAILED` with
  `failureReason = "network"`.

### D.4b — Sender-side three-phase wiring + delivery-tracker integration

**Scope**: introduce `SENDING` status transitions and extend the
delivery tracker to paint X→Y on in-flight streaming rows. This is
where the concurrency story lives: `uploadStreamLoop` and the
delivery tracker both write to the same Room row at overlapping
cadences. Splitting this from D.4a is the whole point of doing D.4
in two pieces — tracker-vs-sender race conditions and the stall-
safeguard behavioural change are worth reviewing in isolation.

**What changes:**

1. Delivery tracker eligibility predicate (today: rows with
   `status == COMPLETE AND direction == OUTGOING AND !delivered`)
   extends to also include: `(status == UPLOADING OR status ==
   WAITING_STREAM OR status == SENDING) AND negotiatedMode ==
   "streaming"`. The 500 ms tick and per-poll 750 ms timeout stay
   identical.
2. `UploadWorker.uploadStreamLoop` gains a `SENDING` transition:
   when the delivery-tracker observation reports
   `chunks_downloaded > 0` on the server side, the sender loop
   flips the row from `UPLOADING` to `SENDING`. Mechanism: read
   the Room row's `deliveryChunks` on every `uploadChunk` return;
   when it first crosses zero, stamp `SENDING`. Single writer
   for the `SENDING` transition — the upload loop, not the
   tracker — so there's no write-write race on `status`.
3. Final-chunk transition in streaming mode: the sender's
   `Ok` on the last chunk no longer flips to `COMPLETE` directly.
   Instead it stamps `chunksUploaded = totalChunks` and leaves
   `status` as `SENDING`. The tracker observes the final
   delivery ACK (`downloaded == 1` in sent-status) and flips
   `status` to `DELIVERED` via the existing `markDelivered`
   path. Intermediate `COMPLETE` is no longer emitted for
   streaming — the "I finished uploading" moment is implicit in
   `chunksUploaded == totalChunks` while `status == SENDING`.
   Classic's COMPLETE → DELIVERED path is untouched.
4. 2 min stall safeguard (existing):
   - Classic behaviour unchanged.
   - Streaming behaviour changes per `streaming-improvement.md
     §5.5`: on stall, the tracker clears `deliveryChunks` /
     `deliveryTotal` only — it does NOT flip `status` or mark
     the row `FAILED`. The sender keeps uploading independently;
     the recipient has its own 5 min `TooEarly` budget; if
     delivery really is stuck the recipient will abort and the
     sender will see 410 on its next chunk.
5. WAITING_STREAM display integration: while the row is
   `WAITING_STREAM`, the tracker continues to poll and paint
   `deliveryChunks` / `deliveryTotal` (recipient may still be
   draining already-uploaded chunks). The D.5 UI step consumes
   this to render `Waiting X→Y`.
6. Concurrency audit: every Room write now happens on exactly one
   owner per field.
   - `status`: upload loop only (via `transferDao.updateStatus`).
   - `chunksUploaded`: upload loop only.
   - `deliveryChunks` / `deliveryTotal`: tracker only.
   - `delivered` / `status = DELIVERED`: tracker only (on final
     ACK observation).
   - `abortReason` / `failureReason`: upload loop only.
   Document this contract in a comment near the DAO definitions.

**Why fourth-b:** depends on D.4a (the state machine has to
correctly reach `UPLOADING` / `WAITING_STREAM` / final-chunk Ok
before this sub-phase's transitions mean anything) and on D.2
(fields exist). Deliberately separate so the tracker-vs-sender
concurrency story is the only thing in this commit's diff.

**Acceptance:**

- `./gradlew test` passes a new `SenderDeliveryPhaseTest` JVM
  unit test (or an extension of D.4a's test) that drives the
  upload loop + a fake tracker loop in interleaved fashion and
  confirms the documented field-ownership contract: `status`
  transitions UPLOADING → SENDING → DELIVERED, `chunksUploaded`
  monotonically climbs, `deliveryChunks` only changes on tracker
  ticks.
- `test_loop.sh` (classic) still green.
- Phone → desktop streaming end-to-end now shows the three-phase
  UI over time: brief `Uploading 0/N` → `Sending X→Y` (X = our
  `chunksUploaded`, Y = `deliveryChunks`, both advancing in
  parallel) → `Delivered`. The UI text itself lands in D.5 — the
  acceptance here is that the *Room fields* have the right values
  on every poll tick, inspectable via `adb shell content query`
  or an `_devel_`-free Room inspector.
- 2 min stall: with the desktop receiver paused mid-stream, the
  phone row's `deliveryChunks` / `deliveryTotal` clear (paint
  drops) but `status` stays `SENDING` and upload continues
  independently. On resumption, painting resumes.
- 507 mid-stream: `WAITING_STREAM` row still has tracker painting
  up to whatever the recipient already drained.

### D.5 — HomeScreen + history row actions

**What changes:**

1. `HomeScreen.TransferItem` label + colour branches flesh out
   (stubs from D.2):
   - `SENDING` → blue bar, text `Sending X→Y` where X = our local
     `chunksUploaded`, Y = `deliveryChunks`. Progress fraction =
     `deliveryChunks / deliveryTotal` (what the recipient has),
     with a subtler secondary tick at `chunksUploaded / totalChunks`
     (or a two-tone bar — pick the simpler Compose widget and
     document the choice in the commit).
   - `WAITING_STREAM` → yellow pulsing bar + text `Waiting X→Y` in
     the brand yellow.
   - `ABORTED` → no bar, orange text `Aborted` with optional
     suffix from `abortReason` ("sender cancelled" / "recipient
     cancelled" / "sender gave up").
2. `TransferViewModel.refreshTransfers()` zombie scrub grows a
   sibling pass for `status == WAITING_STREAM` using
   `waitingStartedAt` + the same 30 min rule. Scrubbed rows get
   `failureReason = "quota_timeout"` → status `FAILED`. DB writes
   only fire on value change (same as today).
3. `TransferViewModel.cancelAndDelete(t)` branches on mode:
   - `t.mode == "streaming"` or `t.negotiatedMode == "streaming"` →
     `apiClient.abortTransfer(tid, "sender_abort")`.
   - else → `apiClient.cancelTransfer(tid)` (back-compat wrapper,
     unchanged).
   - WorkManager tag cancellation + Room row delete identical.
4. Recipient-side row deletion during streaming: when the user
   swipe-deletes a `status == UPLOADING` row whose `direction ==
   INCOMING` and `mode == "streaming"`, the VM calls
   `apiClient.abortTransfer(tid, "recipient_abort")` FIRST, then
   removes the row. PollService's `receiveStreamingTransfer` also
   reads the Room row on each iteration; if it's gone mid-loop, it
   aborts cleanly on the next chunk attempt (same pattern as the
   classic receiver's "row deleted → ack + stop" behaviour).
5. `isInFlight(t)` updated: `WAITING_STREAM` and `SENDING` both count
   as in-flight (dialog fires on swipe-delete); `DELIVERING` too
   (the sender is still on the wire until the recipient finishes).
   `ABORTED` counts as NOT in-flight (no confirmation dialog; it's
   already terminal).

**Why fifth:** UI depends on D.2 (statuses exist) + D.3 + D.4a / D.4b
(something actually writes them). Deliberately separate from D.4 so
the sender state machine and tracker-vs-sender concurrency can be
reviewed / bisected without Compose churn in the same diff. The
`Sending X→Y` label specifically depends on D.4b having wired the
tracker to paint on in-flight streaming rows.

**Acceptance:**

- Manual UI sweep over ADB: each new status renders readably,
  progress fractions animate correctly in both directions, swipe-
  delete on an in-flight streaming transfer propagates to the
  server and the other side sees `Aborted`.
- Zombie-WAITING_STREAM scrub: temporarily force a row to stay in
  `WAITING_STREAM` past 30 min (clock-skew the `waitingStartedAt`
  via a one-shot debug code path OR via the `_devel_` tool
  simulating long quota backpressure) → row flips to `FAILED
  (quota exceeded)` without needing to reopen the screen.
- Visual QA: brand-palette colours match the CLAUDE.md table
  (yellow `#FDD00C` for WAITING_STREAM in dark theme, `#FAA602` in
  light; orange `#EA7601` for ABORTED).

### D.6 — Integration test + test_loop.sh coverage

**What changes:**

1. Extend `tests/protocol/test_server_contract.py` if D.1 / D.4
   surface any new contract shape not already covered — most
   likely unnecessary; Phase A server tests already pin the
   surface. Re-check after D.4 lands.
2. New pure-Kotlin unit tests (local JVM, no emulator) for the
   state machines:
   - `UploadStreamLoopTest` — drive the state machine with a
     hand-built fake `ApiClient`; exercise 507 backoff + 30 min
     cap, 410 abort, final-chunk happy path, and the interleaving
     where delivery overtakes upload.
   - `ReceiveStreamingTransferTest` — same shape for the recipient
     loop (425 budget, 410 mid-stream, network-error abort path).
   - Both live under `android/app/src/test/kotlin/` and run via
     `./gradlew test` in CI-lite fashion (the user runs them
     locally before commits).
3. New `tests/protocol/test_loop_android_streaming.sh` (or a flag
   in the existing `test_loop.sh`): drives the hermetic PHP
   server with `streamingEnabled=true`, pairs a scripted
   `desktop/src/main.py --headless` against an Android build
   installed on the connected test phone, runs one streaming
   transfer in each direction on a ~20 MB payload, asserts:
   - File hash identical across both sides.
   - `server/storage/` peak size stays below `4 × CHUNK_SIZE = 8 MB`.
   - `transfer.init.accepted mode=streaming` in server logs (no
     classic reservation path hit).
4. Abort tests — phone aborts at chunk K/N; desktop aborts at
   chunk K/N. Assert the opposite side observes `Aborted`.
5. Quota-gate test: tight `storageQuotaMB=2`, verify phone's
   sender flips `WAITING_STREAM` and drains on recipient catch-up.

**Acceptance:**

- `test_loop.sh` (classic) still green.
- Both unit-test suites pass via `./gradlew test`.
- `test_loop_android_streaming.sh` passes on a clean checkout with
  the phone connected via USB.

### D.7 — Cleanup + plan status

**What changes:**

1. Update `docs/plans/streaming-improvement.md` Phase D status block
   (`LANDED` with the commit list).
2. Short entry under CLAUDE.md's "Key design decisions" describing
   the Android streaming state machine and per-chunk ACK contract
   (mirrors the existing desktop "Three-phase transfer state" /
   "Streaming status vocabulary" entries).
3. Remove `_devel_/` from the deployed server (we don't need it
   after D lands; the local `temp/_devel_/` folder stays for
   reference).
4. Dead-code sweep: the legacy `uploadChunk` / `downloadChunk` bool
   overloads from D.1 can be removed if every caller is on the
   typed-outcome path (D.3 + D.4 should have migrated them all).

---

## Risks + mitigations specific to Phase D

1. **Room migration.** Only one unavoidable-on-upgrade risk. We
   write a manual `Migration{N}_{N+1}` and verify install-over-
   upgrade from a paired pre-D.2 build before landing D.3. Rollback
   is a `Migration{N+1}_{N}` drop — but we won't need it because
   all new columns are additive with defaults.

2. **Background throttling eats the sender loop.** UploadWorker is
   a WorkManager worker, which gets foreground-service privileges
   only while PollService's notification is showing. Long-running
   `WAITING_STREAM` (up to 30 min) needs to stay alive under Doze.
   Existing wake lock + WiFi lock policies (CLAUDE.md "Download
   reliability") cover the receiver side; we mirror the pattern
   for the streaming sender in D.4a: `PARTIAL_WAKE_LOCK` (2 min
   timeout refreshed per chunk or per quota retry) plus WiFi lock.

3. **Delivery tracker vs streaming row ownership.** Today the
   delivery tracker writes `deliveryChunks` / `deliveryTotal` on
   COMPLETE rows. Streaming breaks that assumption: the recipient
   starts downloading from chunk 0 while the sender is still
   uploading, so the tracker needs to fire on in-flight rows too.
   This is the entire subject of D.4b — extending the tracker's
   eligibility predicate, introducing the `SENDING` transition,
   and changing the 2 min stall safeguard's semantics in streaming
   mode (clear Y only; don't abort). Splitting D.4 into D.4a (state
   machine) and D.4b (tracker wiring) is a deliberate mitigation
   for this risk — the field-ownership contract is reviewed in
   isolation from the upload state machine.

4. **FCM `stream_ready` wake races with the poll loop.** The wake
   just flips `fcmWakeSignal = true`; the poll loop coalesces
   multiple wakes within a cycle. No new race vs today's
   `transfer_ready` wake — but confirm via `adb logcat` that
   stream_ready wakes actually shorten perceived latency
   (desktop→phone streaming starts in < 2 s with FCM wired).

5. **Multi-process history writes.** Android is single-process for
   the app (Room is fine), unlike desktop's history.json. No
   fcntl-style write batching needed. We DO still batch Room
   writes to every ~500 ms + on state transition to keep the UI
   from thrashing.

6. **The phone's clock vs `waitingStartedAt`.** Using
   `System.currentTimeMillis()` ties the 30 min window to wall
   time, which can jump (time-zone change, NTP sync). Desktop
   uses `time.monotonic()` but Android doesn't have a great
   monotonic long-lived clock across process restarts. Use
   `SystemClock.elapsedRealtime()` plus the row's existing
   `createdAt` as a sanity check — clamp the elapsed-real delta
   against `createdAt`-derived wall time so a backwards NTP jump
   can only shorten, not extend, the window.

7. **Stall on slow cellular.** Streaming's per-chunk ACK means
   more round-trips than classic. On a bad mobile link, per-chunk
   RTT matters more. The recipient's 5 min `TooEarly` budget
   already accommodates this; sender's 120 s `NetworkError` budget
   may be tight on flaky cellular. If we see real-world aborts on
   slow networks during D.6 testing, we bump the sender's
   network-error budget before landing D.7.

---

## Sequencing summary

```
D.1  ApiClient.kt protocol       → additive, server-only reviewable
D.2  Room schema + statuses      → additive, Migration class, no writer
D.3  recipient streaming loop    → depends on D.1 / D.2
D.4a sender state machine        → depends on D.1 / D.2; parallel to D.3 in theory
D.4b tracker wiring + SENDING    → depends on D.4a
D.5  Compose UI + row actions    → depends on D.3 + D.4b
D.6  integration tests           → ties it all together
D.7  docs + _devel_ decommission → final pass
```

Each commit is reviewable in isolation. No sub-phase leaves the tree
in a state where classic transfers regress. Old server + new client:
capability probe comes back empty, streaming disabled at source. New
server + old client: no `mode` field in init, server defaults to
classic (same path Phase A was designed around).

After Phase D is green end-to-end against the deployed server, Phase E
(integration + cleanup, per `streaming-improvement.md §8.5`) is the
only remaining work.
