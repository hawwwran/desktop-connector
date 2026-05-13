# Android radio tail cost on cellular sleep

Tracking the Android battery investigation triggered by
`android_logs_4.txt` (dumpsys batterystats, 2026-05-12, 10 h 49 m
window). Diagnostic logs planted in the same session; this file is the
post-deploy hand-off so a later session can pick up after the next
dumpsys.

---

## What we detected

`com.desktopconnector` (UID `u0a454`) was the **#1 single-app battery
drain on the device** despite ~0 s of screen time:

- **146 mAh / 10 h 49 m = ~10 % of total computed drain (1467 mAh).**
- Breakdown: `mobile_radio: 137 (94 %)`, `cpu: 9.08`, `wakelock:
  0.110`. CPU and wakelocks are cheap; this is a radio problem.
- App-attributed `mobile_radio` time = 2 h 24 m 4 s.
- 356 761 packets / 204 radio activations (≈ one every 3 min).
- 74.4 MB received / 22.5 MB sent — about 10 MB/h of background
  traffic.
- `FcmService` launched **112×** in 10 h ≈ once every 5.7 min — matches
  the desktop's 5-min background ping cadence exactly.
- Wi-Fi `Sleep time: 100 %` — every byte went over LTE/NR, which has a
  much higher per-byte energy cost than Wi-Fi.

The whole-device cellular kernel was active 94 % of the window
(10 h 10 m of 10 h 49 m). The app's billed 2 h 24 m is its share.

**Wakelocks are clean** — 41 s of partial wakelock in 10 h. The phone
isn't being held awake; the *radio* is being held active.

## Why — what's actually keeping the radio warm

The notify loop's sleep gating IS correctly designed:

- `PollService.pollLoop` (lines 445–476): when
  `!isScreenOn() && FcmManager.isInitialized`, parks in a CPU-only
  `delay(500)` wait. No HTTP fires here.
- `TransferViewModel`'s heartbeat (line 94): parks on
  `!ForegroundTracker.isForeground`. No HTTP while backgrounded.
- `deliveryTrackerLoop` (line 1142): screen-gated.
- `ConnectionManager` heartbeat: only runs from the foreground
  ViewModel — silent in background.

So there is **no rogue background loop**. The radio drain comes from
events that we already know about — they're just more expensive than
budgeted:

1. **Pong-over-cellular on every desktop ping.**
   `FcmService.kt` "ping" handler calls `sendPong()` synchronously.
   Server short-circuits only when `last_seen_at >= time()` (same wall
   second). Once asleep > 0 s, every desktop ping (default 5 min) fires
   FCM, the phone wakes the radio for a single POST, and the LTE/NR
   modem stays in RRC-connected for ~20–30 s of *tail time* after the
   request completes.
   - 12 pongs / h × ~30 s tail ≈ 6 min / h of radio
   - ≈ 60 min / 10 h of `mobile_radio` active time
   - ≈ **~50–55 mAh / 10 h** just on pong tail time, with zero payload
     value

2. **Each screen-on event → one full long-poll cycle.** Screen flipped
   on 55× in 10 h. Each time, the pollLoop drops the screen-off wait
   and runs a 25 s long-poll + ~20 s tail before re-evaluating screen
   state. Most are brief peeks (notifications, ambient display, lock
   screen wake without unlock), so most of those 55 long-polls produce
   nothing useful.
   - 55 events × ~45 s of radio ≈ 40 min / 10 h
   - ≈ **~25–40 mAh**

3. **LTE tail overlap.** Pongs at 5-min intervals plus screen-on
   long-polls leave the radio still in tail when the next event hits.
   Active time aggregates rather than resetting between events.

`(1) + (2)` accounts for the bulk of the observed 137 mAh.

## Diagnostic logs planted (2026-05-12)

So the **next** `dumpsys batterystats` window can attribute the cost
precisely instead of by arithmetic estimate:

| Event | File | Tags added |
|---|---|---|
| `ping.pong.sent` | `service/FcmService.kt:sendPong` | `screen_off`, `metered`, `ok`, `duration_ms` |
| `ping.pong.failed` | `service/FcmService.kt:sendPong` | `screen_off`, `metered`, `duration_ms`, `error_kind` |
| `poll.loop.iteration` (new) | `service/PollService.kt:pollLoop` top | `screen_off`, `metered`, `fcm`, `connected` |

Documented in `docs/diagnostics.events.md` under §`ping` and §`poll`.

