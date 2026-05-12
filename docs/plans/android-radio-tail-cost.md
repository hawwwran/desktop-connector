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
