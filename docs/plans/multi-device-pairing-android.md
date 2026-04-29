# Multi-device pairing — Android (phase A)

Phone-side support for being paired with **N desktops simultaneously**. Today
the phone keeps an N-capable storage layout but every send/receive site
funnels through `getFirstPairedDevice()`. This plan replaces the singleton
assumption with an explicit *selected peer* model + per-peer history +
per-peer auth-failure recovery, and adds the device-selector / rename /
"pair another" UI.

Scope: **Android only**. The server already supports N pairings per device
(`pairings` table is `(device_a_id, device_b_id)` with a UNIQUE pair, no
cardinality constraint per device — see `server/migrations/001_initial.sql:21`).
Desktop side is unaffected because each desktop only sees the pair it
participates in. The one cross-cutting change is how the phone reacts to
desktop-initiated unpair (`.fn.unpair`) — that lands in this plan as a
behavior tweak, not a desktop code change.

---

## Goals

1. **Pair with another desktop** without dropping existing pairs.
2. **Each pair has a user-visible name** (default "Desktop", auto-iterate to
   "Desktop 2" if taken; legacy nameless rows migrate to "Desktop").
3. **Per-pair history** — the recent-files list filters to the currently
   selected peer.
4. **Device selector** replaces the "Desktop Connector" title when N > 1.
   Tapping the header opens a chooser; the chosen name is shown in place of
   the title.
5. **Settings → Pairings list** with rename (pencil) + unpair (trash) per row,
   plus a "Pair with another desktop" button.
6. **Sender-targeted unpair**: a `.fn.unpair` from desktop A removes only
   the (phone, desktop A) pair. The pairing screen auto-shows only when
   **zero** pairs remain.
7. **Send-to-selected**: every send action (clipboard, share, file picker,
   send-logs, find-my-phone) targets the currently selected pair.
8. **Background-only auto-switch**: when a transfer or fasttrack message
   arrives while the app is **not in foreground**, switch the selected pair
   to the sender. When the app is in foreground, never auto-switch.