### What we expected to see in the next dump

If the diagnosis above is right:

- `ping.pong.sent screen_off=true metered=true` will fire **~100×**
  over a comparable window (every ~5 min while screen-off on cellular).
  Each `duration_ms` will be small (~100–500 ms) — the tail cost isn't
  in the request, it's in what stays warm afterward.
- `poll.loop.iteration screen_off=true metered=true` will fire **~50×**
  per 10 h on cellular sleep — one per screen-on peek where the loop
  dropped out of its wait, ran one cycle, and immediately re-entered
  the wait.
- `poll.loop.iteration screen_off=false metered=false` will dominate
  numerically (Wi-Fi attached, normal use) and is fine — Wi-Fi long-
  polls are cheap.
- Counter-check: `fcm.message.received type=ping` count should equal
  `ping.pong.sent` count. If it doesn't, FCM pings are being silently
  dropped or our pong is throwing early.

### What the dump actually showed (android_logs_5.txt, 2026-05-12 19:06)

New-APK window: **1 h 40 m** (17:21 → 19:01). Phone idle / screen-off
most of it, on cellular the whole time, no transfers, no foreground
app activity. App process restart at 17:21:39 (`App Started`) =
~1 min after `adb install -r`.

| Metric | Observed (1 h 40 m) | Pro-rated to 10 h | Prediction |
|---|---:|---:|---:|
| `ping.pong.sent` total | 19 | ~115 | ~115 ✓ |
| ↳ `screen_off=true metered=true` | 15 | ~90 | ~100 ✓ |
| ↳ `screen_off=false metered=true` | 4 | ~25 | — |
| `poll.loop.iteration` total | 2 | ~12 | — |
| ↳ `screen_off=true` | 1 | ~6 | ~50 ✗ |

**Pong cadence confirmed** — 5-min from the desktop, all 19 on metered
cellular, ~80 % with screen off. `duration_ms` median ~200 ms, max
1275 ms (one outlier — a slow tower probably). All `ok=true`.

**Long-poll iterations refuted as a major cost.** Only 1 screen-off
iteration / 100 min ≈ 6 / 10 h. The screen-off wait branch is doing
its job — the loop genuinely parks. Original estimate of ~50 / 10 h
was based on the device-wide "55 screen-on events" stat, but most
short screen-ons (ambient display, notification peek) don't keep
`pm.isInteractive` true long enough for the pollLoop's wait branch
to release.

**Revised cost attribution (the math):**

- 90 sleeping-metered pongs / 10 h × ~20-25 s effective LTE-tail
  per pong ≈ 30-35 min of modem active time ≈ **~25 mAh / 10 h**.
- That's ~20 % of the previously observed 137 mAh, not the ~40 % first
  estimated. Pongs are still the biggest *fixable* slice on the idle-
  background path, but **transfer activity + foreground use dominate
  the remaining ~110 mAh.**

### Surprise finding: `delivery.tracker.skipped` log spam

When an OUTGOING delivery is in flight and the screen is on, the
500 ms-tick delivery tracker writes `delivery.tracker.skipped
reason=previous_in_flight` to AppLog ~once per second for the
duration. New-APK window shows runs of 50+ consecutive entries.

Symptoms:
- Disk-write pressure on AppLog (sync writes on the same Dispatchers.IO
  thread the tracker uses).
- Implies the previous `getSentStatus` HTTP call is taking >750 ms
  often enough that the `withTimeout(750)` consistently elapses
  before the call returns (probable on slow cellular).
- Doesn't drive new HTTP traffic — `inFlightJob?.isActive == true`
  causes the tick to *skip* launching a new request, so this is a
  noise/CPU/disk problem, not a radio problem.

Fix shape (cheap, separate from the radio mitigation):
- In `deliveryTrackerLoop`, emit `delivery.tracker.skipped` only on
  the *first* skip in a consecutive run, not every tick. Keep a
  `consecutiveSkips` counter; log when the streak ends with
  `streak=$n`.
- Optional: bump the timeout from 750 ms to ~3 s. The 750 ms was
  chosen for a Wi-Fi LAN round-trip; cellular routinely needs more.

## Plans for handling the issue

Updated 2026-05-12 after android_logs_5: pongs are the dominant
*fixable* slice (~25 mAh / 10 h) but not the whole picture. Long-poll
iterations are exonerated. Order below reflects post-dump priority.

### Option A — extend desktop background ping cadence — **picked**

