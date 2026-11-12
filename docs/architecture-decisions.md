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

### 2026-11-12 — Locked keyring stops startup; tracker polls back off to 60 s

**Status:** accepted.

**Context.** On 2026-11-11 a one-shot `--send` ran while the keyring
could not be unlocked. `Config` fell back to the JSON store, which was
empty because the identity lives in the keyring, so startup registered a
second device. The next keyring-backed run migrated that plaintext
identity over the paired one. Sends from then on went out under a device
the phone was not paired with and were never fetched. The running tray
still held the old credentials, so the server listed none of those
transfers for it: the stall safeguard only looked at listed transfers,
the rows counted as live outgoing indefinitely, and the tracker plus the
incoming poll hit the relay about four times a second for hours.

**Decision.** Identity:
- `Config` writes `secrets_backend: "keyring"` to `config.json` whenever
  the active store is the keyring. With that marker (or the older
  `secrets_migrated_at`) present and the keyring unreachable,
  `Config.identity_locked_in_keyring` is true, `build_startup_context`
  raises before `KeyManager` can generate a key file, and `main` exits 1
  with a message. `register_device` refuses in the same state.
- Legacy-secret migration keeps a complete keyring identity and drops a
  differing plaintext `device_id` / `auth_token` pair, the same rule
  `KeyManager` applies to a stray PEM.

- Startup compares the stored `device_id` with the hash of the device's
  public key. On a mismatch `--send`, `--pair` and `--headless` exit 1;
  the interactive tray starts with `CREDENTIALS_INVALID` latched so the
  existing full-wipe re-pair banner is the way out.
- The relay refuses `POST /api/transfers/init` with 403 unless sender
  and recipient are paired (ping and fasttrack already did). The desktop
  treats 401/403 at init as terminal.

Polling:
- The tracker's `sent-status` poll stays at 0.5 s while a delivery is
  progressing. After 30 s with no news it doubles its interval on every
  further poll without news, up to 60 s, and resets on recipient
  progress, delivery, a new send, or local upload progress.
- The 2 min stall now also covers transfers the server does not list;
  those are dropped from polling. Listed transfers that stall stop
  counting as live outgoing but stay on the backed-off poll and are
  revived when the recipient moves.
- The incoming poll that stands in for long-polling during a live send
  uses the tracker interval instead of a fixed 0.5 s.

**Alternatives.** (a) Let the fallback run read-only instead of exiting:
every caller that needs credentials would still fail, later and less
clearly. (b) Prefer the newer plaintext identity during migration: it is
the one without pairings. (c) Keep the fixed 0.5 s poll and rely on the
stall give-up alone: bounded per transfer, but it loses delivery
detection on relays without long-polling and still spends 240 requests
on every send to an offline phone.

**Consequences.** A user who removes the keyring for good must delete
`secrets_backend` and `secrets_migrated_at` from `config.json` to start
over with a new identity. Progress painting for a recipient that wakes
after a long silence can lag by up to 60 s. Android's tracker is
unchanged (fixed 500 ms, give-up after 2 min).

**Anchor.** `desktop/src/config.py`: `identity_locked_in_keyring`,
`_record_keyring_backend`, `_drop_plaintext_identity_shadowed_by_store`;
`desktop/src/bootstrap/startup_context.py`: `IdentityLockedInKeyring`,
`stored_identity_matches_key`; `server/src/Services/TransferService.php`:
`init`;
`desktop/src/receive/delivery_tracker.py`: `_tracker_poll_due`,
`_tracker_reschedule`, `_note_absent_transfer`. Tests:
`tests/protocol/test_desktop_delivery_tracker.py`,
`test_desktop_secrets.py` `KeyringLockoutTests`.

---


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

### 2026-05-11 — Reconnect ping reuses a <30 s result; background cadence 15 min

**Status:** accepted.

**Context.** A battery dump from a paired phone showed Desktop
Connector as the #2 power consumer (109 mAh / 4h 50m, 95 % on
`mobile_radio`, 80 `FcmService` launches). Over the same window the
desktop log showed transient `[Errno 101] Network is unreachable`
retry storms. `icon_poll` called `_maybe_ping(0.0)` on every
CONNECTED transition, so each wifi-reassoc / DHCP-renew blip forced a
fresh HIGH-priority FCM wake on the phone, and the 5 min background
probe added ~12 pongs/h, each followed by ~25 s of LTE tail
(android_logs_4/5, 2026-05-12).

**Decision.** The reconnect ping uses `min_age = 30.0`, the same cache
window as the menu-open ping, so sub-30 s blips reuse the recent
result. The background probe interval goes from 300 s to 900 s. The
on-connect and menu-open paths still ping, so the tray dot is fresh
whenever the user actually looks at it.