9. **Per-pair auth-failure recovery**: a 403 PAIRING_MISSING attributed to
   pair X removes only X (banner names the lost peer); a 401
   CREDENTIALS_INVALID stays app-global (the device's creds are gone).

---

## Key architectural decisions

### D1. Single source of truth: `PairingRepository`

Today, sites scattered across `TransferViewModel`, `Navigation`,
`ShareReceiverActivity`, `PollService`, and `FindPhoneManager` call
`KeyManager.getFirstPairedDevice()` directly. We add a thin
`PairingRepository` (no Room, just a `KeyManager` wrapper + a `StateFlow`)
that owns:

- `pairs: StateFlow<List<PairedDeviceInfo>>` — recomputed from KeyManager
  on every change (saveCurrentPair / removePairedDevice / rename).
- `selectedDeviceId: StateFlow<String?>` — backed by `AppPreferences`,
  defaults to the most-recently-paired entry on first read.
- `selected: StateFlow<PairedDeviceInfo?>` — derived from the two above.
- `selectPair(deviceId)` — used by the selector UI and the auto-switch
  logic.
- `rename(deviceId, newName)` / `unpair(deviceId)` — mutating ops that
  also notify the desktop (unpair sends `.fn.unpair` first, same as today).

Every call site that needs "the peer to send to right now" reads
`pairingRepo.selected.value` instead of `keyManager.getFirstPairedDevice()`.
Every call site that needs *a specific* peer (e.g., responding to find-phone
from sender X, or DAO writes that already store `peerDeviceId` on the row)
keeps using a passed-in id.

**Why a repo, not a flag in `TransferViewModel`**: `PollService` (background)
and `ShareReceiverActivity` (one-shot) need to read selected-pair without
spinning up the view-model. The repo is process-singleton, view-model-free.

### D2. Schema — rename `recipientDeviceId` → `peerDeviceId` (v8 → v9)

`QueuedTransfer.recipientDeviceId` only makes sense for `OUTGOING` rows.
Incoming rows currently throw the sender id away (the value is on the
poll-response payload at `service/PollService.kt:521` but never persisted).
We need it persisted to filter history per peer.

The cleanest move is a single column `peerDeviceId` that means *the other
party*: for OUTGOING rows it's the recipient, for INCOMING rows it's the
sender.

Migration `MIGRATION_8_9` (table-rebuild, since SQLite can't rename
columns until 3.25, and Room's expected schema must match the entity exactly):

```sql
CREATE TABLE queued_transfers_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    contentUri TEXT NOT NULL,
    displayName TEXT NOT NULL,
    displayLabel TEXT NOT NULL DEFAULT '',
    mimeType TEXT NOT NULL,
    sizeBytes INTEGER NOT NULL,
    peerDeviceId TEXT NOT NULL,        -- was recipientDeviceId
    direction TEXT NOT NULL DEFAULT 'OUTGOING',
    status TEXT NOT NULL DEFAULT 'QUEUED',
    chunksUploaded INTEGER NOT NULL DEFAULT 0,
    totalChunks INTEGER NOT NULL DEFAULT 0,
    deliveryChunks INTEGER NOT NULL DEFAULT 0,
    deliveryTotal INTEGER NOT NULL DEFAULT 0,
    errorMessage TEXT,
    transferId TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    createdAt INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'classic',
    negotiatedMode TEXT,
    abortReason TEXT,
    failureReason TEXT,
    waitingStartedAt INTEGER
);
INSERT INTO queued_transfers_new
    SELECT id, contentUri, displayName, displayLabel, mimeType, sizeBytes,
           recipientDeviceId, direction, status, chunksUploaded, totalChunks,
           deliveryChunks, deliveryTotal, errorMessage, transferId, delivered,
           createdAt, mode, negotiatedMode, abortReason, failureReason,
           waitingStartedAt
    FROM queued_transfers;
DROP TABLE queued_transfers;
ALTER TABLE queued_transfers_new RENAME TO queued_transfers;
```

Existing INCOMING rows post-migration have `peerDeviceId` =
`recipientDeviceId` from before, which was the *phone's own* device id (not
useful) — but those rows are read-only history, so we accept the slight
incorrectness for already-completed downloads. **Filter caveat**: post-
migration legacy INCOMING rows won't appear under any pair filter; they'll
appear under the special "All" view if we add one (open question Q5).
A simpler stance: backfill `peerDeviceId` to the pre-migration first paired
device id — that matches what the user saw at the time. We do that with one
extra `UPDATE` after the rebuild:

```sql
-- Best-effort: assign legacy incoming rows to the first known pair.
-- Reads from EncryptedSharedPreferences happen outside SQL — see
-- MultiPairMigrationRunner below; the SQL version of this is "set
-- peerDeviceId = '<resolved id>' WHERE direction='INCOMING' AND <heuristic>".
```

Implementation note: the `UPDATE` runs in `MultiPairMigrationRunner`
(application-level), not inside the Room migration block, because Room
migrations don't have access to EncryptedSharedPreferences.

### D3. Naming logic at pairing-confirm

`PairingViewModel.confirmPairing()` currently saves with the QR-supplied
name immediately. We add an extra stage **after** the user accepts the
verification code:

- New `PairingStage.NAMING` between `CONFIRMING` and `COMPLETE`.
- UI: a single `OutlinedTextField` prefilled with the suggested name
  plus "Save" / "Cancel" buttons.
- **Suggestion is the QR-supplied desktop name** (i.e. the desktop's
  hostname). If the QR didn't carry one, fall back to
  `nextDefaultName(existingNames)` — start at "Desktop"; if taken
  (case-insensitive), try "Desktop 2", "Desktop 3", … until free.
  `nextDefaultName` is pure Kotlin, unit-tested.
- The user can edit the suggestion freely before saving. Allow
  duplicates if the user explicitly types one — we don't enforce
  uniqueness on save, only the fallback iterates. Keeps the validation
  path simple and avoids a "name in use" footgun.

### D4. Foreground / background detection — `ForegroundTracker`

We don't have process-wide foreground tracking today. Add a tiny
singleton wired to `ProcessLifecycleOwner`:

```kotlin
object ForegroundTracker {
    @Volatile var isForeground: Boolean = false
        private set

    fun install() {
        ProcessLifecycleOwner.get().lifecycle.addObserver(
            LifecycleEventObserver { _, event ->
                when (event) {
                    Lifecycle.Event.ON_START -> isForeground = true
                    Lifecycle.Event.ON_STOP  -> isForeground = false
                    else -> Unit
                }
            }
        )
    }
}
```

Installed once in `DesktopConnectorApp.onCreate()`. Reads are lock-free
(single-writer, single-reader idiom; `@Volatile` for visibility).

`PollService` and `FcmService` consult `ForegroundTracker.isForeground`
when deciding whether to auto-switch the selected pair on incoming
activity (D9 below).

### D5. Per-peer auth-failure attribution

Today `ConnectionManager` latches a single global `_authFailureKind`
(file: `network/ConnectionManager.kt:54`). We refactor to:

- `_authFailureByPeer: StateFlow<Map<String, AuthFailureKind>>` — keyed by
  peer id when attributable; `""` (empty key) for app-global failures
  (CREDENTIALS_INVALID has no peer attribution because the phone's own
  creds are bad regardless of who we're talking to).
- `ApiClient.observeAuth` callback gains a nullable `peerId: String?`
  parameter so call sites can attribute. Endpoints that target a peer:
    - `initTransfer(transferId, recipientId, ...)` — peer = recipientId.
    - `uploadChunk` / `getChunk` — peer derivable from the cached
      transfer→peer mapping in the worker.
    - `sendFasttrack(recipientId, ...)` — peer = recipientId.
    - `getStats(pairedWith)` — peer = pairedWith if non-null.
  Endpoints that don't (`/health`, `/devices/register`, `/devices/ping`
  with `target=`) keep `peerId = null`, treated as global.
- The 3-in-a-row latch becomes per-key (each key has its own streak
  counter). A 401 still fires globally (key `""`); a 403 against
  recipient `X` fires for key `X`.
- `repairFromAuthFailure(peerId)` removes only that peer from KeyManager
  and clears that peer's streak. The CREDENTIALS_INVALID branch (key
  `""`) wipes everything as today.