**Where**: `desktop/src/tray/app.py` line 269,
`self._ping_interval = 300.0` — the icon_poll thread's "how stale is
too stale" gate. Menu-open path (`tray/status.py:70`) keeps its own
30 s cache for responsiveness on user touch.

**Idea**: bump the background icon-poll cadence from 5 min to 15 min.
The 5-min number was chosen when liveness was a UX concern; the new
constraint is phone battery on cellular. The menu-open and on-connect
paths still ping immediately, so the icon dot stays accurate when the
user actually looks.

**Trade-off**: when the user *isn't* clicking the tray icon, the dot
may lag up to 15 min behind reality. Worst case: dot shows online,
user opens menu → menu-open re-pings → dot updates. The "send file"
action gates on `is_paired`, not the dot color, so no functional
regression.

**Expected saving**: ~3× fewer background pongs.
- Before: ~115 pongs / 10 h, ~90 of them sleeping-metered.
- After: ~38 pongs / 10 h, ~30 sleeping-metered.
- Radio savings: ~17 mAh / 10 h (out of the ~25 mAh attributable to
  pong tail time).

**Verification**: after deploy, the next dumpsys should show
`ping.pong.sent` count drop by ~3×, `fcm.message.received type=ping`
count likewise. App `mobile_radio` should drop by ~15–20 mAh / 10 h
in a comparable idle window.

### Option B — coalesce pong responses

**Where**: `FcmService.kt:sendPong`.

**Idea**: if the phone has talked to the server in the last 5 s
already (e.g. via a long-poll reconnect or a queued upload), skip
the pong. Server's `last_seen_at` is already fresh; sending another
POST is redundant.

**Trade-off**: requires reading server's last-seen state, which we
don't track locally. Could be approximated by remembering "last
outbound HTTP completed at" in a `@Volatile` on `ApiClient`.

**Expected saving**: cuts pong count by some fraction (the fraction
where any unrelated outbound HTTP happened in the prior 5 s). Likely
modest — most sleep-time pongs are isolated events with no nearby
traffic.

### Option C — server-side debounce on the ping endpoint

**Where**: `server/src/Controllers/DeviceController.php` `ping`
handler.

**Idea**: track per-recipient `last_fcm_ping_at`. If the desktop
issues a ping for the same recipient within (say) 4 min of the
previous FCM dispatch, return the cached `via: fcm_throttled` status
instead of re-firing.

**Trade-off**: desktop has to tolerate the response. Simpler than
Option A because all the policy is server-side and benefits every
client uniformly.

**Expected saving**: similar to Option A — caps the pong cadence at
4–5 min regardless of how aggressively desktops poll.

### Option D — drop the on-demand ping primitive entirely on cellular

**Idea**: when desktop detects the phone's last contact was via the
long-poll within the last N minutes, skip the explicit ping cycle
altogether. The long-poll's TCP keepalive already answers
"reachable?" without any new traffic.

**Trade-off**: the dot accuracy degrades the same way as Option A,
but with zero server changes.

**Expected saving**: largest of the four — eliminates the entire
pong-tail bill on idle days. Closer to ~5 mAh / 10 h.

### Non-option: keep long-poll TCP connection alive across screen-off

Tempting (avoid re-handshake on each screen-on), but the radio cost
isn't in the handshake — it's in the request + tail. Holding a TCP
connection open across sleep is *more* expensive, not less, because
the modem has to send periodic TCP keepalives.

## Acceptance / decision criteria

After Option A deploy, the next dumpsys should show:

1. `ping.pong.sent` count drops by ~3× (from ~115 / 10 h to ~38 / 10 h).
2. `fcm.message.received type=ping` count matches the new pong count.
3. App-attributed `mobile_radio` drops by ~15–20 mAh on a comparable
   idle window (caveat: noisy if user activity / transfer load differ).
4. `poll.loop.iteration screen_off=true` count stays bounded (<10 / 10 h).
   If it spikes, something is bouncing in/out of the screen-off wait
   and we have a separate problem.

If A under-delivers (e.g. only 5–8 mAh saving), consider stacking:
- The optional 15 min → 30 min step in Option A.
- The `delivery.tracker.skipped` rate-limit (separate, cheap; cuts
  CPU + disk noise but not radio).
- A deeper foreground-time audit (TransferViewModel heartbeat
  interval on cellular) — the dump shows 15 s cadence even when the
  metered-fast-path should give us 60 s; that's a separate bug.