**Alternatives.** Dropping the reconnect ping entirely (rejected: the
"opened the laptop after lunch" case still wants fast feedback).
Subscribing to NetworkManager D-Bus network-up signals (rejected:
platform-specific; the connection-state callback is portable and
sufficient). Coalescing pongs or debouncing server-side (see
`docs/plans/android-radio-tail-cost.md`; the client-side cadence
change was the smallest fix with the biggest effect).

**Anchor.** `desktop/src/tray/app.py`: `_ping_interval` and the
`min_age` choice in `icon_poll`.

### 2026-05-06 — Keyring service name derives from `config_dir`; device_id lives beside auth_token

**Status:** accepted.

**Context.** The OS keyring is per-user, shared by every Desktop
Connector process on the host. With a hard-coded service name, a dev
twin started with `--config-dir=~/.config/desktop-connector-dev` loaded
the real install's key material and overwrote the real `auth_token`
when it registered against a local relay; recovery needed a full
re-pair. The `DC_KEYRING_SERVICE` env var only helped when someone
remembered to set it. Separately, `auth_token` lived in the keyring
while `device_id` lived in plaintext `config.json`, so an interrupted
registration could leave a half-registered install (token present,
id missing) that every relay call rejected.

**Decision.** `Config` computes the keyring service as
`DC_KEYRING_SERVICE` env, else `config_dir.name`, else
`"desktop-connector"`, and passes it to `open_default_store`. The
default config dir maps to the historical service name, so existing
installs need no migration. `device_id` moves into the secret store
under `SECRET_KEY_DEVICE_ID`; `_migrate_legacy_secrets_if_needed`
moves an existing JSON `device_id` into the keyring on first boot, and
`JsonFallbackStore` keeps the plaintext shape on hosts without
libsecret.

**Alternatives.** Keeping the env var as the only isolation knob
(rejected: isolation that depends on shell discipline fails silently,
and the failure mode is damage to the user's real credentials).
Forcing the JSON fallback for non-default config dirs (rejected:
loses keyring protection for legitimate multi-profile setups).

**Anchor.** `desktop/src/config.py` (`Config.__init__` service
derivation, `device_id` property, `_migrate_legacy_secrets_if_needed`),
`desktop/src/secrets.py` (`SecretServiceStore(service_name=…)`,
`open_default_store`, `SECRET_KEY_DEVICE_ID`).

### 2026-05-06 — File-manager scripts carry a config-id marker for cross-install isolation

**Status:** accepted.

**Context.** `~/.local/share/nautilus/scripts/`, `~/.local/share/nemo/scripts/`,
and `~/.local/share/kservices5/ServiceMenus/` are per-user XDG paths
shared across **all** Desktop Connector installs on a host. A test
run launched a dev twin
(`--config-dir=~/.config/desktop-connector-dev`, no pairings); on
startup the twin's `sync_file_manager_targets` call iterated the shared
Nautilus dir, treated the canonical install's "Send to Vivo Phone"
managed script as stale (because its peer wasn't in the dev twin's
empty pair list), and unlinked it. Same shape as the 2026-05-06
keyring-isolation bug (see the keyring entry above) but on a different shared
resource. Per `feedback_test_isolation.md` the rule is: shared-resource
isolation must live in the code path, not in shell discipline.

**Decision.** Every managed file-manager entry now embeds a
`# desktop-connector:config-id=<config_dir.name>` marker alongside the
existing `MANAGED_SENTINEL` and `PAIRING_ID_PREFIX`. Both the cleanup
pass and the write-collision check honour ownership: a managed entry
whose marker doesn't match the current `config_dir.name` is left alone
(even if it would otherwise look stale), and the write pass refuses to
clobber such an entry with `skip_other_config_collision`. Pre-fix
unmarked managed entries (and unmarked legacy "Send to Phone" scripts)
are treated as canonical-owned: only the canonical install
(`config_dir.name == "desktop-connector"`, the XDG default) adopts and
rewrites them with the marker on first sync; alternate-config installs
leave them untouched.

**Alternatives.** (a) Skip `sync_file_manager_targets` entirely on
non-default config dirs — one-line change, but loses multi-profile
support for power users running e.g. AppImage + dev-tree side by side
with their own paired phones. (b) Add a `--no-file-manager-sync` flag
used only by test runs — same shape as the
`DC_KEYRING_SERVICE` mistake the rule above warns against (easy to
forget, leaves shared-state damage as the failure mode). (c) The
config-id marker — chosen — costs one comment line per script and
keeps multi-profile working correctly.

**Anchor.** `desktop/src/file_manager_integration.py`:
`CONFIG_ID_PREFIX`, `_config_marker`, `_owns`, `_extract_config_id`;
the cleanup ownership gate and the collision refusal in
`_sync_script_dir` and `_sync_dolphin_service`. Tests:
`tests/protocol/test_desktop_file_manager_integration.py`
`FileManagerCrossConfigIsolationTests`.

---

_(add new decisions above this section header)_