- Banner becomes a **list** of broken-pair banners (one per failing key),
  each with its own "Re-pair <Name>" action.

### D6. Connection-state semantics with N peers

Keep `ConnectionState` enum (`CONNECTED / RECONNECTING / DISCONNECTED`)
**global** — it reflects server reachability + the *selected peer's*
auth-failure state. The header dot keeps painting based on global
+ selected-peer-attributed state.

The selector dropdown shows per-peer "desktop online" indicators driven by
existing `getStats(pairedWith=…)`. Add a periodic batched refresh (a single
poll loop hitting `/api/devices/stats` per pair, every 30 s while the
selector sheet is open) so users see live per-pair status without spamming
the server.

Phase 1 deliberately does NOT add a per-pair `last_seen` cache to disk —
the dropdown can show "Online" only while it's open, "—" otherwise. Good
enough for v1; revisit if users want at-a-glance status.

---

## Schema & state changes

### Room (Android)

| Change | Where |
|---|---|
| `MIGRATION_8_9` table-rebuild rename `recipientDeviceId` → `peerDeviceId` | `data/Migrations.kt` |
| `QueuedTransfer.recipientDeviceId` → `peerDeviceId` | `data/QueuedTransfer.kt` |
| `AppDatabase` version 8 → 9 | `data/AppDatabase.kt` |
| New DAO: `getRecentForPeer(peerId)` (LIMIT 100, ORDER BY createdAt DESC) | `data/QueuedTransfer.kt` |
| New DAO: `clearAllForPeer(peerId)` (preserves in-flight, like `clearAll`) | `data/QueuedTransfer.kt` |
| `getRecent()` / `clearAll()` retained for "All" view if we add one | unchanged |
| Maintenance queries (`getActiveDeliveryIds`, `getUndeliveredTransferIds`, `getStaleWaitingStream`, `getPending`) stay peer-agnostic | unchanged |

### EncryptedSharedPreferences (KeyManager)

No structural change — we already store one JSON blob per peer keyed
`paired_<deviceId>` with `{pubkey, symmetric_key, name, paired_at}`. Add:

- `setPairedDeviceName(deviceId, name)` — read-modify-write the JSON blob,
  bumps a `renamed_at` field for observability (optional).

### AppPreferences (plain SharedPreferences)

Add:

- `selectedDeviceId: String?` — last-selected peer; null = no preference,
  use most-recently-paired.
- `multiPairMigrationDone: Boolean` — gate for `MultiPairMigrationRunner`
  (one-shot rename of legacy unnamed pairs to "Desktop").

---

## Phases

Following the codebase's `D.1`-style phasing for trackability. Each phase is
a self-contained, mergeable unit.

### A.1 — `PairingRepository` + selected-pair preference

**Goal**: introduce the abstraction without changing any UI yet. All
existing call sites still work because `pairingRepo.selected` returns the
same first-paired-device they read today.

- Add `crypto/PairingRepository.kt` (process-singleton via `Application`).
- Add `AppPreferences.selectedDeviceId`.
- `PairingRepository.selected` is the first non-null of:
    1. KeyManager entry for `selectedDeviceId` (if present and valid).
    2. Most-recently-paired entry by `paired_at` (replaces `getFirstPairedDevice`'s
       arbitrary iteration order — now deterministic).
- `PairingRepository.pairs` is the sorted list of all entries (most recently
  paired first).
- Replace every `keyManager.getFirstPairedDevice()` call with
  `pairingRepo.selected.value`. Sites:
    - `TransferViewModel.queueFiles / sendClipboard / sendClipboardText /
      queueClipboardText / queueClipboardFile / resend / sendLogsToDesktop
      / pairedDeviceName getter`
    - `Navigation.kt` (`val paired = keyManager.getFirstPairedDevice()`)
    - `ShareReceiverActivity` — uses `pairingRepo.selected.value != null`
      gate instead of `hasPairedDevice()`
- Keep KeyManager's `getFirstPairedDevice` for now (delete in A.7).

**Acceptance**: existing single-pair behaviour unchanged. With one pair,
`selected` resolves to that pair. With zero pairs, `selected = null` and
gates fall through as today.

### A.2 — Schema rename `recipientDeviceId` → `peerDeviceId`

**Goal**: persist the sender id on incoming rows so per-peer history works.

- Bump `AppDatabase` to version 9.
- Add `MIGRATION_8_9` (table-rebuild SQL above).
- Rename Entity field; update every reference (compile-driven).
- `PollService.receiveTransfer` (sender-id branch around
  `service/PollService.kt:521`): write `senderId` into `peerDeviceId` on
  the inserted INCOMING row. (Today these rows are inserted only on
  streaming receives — classic incoming downloads insert via a different
  path; verify both paths.)
- `MultiPairMigrationRunner` (called once at app startup, gated on
  `prefs.multiPairMigrationDone`):
    - For every paired device blob in EncryptedSharedPreferences with
      empty/missing `name`, set `name = "Desktop"` (collisions resolved
      by the `nextDefaultName` rule in D3).
    - For every legacy INCOMING row with `peerDeviceId` = phone's own
      device id (the buggy pre-migration value), reassign to the
      most-recently-paired peer id at migration time. Best-effort —
      these are read-only history rows.
    - Set `multiPairMigrationDone = true`.

**Acceptance**: 
- Fresh installs are unaffected.
- Existing installs with one pair have legacy incoming history attributed
  to that pair; nothing visually disappears.
- New incoming rows persist `peerDeviceId = senderId`.

### A.3 — Naming step in pairing flow

- Add `PairingStage.NAMING`.
- Update `PairingScreen` to render the rename UI on `NAMING`.
- `PairingViewModel.confirmPairing()` flow: on confirm, transition to
  `NAMING` (don't call `savePairedDevice` yet). Store the (deviceId,
  pubkey, symmetricKey, suggestedName) as in-progress state.
- Add `PairingViewModel.commitName(name)` — calls
  `keyManager.savePairedDevice(... name = name)` and transitions to
  `COMPLETE`.
- `nextDefaultName(existing: List<String>)` helper in
  `crypto/PairingRepository.kt` (pure function, unit-tested).
- "Cancel" on the naming screen returns to `CONFIRMING` (no pair saved).
- Set `selectedDeviceId = newDeviceId` on commit so the user immediately
  lands on the new pair after pairing.