### What `android_logs_6.txt` actually showed (2026-05-13 09:52)

Mixed-signal window — partial confirmation of Option A plus a clearer
view of the log-spam side issue.

**Timeline** (important for reading this and any later dump):

- `94bedb2` desktop fix (5 min → 15 min icon-poll cadence): committed
  2026-05-12. **User reinstalled the desktop binary 2026-05-13 ~10:06**
  — *after* the dumpsys window in `_6.txt` ended (06:48 → 09:52).
  Consequence: this dumpsys saw ~3 h of mixed old/new desktop, not
  pure new-desktop steady state.
- `_6.txt` carries the dumpsys block at the top (3 h 4 m, since-charge)
  + the AppLog tail (14 h, MAX_LINES=2000 cap). The two windows don't
  align — AppLog is line-rotated, not time-rotated.

**Dumpsys (3 h 4 m window, 06:48 → 09:52, com.desktopconnector = u0a454):**

| Metric | `_6.txt` | Pro-rated 10 h | Baseline `_4.txt` (10 h 49 m) |
|---|---:|---:|---:|
| App total drain | 32.9 mAh | ~107 | 146 |
| App `mobile_radio` | 26.8 mAh | ~87 | 137 |
| App CPU | 4.20 mAh | ~14 | 9.08 |
| Mobile radio active | 29 m 23 s (16%) | — | 2 h 24 m (22%) |
| Packets rx/tx | 49 047 / 49 378 | — | 122 778 / 82 290 |
| Data rx/tx | 22.05 MB / 6.55 MB | — | 74.4 MB / 22.5 MB |
| Radio activations | 32 | — | 204 |
| Foreground service | 3 h 2 m (99% of window) | — | — |

Headline: app `mobile_radio` pro-rates **~127 → ~87 mAh / 10 h** =
**–31%** vs `_4.txt`. Caveat — the desktop was on the new binary for
only the last ~0% of this dumpsys window (user reinstalled *after* the
dumpsys was exported), so the saving is almost certainly an artefact
of fewer transfers / different user activity, not the cadence change.

**AppLog (14 h window, 19:47 → 09:52, MAX_LINES=2000 cap):**

| Event | Count | % of buffer |
|---|---:|---:|
| `delivery.tracker.skipped reason=previous_in_flight` | **1038** | **52%** |
| `connection.check.succeeded` | 389 | 19% |
| `fcm.message.received type=ping` | 163 | 8% |
| `ping.pong.sent` (tagged) | 30 | 1.5% |
| transfer events | 13 | 0.7% |

Half the rotating buffer is the `delivery.tracker.skipped` noise the
plan flagged in the prior round as a "Surprise finding." Untagged
`ping.pong.sent` lines (133 of 163 total) are from the *pre*-diag-tag
APK — the cadence-fix-only one. They show only the timestamp, not the
screen/metered context.

**Pong cadence — the one piece that already confirms Option A.**
Looking at the freshest pongs (live `app.log` pulled via ADB
2026-05-13 ~10:22, after both the user's desktop reinstall *and* the
overnight phone-on-charger period that cleared all in-flight state):

```
20:22:56 [yesterday evening — last pong before phone went idle]
10:06:56 [next pong — first one after user's desktop restart]
10:21:57 [+901 s exactly]   ← 15-min cadence ✓
```

Inside the old/new desktop overlap window earlier in the same AppLog,
intervals were 5–14 min — exactly what you'd see if the old-binary
desktop (5-min) and the new-binary desktop (15-min) were briefly
active back-to-back across the user's reinstall plus on-demand pings
from menu opens.

**Conclusion on Option A**: the +901 s pair is confirmation enough
that the cadence fix takes effect when the new desktop binary is the
only ping source. A clean steady-state measurement is still pending
in `_7.txt` or later.

## Changes deployed since `_6.txt` analysis (commit `89cd8ad`)

The log-spam side-issue from `_5.txt` got stacked into this round
because half the AppLog buffer was unusable. Three changes:

1. **`AppLog.MAX_LINES` 2000 → 4000.** Doubles the rotating buffer
   for longer post-hoc investigations.
2. **`deliveryTrackerLoop` streak-coalescing.** First skip in a run
   logs `delivery.tracker.skipped reason=previous_in_flight` as
   before; subsequent ticks are silent; on streak end (next non-skip
   tick) emits `delivery.tracker.skipped reason=previous_in_flight
   streak=N` if N>1. Streak is also flushed on early-exit paths
   (screen-off, no trackedIds) so a streak doesn't span unrelated
   periods.
3. **`withTimeout(750)` → `withTimeout(3000)`** in the tracker poll.
   The 750 ms budget was Wi-Fi-LAN-sized; cellular routinely exceeded
   it, which is *what created* the in-flight skip streaks. Bumping
   the budget cuts the skip count at the source, not just at the log
   sink. Log tag renamed `poll_timeout_3000ms`.

### Deploy timeline (for reading later dumpsys against)

| When | What | Where |
|---|---|---|
| 2026-05-12 | `94bedb2` desktop 5 min → 15 min icon-poll cadence | desktop tree |
| 2026-05-12 | `43fe07b` ping/poll diagnostic tags landed in APK | android |
| 2026-05-13 ~10:06 | **User reinstalled desktop from source** — first time the cadence fix was actually running for this user | local |
| 2026-05-13 ~10:30 | `89cd8ad` log-spam coalesce + 3 s timeout + 4000-line buffer | android |
| 2026-05-13 ~10:35 | `89cd8ad` APK installed on the phone | phone |
| 2026-05-13 ~14:06 | `android_logs_7.txt` exported | phone |
| 2026-05-13 ~14:30 | `bf83c67` cancellable getSentStatus + transfer byte-attribution logs | android (this round) |
| 2026-05-13 ~14:35 | `bf83c67` APK installed on the phone | phone |

### What `_7.txt` actually showed (2026-05-13 14:06)

3 h 21 m dumpsys + 3 h 16 m AppLog. App was the *new* `89cd8ad`-built
APK, desktop was the *new* binary the user reinstalled at ~10:06. So
this is a true post-fix window, **but** it had heavy transfer activity
(see §unattributed-bytes below) so it's not a comparable-idle baseline.

**Working:**

- **Log-spam coalesce** — clear win. 1038 raw `delivery.tracker.skipped`
  lines → 38 first-of-streak + 21 `streak=N` summaries. From 52 % of a
  2000-line buffer down to ~3 % of a 4000-line buffer.
- **Pong cadence** — confirmed at 15 min. 17 tagged pongs in 3 h 16 m;
  intervals dominated by 900–902 s (8 of 16 intervals), with the rest
  being on-demand pings (62 s, 241 s, 305 s, 361 s, 481 s) piggybacking
  the background loop on menu opens or connection-state blips.
- **4000-line buffer** — confirmed; AppLog spans 3 h 16 m with much
  lower spam, so the rate of *useful* lines per slot is actually higher
  than before.

**NOT working as intended:**

- **`withTimeout(3000)` did not cancel in-flight OkHttp calls.**
  Zero `poll_timeout_3000ms` events. Yet streak=38 in the coalesced
  output means one HTTP call held the in-flight slot for 19 s. The
  root cause: Kotlin's `withTimeout` sets a cancellation flag on the
  coroutine, but `OkHttp.execute()` runs on a `Dispatchers.IO` thread
  and isn't coroutine-aware. The coroutine looks active for the full
  socket-busy duration. 89cd8ad's bump from 750 → 3000 had no effect
  on actual call durations. Fixed in `bf83c67` — see next section.

**Mystery: 24 MB rx / 6.5 MB sent attributed to `u0a454` over 3 h 21 m
with zero `transfer.*` lines in AppLog.** Wi-Fi was 100 % asleep, so
all of it was cellular. No way to attribute. Plausible suspects: long-
poll bodies, fasttrack messages, downloaded chunks that never logged
a `started`-side event. Resolved by `bf83c67`'s byte-attribution logs.