**Acceptance**: a fresh pair with no existing pairs shows "Desktop"; a
second pair shows "Desktop 2"; naming a pair "Phone" works fine.

### A.4 — Settings → Pairings list

- Replace the singleton `pairedDeviceId` / `pairedDeviceName` parameters
  on `SettingsScreen` with a `pairs: List<PairedDeviceInfo>` callback +
  rename / unpair actions.
- New section `PairingsCard` listing each pair as a row with:
    - Name (left).
    - Truncated id "Desktop ID: 1234abcd".
    - Pencil icon → opens rename dialog (TextField prefilled with current
      name; "Save" calls `pairingRepo.rename(deviceId, newName)`).
    - Trash/Unpair icon → confirm dialog → calls `pairingRepo.unpair(deviceId)`.
- Below the list: a `Button("Pair with another desktop")` that navigates
  to the pairing screen (additive — doesn't unpair existing).
- `pairingRepo.unpair(deviceId)`:
    1. Sends `.fn.unpair` to the desktop (existing
       `transferViewModel.sendUnpairNotification`, which already takes a
       peer id).
    2. **Clears that peer's history rows** (`db.transferDao()
       .deleteAllForPeer(deviceId)`). This is intentional: history is
       per-pair, so leaving rows attached to a pair that no longer
       exists would either orphan them (invisible in any view) or
       require an "All" view to surface them. Either is worse than
       deleting. Active in-flight rows (status in
       `QUEUED/PREPARING/WAITING/UPLOADING/SENDING/WAITING_STREAM/
       DELIVERING`) for the removed peer are also deleted — the sender
       is the desktop we're unpairing from, so no incoming work
       remains worth keeping; outgoing rows already had their server
       row torn down by the `.fn.unpair` round-trip plus the
       subsequent abort path.
    3. Calls `keyManager.removePairedDevice(deviceId)`.
    4. Clears any auth-failure entry keyed to `deviceId` from
       `_authFailureByPeer` (A.8) so the banner doesn't linger.
    5. If removed pair was the selected one: pick a new default
       (most-recently-paired remaining) and update `selectedDeviceId`.
    6. If now zero pairs remain: navigate to pairing (the existing
       `isPaired` flow handles this).

The same cleanup runs for **desktop-initiated unpair** in A.7 — the
PAIRING_UNPAIR handler in `PollService` calls `pairingRepo.unpair(senderId)`
(or an internal `unpairLocal` variant that skips the `.fn.unpair` send-back,
since the desktop initiated this round-trip). This guarantees history is
cleared identically whether the user unpaired from settings or the desktop
sent the notification.

New DAO method:

```kotlin
@Query("DELETE FROM queued_transfers WHERE peerDeviceId = :peerId")
suspend fun deleteAllForPeer(peerId: String)
```

(Aggressive — drops in-flight rows for that peer too. Justified: an
unpaired desktop will reject every chunk-upload with 403 anyway, so the
in-flight row is dead on arrival.)

**Acceptance**: rename persists across app restart; unpair from settings
removes only that pair; zero pairs auto-shows pairing screen.

### A.5 — Device-selector header (HomeScreen)

- HomeScreen consumes `pairs: List<PairedDeviceInfo>` and `selected:
  PairedDeviceInfo?` (passed in from Navigation, sourced from
  `pairingRepo`).
- Title rendering branches:
    - 0 pairs: not reachable (Navigation routes to pairing).
    - 1 pair: keep "Desktop Connector" text (current behaviour).
    - N > 1 pairs: render `selected.name` + a `KeyboardArrowDown` chevron
      next to the status icon. Clickable area opens a `ModalBottomSheet`.
- `DeviceSelectorSheet` content:
    - List of pairs, each with name + per-peer online dot (driven by a
      `LaunchedEffect` poll of `getStats(pairedWith=…)` while the sheet is
      open). Tap to call `pairingRepo.selectPair(id)` and dismiss.
    - Bottom button "Pair with another desktop" → navigates to pairing.
- Status icon + tap-to-refresh behaviour at `ui/HomeScreen.kt:132-150`
  preserved verbatim.

**Acceptance**: with 2+ pairs, header shows selected name; tapping opens
sheet; selection persists across app restart.

### A.6 — Per-pair history filter

- HomeScreen's `transfers` list comes from `transferViewModel.transfers`.
  Refactor `refreshTransfers` to use `getRecentForPeer(selectedDeviceId)`
  when a peer is selected, falling back to `getRecent()` only if `selected
  == null` (transient state during unpair).
- `clearHistory()` button calls `clearAllForPeer(selectedDeviceId)`. Phrasing
  in the confirmation dialog updates to "Clear history for <name>?".
- The 30-min WAITING / WAITING_STREAM zombie scrub stays global (it operates
  on the in-memory snapshot already loaded — switch the input call to
  `getRecentForPeer` so the snapshot is filtered, but the per-row zombie
  detection is unchanged).
- The `getPending()` requeue path on app start (UploadWorker) stays
  peer-agnostic — we want to resume any pending upload regardless of
  which pair is selected.

**Acceptance**: switching pairs in the selector swaps the history list
without restart; in-flight transfers in the *unselected* pair keep
running and reappear if user switches back.

### A.7 — Sender-targeted unpair from desktop

- `messaging/MessageDispatcher` PAIRING_UNPAIR handler at
  `service/PollService.kt:85-92`: change from
  `keyManager.getFirstPairedDevice() → removePairedDevice` to
  `keyManager.removePairedDevice(message.senderDeviceId)` directly. The
  sender id is already on `DeviceMessage`.
- Toast wording: "Disconnected from <name>" (look up name before removing).
- After removal: if remaining pair count > 0, stay on home (selector
  picks a fallback if the removed pair was selected). If 0 left, navigate
  to pairing as today.
- Clean up `KeyManager.getFirstPairedDevice` — remove call sites; keep
  the function in place for one release with a `@Deprecated` annotation
  so a stray reference fails compile (then drop in the next release).
  Actually: just remove it. There are no external API consumers; compile
  errors give us the migration sweep we want.

**Acceptance**: with 2 pairs, desktop A unpairing leaves desktop B intact.
The selected pair stays (or fails over to B if A was selected).

### A.8 — Per-peer auth-failure attribution

- Add `peerId: String?` parameter to `ApiClient.observeAuth` /
  `AuthObservation` flow.
- Update every endpoint method that has a target peer to pass it (see D5
  list).
- `ConnectionManager` rework:
    - Drop scalar `_authFailureKind`, add `_authFailureByPeer:
      MutableStateFlow<Map<String, AuthFailureKind>>` (key `""` = global).
    - 3-in-a-row latch becomes per-key.
    - `clearAuthFailure(peerId: String)` clears one key.
- `HomeScreen.AuthFailureBanner` becomes `AuthFailureBanners` (plural),
  rendering one banner per failed key with the peer name interpolated:
  "Pairing with <name> lost — re-pair to continue.". The CREDENTIALS_INVALID
  global banner reads "Account credentials invalid — re-register and re-pair
  all desktops".
- `repairFromAuthFailure(peerId: String)`:
    - Per-peer (PAIRING_MISSING): remove that peer; navigate to pairing
      (new pair flow); leave other pairs untouched.
    - Global (CREDENTIALS_INVALID, peerId == ""): wipe all paired devices,
      reset keypair, clear creds, FCM reset (today's behaviour).

**Acceptance**: 403 on a single pair's chunk upload removes only that
pair after 3 strikes; 401 still wipes everything; banner identifies the
specific lost pair.

### A.8.5 — Notification-name suffix when N > 1

Tiny but user-visible.

- Update the transfer-completion notification builder (search for
  `NotificationCompat.Builder` / `setContentText` in
  `service/PollService.kt`) to take an optional `senderName: String?`
  and append `" from <name>"` when `pairingRepo.pairs.value.size > 1`.
- N = 1: notifications keep today's wording exactly. No string-resource
  churn for single-pair users.
- The toast in `cancelAndDelete`, `clearHistory`, etc. is unchanged —
  those are user-initiated and the screen context already shows the
  pair.

**Acceptance**: with two pairs, sending a clipboard from Desktop A
shows "Clipboard received from Desktop A". With one pair, the
notification reads "Clipboard received" as today.

### A.9 — Background-only auto-switch on incoming activity

- `ForegroundTracker.install()` from D4 in `DesktopConnectorApp.onCreate()`.
- In `PollService` after `keyManager.getPairedDevice(senderId)` succeeds
  (lines around 537 / 465 for transfers + fasttrack respectively):
    - If `!ForegroundTracker.isForeground`:
      `pairingRepo.selectPair(senderId)`.
    - If foreground: no-op (do not switch).
- The selector UI updates reactively because `pairingRepo.selected` is a
  `StateFlow`. When the user opens the (background) app the next time, the
  HomeScreen's `selected.collectAsState()` will already point at the
  most-recent sender.

**Acceptance** (manual test): with two pairs both sending while phone is
locked, the most-recently-arrived pair becomes selected on next app open;
with the app already open, switching only happens on explicit user tap.

### A.9b — Find-my-phone: drop concurrent second start

One-line guard in `FindPhoneManager.handleDeviceMessage` for
`FIND_PHONE_START`: if `isRinging` is already true and `senderDeviceId !=
activeDesktopId`, log a `findphone.start.dropped_concurrent` event and
return without changing state. The first searcher's alarm/UI continues
unchanged. The original searcher's stop still works as today
(`activeDesktopId` matches).

**Acceptance**: while desktop A is searching, desktop B's start is
silently dropped (visible in logs, not in UI). Desktop A's stop ends
the search normally. No crashes, no orphaned alarm.

---

## Out of scope (this plan)

- Multi-pair on the **desktop** side (one desktop paired with multiple
  phones). Server already supports it; desktop UI/data layer would need
  parallel work in a later plan.
- Per-pair storage subfolders (`DesktopConnector/Desktop A/…`) — see Q4.
- A unified "All pairs" history view — see Q5.
- Per-pair settings (e.g. "Allow silent search" toggled per desktop) —
  current toggles stay phone-global.
- Pair reordering in the selector — sorted by `paired_at DESC` (most
  recently paired first). Drag-to-reorder is a future polish.
- Pair-level connection metrics (bytes/peer, transfer count) beyond what
  `getStats(pairedWith)` already returns.

---

## Resolved questions

- **Q1 — Notifications**: include sender name `"Received from <Desktop A>"`
  **only when N > 1** paired devices. With N = 1 the notification keeps
  today's wording (no name suffix). N = 0 is unreachable here (nothing to
  receive).
- **Q2 — Re-pair after auth failure**: pairing flow treats it as a
  brand-new pair (fresh `paired_at`, name re-suggested via the QR-
  supplied hostname, falling back to `nextDefaultName` if absent).
  Simpler implementation, acceptable UX.
- **Q3 — ShareReceiverActivity**: silent send to selected pair. No chooser.
- **Q4 — Storage layout**: flat `DesktopConnector/`, collision suffix as
  today.
- **Q5 — "All pairs" history view**: not added.
- **Q6 — Stats poll cadence**: per-pair, 30 s, only while selector sheet
  is open.
- **Q7 — Find-my-phone with multiple concurrent searchers**: keep
  single-searcher semantics. `FindPhoneManager.activeDesktopId` is a
  scalar today; phase 1 keeps it scalar. If a second desktop fires a
  start while the phone is already ringing, the easiest handling is one
  of:
    1. **Drop the second** (silently ignored — first-come-first-served).
    2. **Overwrite `activeDesktopId`** so subsequent location updates flow
       to the new searcher; the alarm/UI already running just continues.
  Either is fine. Plan picks (1) — drop the second start while ringing —
  because it's a one-line guard and avoids re-targeting an alarm
  mid-flight. No phone-side "who is searching" indicator.

---

## Risk register

- **R1 — Migration 8 → 9 risk**: a table rebuild on a phone with
  thousands of history rows could be slow on first launch. Empirically
  history is capped at 100 rows by `trimHistory`, so this is fine. We add
  a `Log.i` around the migration so we can spot anomalies in production
  logs. **Resolved on first device-test** (2026-04-29): the initial
  CREATE TABLE missed an explicit `NOT NULL` on the `id INTEGER PRIMARY
  KEY AUTOINCREMENT` column, which Room's PRAGMA validator rejects even
  though SQLite implicitly enforces it. Fix landed AND made the
  migration idempotent on the source-column dimension (selects from
  whichever of `recipientDeviceId`/`peerDeviceId` exists), so a
  half-broken prior run can be recovered without data loss. The
  exported `app/schemas/.../9.json` is the canonical reference for
  future Room migrations to diff against.
- **R2 — Selected-pair drift**: `selectedDeviceId` could point at a pair
  the user just unpaired (race between background auto-switch and
  desktop-initiated unpair). `PairingRepository.selected` re-resolves on
  every emission; if the id is gone, it falls back to most-recently-
  paired. Worst case: a one-frame flash of the wrong name. Acceptable.
- **R3 — Auth-failure key explosion**: `_authFailureByPeer` map could
  accumulate stale keys for unpaired peers. `pairingRepo.unpair` clears
  the entry. Lifecycle: keys only created when a real call happens, so
  stale keys after unpair are bounded.
- **R4 — Receiver attribution gap**: if a phone receives a transfer
  whose `sender_id` doesn't match any paired peer (e.g., desktop server
  state corruption or server sending to wrong recipient), today the code
  logs+skips at `service/PollService.kt:538-540`. With per-peer history,
  the row simply never appears. We add a one-line log noting the orphan
  sender id.
- **R5 — Dead code removed pre-plan**: `TransferViewModel.resend()`,
  `TransferViewModel.tryNow()`, `TransferViewModel.statusText` (+ its
  `_statusText` backing flow and the UI-tick write), and
  `TransferViewModel.pairedDeviceName` were all defined but never
  consumed by any UI. Deleted before A.1 lands so the
  `getFirstPairedDevice()` sweep in A.1 doesn't touch ghost call sites.

---

## Test plan (per phase)

- **Pure-Kotlin unit tests** (no Android runtime):
    - `nextDefaultName_test.kt` — empty list → "Desktop", ["Desktop"] →
      "Desktop 2", ["Desktop", "Desktop 3"] → "Desktop 2", ["Desktop",
      "Phone"] → "Desktop 2", case-insensitive collision.
    - `PairingRepository_test.kt` (with a fake KeyManager) — pairs flow
      reflects savePairedDevice/removePairedDevice; selected falls back
      to most-recently-paired when stored id is invalid.
    - `AuthFailureMap_test.kt` — 3-in-a-row per key, clear by key, global
      key behaviour.
- **Room migration test**: `MIGRATION_8_9` round-trip with a v8 fixture
  — verify column rename, data preserved, INCOMING rows get backfilled
  by the runner.
- **Manual integration tests** (against `test_loop.sh` fixture):
    - Pair desktop A → home shows A. Pair desktop B → naming defaults
      "Desktop 2" → home shows selector with "Desktop 2".
    - Send file from selected → goes to selected; switch to A → file
      from A appears, B's row disappears from list.
    - Lock phone, send from A → unlock, app shows A selected. Open app
      first, send from A → no auto-switch.
    - Unpair A from desktop → only A removed; selected falls back to B.
    - 403 on A only → banner names A; tap re-pair → only A removed.

---

## Estimated work

Rough sizing (Claude-day = a focused half-day of focused human review +
implementation):

| Phase | Effort |
|---|---|
| A.1 PairingRepository | 0.5 day |
| A.2 Schema rename | 0.5 day |
| A.3 Naming step | 0.5 day |
| A.4 Settings list | 1 day |
| A.5 Selector header | 1 day |
| A.6 Per-peer history | 0.5 day |
| A.7 Sender-targeted unpair | 0.25 day |
| A.8 Per-peer auth attribution | 1.5 days |
| A.8.5 Notification name suffix | 0.25 day |
| A.9 Background auto-switch | 0.5 day |
| A.9b Find-phone concurrent guard | 0.1 day |
| Tests + integration | 1 day |
| **Total** | **≈ 7.5 days** |

Phases A.1–A.3 unblock UI work; A.4–A.6 are user-visible changes. A.7
and A.9 are behavioural; A.8 is the most invasive (touches every
auth-emitting code path).