**Radio drain regression — but it's a workload artefact, not a fix
regression.** App `mobile_radio` 41.8 mAh / 3 h 21 m ≈ 125 mAh / 10 h —
about the same as the 137 mAh `_4.txt` baseline. Window had 51 k packets
rx + tx and 24 MB throughput, plus the device's total discharge was
492 mAh in 3 h 21 m (≈146 mAh/h vs `_6`'s 92 mAh/h). The phone was
busy beyond just our app. Acceptance criterion #2 needs a comparable-
idle window — this isn't one.

## Changes deployed in `bf83c67` (2026-05-13)

Two threads, both motivated by what `_7.txt` showed:

1. **`ApiClient.getSentStatus` made coroutine-cancellable.** Now `suspend
   fun`, routed through `OkHttp.enqueue` + `suspendCancellableCoroutine`.
   `cont.invokeOnCancellation { call.cancel() }` closes the socket when
   the surrounding `withTimeout(3000)` fires, unwinding the call
   immediately. The 89cd8ad timeout bump finally becomes effective.

2. **Byte-attribution logging.** Six new fields / events:

   | Event | New field / addition | Site |
   |---|---|---|
   | `transfer.download.started` | new event — `transfer_id`, `chunks`, `mode` | `PollService.receiveFileTransfer` + `receiveStreamingTransfer` (fresh-start branch) |
   | `transfer.download.resumed` | added `chunks=N` | same functions |
   | `poll.notify.cycle` | new event — `duration_ms`, `has_pending` | `PollService.pollLoop` after each `longPollNotify` |
   | `fasttrack.message.pending_listed` | added `encrypted_b64_bytes=B` | `PollService.handleFasttrackMessages` |
   | `transfer.init.accepted` | added `bytes=$sourceSize` | `UploadWorker.doWork` |
   | `transfer.upload.completed` | added `bytes=$sourceSize` (both classic + streaming variants) | `UploadWorker.runClassicUpload` / `runStreamingUpload` |

   After this lands, summing all `bytes=` fields across one log window
   plus the (count × ~1 KB) long-poll bodies and the fasttrack
   `encrypted_b64_bytes` should account for ~all per-app `mobile_radio`
   rx/tx in the matching dumpsys window. Anything unaccounted for is
   a logging gap to close.

### What `_8.txt` should show

Read with the deploy timeline. Cleanest window is a dumpsys taken
≥4 h after `bf83c67` is running on both ends, with cellular only, log
cleared at install time. The user cleared the phone log right after
the `bf83c67` install — so `_8.txt` starts from a clean slate.

1. **Pong cadence ~15 min** — same as `_7`'s confirmation, now over
   a longer window. Most `ping.pong.sent screen_off=*` gaps ≥850 s,
   short outliers attributable to menu opens / reconnects.
2. **Skip streaks short.** The cancellable `getSentStatus` should cap
   any single in-flight at 3 s = 6 ticks. `delivery.tracker.skipped`
   `streak=N` summaries should have N ≤ 6 in almost all cases. If
   N still climbs into the teens or 30s, either a different call is
   blocking the tracker, or `Call.cancel()` isn't unwinding our
   OkHttp version cleanly.
3. **`poll_timeout_3000ms` events present.** Counter-intuitive but
   important — non-zero count means the cancellation is actually
   firing. Pre-`bf83c67` this was always zero because the inner call
   was running uncancelled past 3 s and completing on its own. Post-
   `bf83c67` we expect some count > 0 whenever cellular RTT spikes.
4. **Per-app `mobile_radio` rx/tx fully attributable.** Sum `bytes=`
   across `transfer.download.started`/`completed` +
   `transfer.upload.completed` + `fasttrack.message.pending_listed
   encrypted_b64_bytes=` + (`poll.notify.cycle` count × ~1 KB). The
   sum should be within ~20 % of dumpsys's `Mobile network: …MB
   received, …MB sent` for `u0a454`. If a big gap remains, identify
   the offender and add another `bytes=` site for it.
5. **App `mobile_radio` ≤ 70 mAh / 10 h equivalent — finally.**
   This is acceptance criterion #2 from the prior round, deferred
   because `_7.txt` wasn't an idle window. Ideally `_8.txt` is.
6. **AppLog window ≥ 24 h.** Same as before — buffer should hold
   roughly a day under normal activity.

If items 1–4 land, the diagnostic surface is now adequate. Items 5–6
plus a clean idle baseline close the Option A acceptance loop. If
mobile_radio is still > 100 mAh / 10 h with everything attributed,
Option C (server-side ping debounce) or Option D (skip ping if long-
poll touched the server recently) come back in scope.

## Cross-references

- Earlier round of this same investigation: `android_logs_3` analysis
  (2026-05-11), which led to the `NetworkPolicy` metered-hold gating
  in `PollService.pollLoop` lines 477–510. That fixed the *active-app*
  metered cost (95 mAh of 99 mAh app drain in that window); this round
  is the *background-sleep* residue that the metered hold doesn't
  cover.
- Architecture rationale for on-demand pings (not periodic
  heartbeats): `CLAUDE.md` § *Liveness (ping/pong)*.
- Diagnostic event catalog: `docs/diagnostics.events.md` § `ping`,
  § `poll`.
