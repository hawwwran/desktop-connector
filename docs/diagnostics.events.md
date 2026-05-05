# Diagnostic events

Canonical vocabulary for cross-platform logging across the PHP server,
Python desktop, and Kotlin Android runtimes. Landed as part of
refactor-9. This document is the authoritative reference — all three
runtimes emit event names from this catalog.

The goal is not structured logging. It's *consistent* logging. The
event name lives inside the free-form log message as a dot-notation
string anchor, so the existing text logs stay human-readable and no
runtime needs a new logger class.

---

## Privacy rule (must never be violated)

Never log confidential data, regardless of how convenient it would be
for debugging. Specifically:

- **Cryptographic material** — private keys, symmetric keys, key
  derivations, HKDF salts, anything key-like.
- **Credentials** — `auth_token`, Bearer headers, FCM service-account
  JSON, OAuth JWTs.
- **FCM registration tokens** (`fcm_token`) — Firebase policy
  classifies these as sensitive.
- **Public keys of other parties** — `public_key` / `phone_pubkey`.
  Not secret by the math, but leaking them from logs weakens pairing's
  auditability.
- **Decrypted user data** — clipboard text/images, file bytes, file
  names when they are reasonably sensitive, GPS coordinates, any
  `.fn.*` payload.
- **Encrypted ciphertext** — `encrypted_meta`, `encrypted_data`, blob
  bytes. Opaque to the server but still user data.

What IS safe to log: `transfer_id`, `message_id`, `device_id` (first
12 chars), sizes (`size=`, `chunks=`), outcomes (`sent|failed|no_token`),
counts, retry-after values, `rtt_ms`, `via`, `reason`, `error_kind`.

When in doubt: log the outcome and the correlation ID, not the data.

---

## Naming pattern

`<category>.<subject>.<outcome>` — lower-case, dots only, no spaces.

Examples:
- `transfer.init.accepted`
- `transfer.chunk.uploaded`
- `ping.request.rate_limited`
- `poll.notify.timeout`
- `clipboard.write_text.succeeded`

Outcome verbs are drawn from a short list to keep grep queries
predictable: `accepted`, `started`, `progressed`, `completed`,
`succeeded`, `failed`, `skipped`, `timed_out`, `retried`, `ignored`,
`received`, `sent`, `stored`, `acked`, `rate_limited`, `stall`.

---

## Categories

| Category | Runtimes | Purpose |
|---|---|---|
| `startup` | all | App/service boot, FCM init, migrations |
| `auth` | server | Auth header validation outcomes |
| `pairing` | all | QR → request → confirm → unpair lifecycle |
| `transfer` | all | Init, chunk upload, upload-complete, download, progress |
| `delivery` | all | Sender-side delivery tracking: progress, acked, stall |
| `fasttrack` | all | Message send / store / pending / ack / command dispatch |
| `ping` | all | Liveness probe: request / fcm / rate-limit / pong |
| `poll` | all | Long-poll and regular-poll state |
| `fcm` | desktop/android | Firebase init, token, incoming message |
| `connection` | desktop/android | Health check, backoff state machine |
| `clipboard` | desktop/android | Clipboard read/write outcomes |
| `notification` | desktop/android | System-notification display/send |
| `platform` | desktop/android | Shell/open, dialogs, subprocess spawn, permissions |
| `apierror` | server | Top-level Router ApiError catch |

---

## Severity rules

Minimal and opinionated. Pick the level by what action, if any, an
operator should take when they see the line.

- **`info`** — a lifecycle milestone an operator might want to see in
  a normal session (init accepted, upload completed, delivery acked,
  pairing confirmed, ping received). Use sparingly.
- **`warning`** — an expected-failure-with-recovery: retry, fallback
  to polling, decrypt failure on one chunk (will re-download),
  capability missing (no clipboard tool installed).
- **`error`** — a flow-ending failure. The operation gave up.
- **`debug`** — per-chunk, per-tick, per-retry-attempt granularity.
  Off by default on server and Android; on by default via `--verbose`
  on desktop.

---

## Correlation IDs

Every event that has access to an ID should include it. IDs are always
truncated to the **first 12 hex characters** on all three runtimes so
grepping cross-runtime lines works.

- `transfer_id` — any transfer event
- `message_id` — any fasttrack event
- `device_id` — any auth/pairing/ping event; always the caller's own
  side (the server-side log, for instance, logs the device_id of the
  request's `X-Device-ID`, not the recipient)
- `sender_id` / `recipient_id` — transfer and fasttrack events where
  direction matters
- `chunk_index` — per-chunk transfer events
- `rtt_ms` / `via` — ping response events
- `retry_after` — rate-limit events
- `reason` / `error_kind` — free-form short strings on failure events

Do NOT log:
- full URLs, file paths, or filenames when the content is
  user-sensitive (filenames for ordinary file transfers are OK; think
  twice for clipboard content)
- request bodies
- response bodies beyond the outcome status

---

## Event catalog

The core set. Not every event has to be emitted on every runtime —
the matrix column `where` names the runtimes that emit it. Runtimes
marked `(new)` land in refactor-9; others already exist and are being
renamed/restructured.

### startup

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `startup.app.started` | desktop, android | info | — | Desktop: existing "Already registered as …" also emits `startup.device.registered` |
| `startup.device.registered` | desktop, android | info | `device_id` | First-time registration with the server |
| `startup.fcm.initialized` | android | info | `project_id` | Firebase dynamic init; project_id is non-secret |
| `startup.fcm.init_failed` | android | warning | `error_kind` | Falls back to long-poll |
| `multipair.migration.done` | android | info | — | One-shot post-Room migration — renamed legacy unnamed pairs and backfilled INCOMING `peerDeviceId`. Gated by `AppPreferences.multiPairMigrationDone`, fires once per install |

### auth

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `auth.missing` | server | warning | `uri` | No X-Device-ID / Bearer headers |
| `auth.invalid` | server | warning | `device_id`, `uri` | Credentials didn't match |
| `auth.failure.tripped` | android | warning | `kind`, `peer`, `count` | 3-in-a-row auth-failure streak latched. `peer` is the truncated device id when 403 PAIRING_MISSING was attributable, empty for global (CREDENTIALS_INVALID always lands here, plus unattributed 403s) |
| `register.conflict` | server | info | `device_id`, `reason=already_registered` | `/api/devices/register` was called with a `public_key` whose `device_id` already exists. Server refuses with 409 instead of returning the existing `auth_token` (closes the credential-leak vector — public keys are not secret material, so anyone holding a QR / `.dcpair` could otherwise harvest tokens). Legitimate clients normally short-circuit registration after the first success; recovery clients with no local pairs must rotate their keypair and retry registration with a fresh public key. |

### pairing

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `pairing.qr.generated` | desktop | info | `device_id` | QR material never logged |
| `pairing.qr.scanned` | android | info | — | User scanned QR; no identifiers yet |
| `pairing.request.sent` | android (new) | info | `desktop_id` | Pairing request posted |
| `pairing.register.conflict_keypair_rotated` | android | info | — | Android hit server-side 409 registration conflict while it had no local pairs, rotated its local keypair, and retried registration with a fresh public key. |
| `pairing.request.received` | server (new) | info | `desktop_id`, `phone_id` | Row inserted in `pairing_requests` |
| `pairing.request.claimed` | server (new) | info | `desktop_id`, `count` | Desktop polled and claimed N requests |
| `pairing.confirm.accepted` | server (new), desktop, android (new) | info | `device_a`, `device_b` | Pairing row created |
| `pairing.unpair.received` | desktop, android | info | `peer_id` | `.fn.unpair` consumed |
| `pairing.unpair.sent` | desktop, android | info | `peer_id` | Local user unpaired, notifying peer |
| `pairing.key.shown` | desktop (M.11) | info | — | User opened the "Show pairing key" dialog. Key contents never logged. |
| `pairing.key.exported` | desktop (M.11) | info | `path` | User saved the pairing key to a `.dcpair` file. The user-chosen path is logged; key contents are not. |
| `pairing.key.export_failed` | desktop (M.11) | warning | `err` | OS error writing the export file (read-only mount, permissions). |
| `pairing.key.import_parse_failed` | desktop (M.11) | warning | `surface`, `err` | D9 parser rejected. `surface ∈ {text, file}`. No payload contents in the log. |
| `pairing.key.import_self_pair_refused` | desktop (M.11) | warning | — | D8/D10 self-pair refusal (device id matched the local one). |
| `pairing.key.import_relay_mismatched` | desktop (M.11) | warning | `local`, `remote` | D8 relay mismatch. **Hostnames only** — full URLs are not logged because they may carry subdirectory tokens or query material. |
| `pairing.key.import_already_paired_refused` | desktop (M.11) | warning | `peer` | The pairing key targets a device id that is already in `paired_devices`. |
| `pairing.key.import_request_failed` | desktop (M.11) | warning | `peer` | Relay refused the pairing request (inviter window closed, transient network failure). |
| `pairing.request.sent_as_joiner` | desktop (M.11) | info | `target` | Joiner-side counterpart of `pairing.request.sent`. Short-id of the inviter we sent a request to. |

### transfer

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `transfer.init.accepted` | server (new), desktop, android | info | `transfer_id`, `sender`, `recipient`, `chunks` | Row created |
| `transfer.init.failed` | desktop, android | error | `error_kind` | Pre-upload validation failed |
| `transfer.init.waiting` | desktop, android | warning | `transfer_id`, `reason=storage_full` | 507 from server; client will retry until cap |
| `transfer.init.waiting.timed_out` | desktop, android | warning | `transfer_id`, `elapsed_ms` | 30-min retry budget exhausted — row flipped to Failed |
| `transfer.init.too_large` | desktop, android | error | `transfer_id` | 413 from server; transfer alone exceeds quota — terminal |
| `transfer.cancel.accepted` | server | info | `transfer_id`, `sender`, `recipient` | Sender DELETE tore down in-flight transfer |
| `transfer.chunk.uploaded` | server (new), desktop, android | debug | `transfer_id`, `chunk_index`, `size` | Per-chunk; debug by default |
| `transfer.chunk.failed` | desktop, android | warning | `transfer_id`, `chunk_index`, `attempt` | Will be retried |
| `transfer.upload.completed` | server (new), desktop, android | info | `transfer_id`, `sender`, `recipient`, `chunks` | All chunks received by server |
| `transfer.pending.found` | desktop, android | info | `count` | Poll returned pending transfers |
| `transfer.download.started` | desktop, android | info | `transfer_id`, `sender`, `chunks` | Download begun |
| `transfer.chunk.served` | server (new) | debug | `transfer_id`, `chunk_index` | Server sent a chunk to recipient (classic) |
| `transfer.chunk.served_and_pending_ack` | server | debug | `transfer_id`, `chunk_index` | Streaming: chunk served, awaiting per-chunk ACK |
| `transfer.chunk.acked_and_deleted` | server | debug | `transfer_id`, `chunk_index` | Streaming: per-chunk ACK removed the blob from disk |
| `transfer.chunk.too_early` | server, desktop, android | debug | `transfer_id`, `chunk_index` | Streaming: recipient got 425 — chunk not yet stored. Surfaces via `apierror.caught` at 425 — no dedicated info-level line to avoid spamming |
| `transfer.download.completed` | desktop, android | info | `transfer_id`, `bytes` | File saved locally |
| `transfer.download.cancelled` | android | info | `transfer_id`, `chunk_index` | User deleted row mid-download |
| `transfer.wake.sent` | server (new) | info | `transfer_id`, `target`, `fcm_result`, `fcm_type` | FCM push; `fcm_type ∈ {transfer_ready}` (classic) |
| `transfer.stream.ready` | server | info | `transfer_id`, `sender`, `recipient` | Streaming: first chunk stored, `stream_ready` FCM fired |
| `transfer.stream.waiting_quota` | server | warning | `transfer_id`, `chunk_index`, `current`, `cap` | Streaming: chunk upload bounced on 507 — recipient's on-disk bytes would exceed quota |
| `transfer.abort.sender` | server | info | `transfer_id`, `sender`, `recipient`, `reason` | `DELETE` by sender, reason=sender_abort |
| `transfer.abort.recipient` | server | info | `transfer_id`, `sender`, `recipient`, `reason` | `DELETE` by recipient, reason=recipient_abort |
| `transfer.abort.wake.sent` | server | info | `transfer_id`, `target`, `fcm_result`, `fcm_type` | Abort FCM wake to the opposite party |
| `transfer.cleanup.expired` | server (new) | info | `count` | Sweep deleted expired rows |
| `transfer.cleanup.invariant_violation` | server | warning | `id`, `reason` | Invariant assertion threw on a corrupt row inside `deleteTransferFiles` — cleanup proceeded with the delete anyway. Recovery path; absence of this event after a corrupt-row symptom means the cleanup never ran |
| `transfer.cleanup.failed` | server | warning | `reason` | Opportunistic cleanup raised in `/api/transfers/pending`'s 1-in-20 sampling. Caught at the controller so the response stays 200; the actual reason names which step inside the cleanup misbehaved |

### delivery

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `delivery.progress` | server (new), desktop, android | debug | `transfer_id`, `chunks_downloaded`, `chunk_count` | Progress advanced |
| `delivery.acked` | server (new), desktop, android | info | `transfer_id`, `recipient`, `total_bytes` | Recipient ACK finalized the transfer |
| `delivery.tracker.stall` | desktop, android | warning | `transfer_id`, `stall_seconds` | Tracker gave up; transfer row preserved |

### fasttrack

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `fasttrack.message.send_started` | desktop, android | info | `recipient`, `size` | Outgoing; payload never logged |
| `fasttrack.message.send_received` | server | info | `sender`, `recipient`, `size` | Existing `"[Fasttrack] send from=…"` renamed |
| `fasttrack.message.stored` | server | info | `message_id`, `fcm_result` | Existing `"stored message_id=…"` renamed |
| `fasttrack.message.send_succeeded` | desktop, android | info | `message_id` | Server accepted |
| `fasttrack.message.send_failed` | desktop, android | error | `error_kind` | |
| `fasttrack.message.rate_limited` | server (new) | warning | `sender`, `recipient`, `pending_count` | 429 fired |
| `fasttrack.message.pending_listed` | server, desktop, android | debug | `recipient`, `count` | Skip when count=0 |
| `fasttrack.message.processed` | android | info | `message_id`, `fn` | Payload never logged |
| `fasttrack.message.acked` | server, desktop, android | info | `message_id`, `by` | |
| `fasttrack.command.sent` | desktop, android | info | `fn`, `recipient` | e.g. `fn=find-phone action=start` |
| `fasttrack.command.received` | desktop, android | info | `fn`, `sender` | On `.fn.*` dispatch |
| `fasttrack.command.unknown` | desktop, android | warning | `fn` | |
| `findphone.start.accepted` | desktop (M.8) | info | `peer`, `silent` | Receiver accepted a locate request; alert + heartbeats begin |
| `findphone.start.dropped_concurrent` | desktop (M.8), android | info | `active`, `new` | Second find-phone start arrived while already ringing for a different sender; FCFS, second start ignored |
| `findphone.stop.accepted` | desktop (M.8) | info | `peer` | Active sender's stop accepted; heartbeat + alert torn down |
| `findphone.stop.ignored` | desktop (M.8) | info | `reason`, `active`, `saw` | Stop arrived from a non-active sender (e.g. `wrong_sender`) |
| `findphone.timeout` | desktop (M.8) | info | — | 5 min hard cap fired; treated as a local stop |
| `findphone.command.dropped` | desktop (M.8) | warning | `reason` | Inbound message lacked sender id; pre-dispatch drop |
| `findphone.alert.start_failed` / `findphone.alert.stop_failed` | desktop (M.8) | error | `peer` | GTK4 modal subprocess or sound thread failed; responder still sends heartbeats |
| `findphone.alert.subprocess_failed` | desktop (M.8) | error | `sender` | `Popen` for the locate-alert window failed (no DISPLAY, missing GTK, etc.) |
| `findphone.alert.sound_skipped` | desktop (M.8) | info | `reason` | `no_sound_file` or `no_player` — alert is visual only this session |
| `findphone.alert.sound_started` / `findphone.alert.sound_stopped` | desktop (M.8) | info | `player` | Player binary used (`paplay`, `aplay`, `play`, `mpv`) |
| `findphone.consumer.started` / `findphone.consumer.stopped` | desktop (M.8) | info | — | Background fasttrack consumer loop lifecycle |
| `findphone.consumer.dropped` | desktop (M.8) | warning | `reason`, `peer`?, `kind`? | Inbound message dropped — `no_sender_id`, `unknown_sender`, `base64_decode`, `decrypt_failed`, `json_parse`, `non_dict_payload` |
| `findphone.consumer.unhandled` | desktop (M.8) | debug | `fn`, `peer` | Sender-side response (e.g. `fn=find-phone state=ringing`) seen by the receiver-side consumer; ignored, ACKed |
| `findphone.consumer.ack_failed` | desktop (M.8) | debug | — | Best-effort ACK failed (transient network); message will expire server-side |
| `findphone.update.skipped` | desktop (M.8) | warning | `reason`, `peer` | Outbound state update skipped (`no_symkey` for an unpaired recipient) |
| `findphone.update.encrypt_failed` / `findphone.update.fasttrack_send_failed` | desktop (M.8) | error | `peer` | Encrypt or transport leg of an outbound state update failed |
| `findphone.update.send_failed` / `findphone.update.send_rejected` | desktop (M.8) | error/warning | `peer`, `state` | Responder couldn't queue an update; heartbeat thread retries on next tick |
| `findphone.heartbeat.loop_failed` / `findphone.heartbeat.cancel_failed` | desktop (M.8) | exception/debug | — | Heartbeat thread book-keeping failures; session continues |
| `findphone.location.unavailable` | desktop (M.9) | info | `reason`, `err`? | GeoClue connect failed (`gi_import_failed`, `geoclue_unreachable`, `geoclue_start_failed`); receiver falls back to state-only heartbeats |
| `findphone.location.connected` | desktop (M.9) | info | `backend` | `backend=geoclue` — D-Bus client started; future fixes flow through `LocationUpdated` signals |
| `findphone.location.fix_updated` | desktop (M.9) | info | `accuracy` | Accuracy radius (meters) only; raw lat/lng never logged |
| `findphone.location.provider_failed` | desktop (M.9) | exception | — | LocationProvider raised; this tick falls back to state-only |

### ping

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `ping.request.sent` | desktop | info | `recipient` | Desktop-initiated probe |
| `ping.request.received` | server (new) | info | `sender`, `recipient` | |
| `ping.request.rate_limited` | server (new), desktop | warning | `sender`, `recipient`, `retry_after` | 429 |
| `ping.response.fresh` | server (new) | info | `sender`, `recipient` | Short-circuit; phone talked to server this second |
| `ping.fcm.sent` | server (new) | info | `sender`, `recipient` | HIGH-priority wake dispatched |
| `ping.fcm.timeout` | server (new) | info | `sender`, `recipient` | Phone didn't respond within 5s |
| `ping.response.received` | desktop | info | `recipient`, `via`, `rtt_ms` | `via ∈ {fresh, fcm, no_fcm, fcm_failed, fcm_timeout}` |
| `ping.pong.sent` | android | info | — | Phone's onMessageReceived fired pong |
| `ping.pong.received` | server (new) | info | `device_id` | Auth middleware already bumped last_seen |

### poll

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `poll.notify.started` | server (new) | debug | `device_id`, `since`, `is_test` | Long-poll loop entered |
| `poll.notify.event` | server (new) | info | `device_id`, `pending`, `delivered`, `progress` | Loop woke on a state change |
| `poll.notify.timeout` | server (new), desktop | info | `device_id` | 25s window expired |
| `poll.notify.available` | desktop, android | info | — | Long-poll probe succeeded |
| `poll.notify.unavailable` | desktop, android | warning | — | Falls back to regular polling |
| `poll.loop.screen_off` | android | info | — | Phone paused polling |
| `poll.loop.screen_on` | android | info | — | Phone resumed polling |
| `poll.loop.fcm_wake` | android | info | `type` | FCM woke the poll loop |
| `fgs.start.denied` | android | error | `reason` | `startForeground` rejected (e.g. `dataSync` budget exhausted on Android 15+, or background-restricted). PollService stops itself; activity-foreground retry will re-attempt on next app open |
| `fgs.bind.denied` | android | error | `reason` | `startForegroundService` from `PollService.start(context)` rejected by the system; service never bound |
| `fgs.type.changed` | android | info | `location` | Boolean `location` indicates whether LOCATION was added to the FGS type for find-phone GPS |
| `fgs.type.denied` | android | warning | `reason` | `setForegroundType` upgrade refused (e.g. missing location permission) |

### fcm

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `fcm.token.registered` | android | info | — | Token sent to server; value never logged |
| `fcm.token.refreshed` | android | info | — | |
| `fcm.message.received` | android | info | `type` | `type ∈ {ping, transfer_ready, fasttrack}` |

### connection

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `connection.check.started` | android (new) | debug | — | Health probe started |
| `connection.check.succeeded` | desktop, android (new) | info | — | |
| `connection.check.failed` | desktop, android (new) | warning | `error_kind` | |
| `connection.backoff.retry` | desktop, android (new) | warning | `attempt`, `delay_seconds` | Warning when attempt > 3, info otherwise |

### clipboard

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `clipboard.read.succeeded` | desktop, android | info | `kind`, `length` | `kind ∈ {text, image}` |
| `clipboard.read.failed` | desktop, android | error | `error_kind` | |
| `clipboard.write_text.succeeded` | desktop, android | info | `length` | Never log the text |
| `clipboard.write_text.failed` | desktop, android | warning | `error_kind` | |
| `clipboard.write_image.succeeded` | desktop, android | info | `size` | |
| `clipboard.write_image.failed` | desktop, android | warning | `error_kind` | |
| `clipboard.tool.missing` | desktop | warning | — | No xclip/wl-copy found |
| `clipboard.subtype.unknown` | desktop, android | warning | `subtype` | |

### notification

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `notification.shown` | desktop, android | debug | `kind` | `kind ∈ {transfer, alarm, connection, …}` |
| `notification.send.failed` | desktop, android | warning | `error_kind` | |
| `notification.tool.missing` | desktop | warning | — | No notify-send found |

### platform

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `platform.open_url.succeeded` | desktop, android | info | `length` | Never log the URL |
| `platform.open_url.failed` | desktop, android | warning | `error_kind` | |
| `platform.open_folder.succeeded` | desktop | info | — | |
| `platform.open_folder.failed` | desktop | warning | `error_kind` | |
| `platform.dialog.failed` | desktop | warning | `error_kind` | zenity / file picker error |
| `platform.subprocess.spawned` | desktop | info | `window_name` | GTK4 window launch |
| `platform.permission.requested` | android | info | `permission` | |
| `platform.permission.granted` | android | info | `permission` | |
| `platform.permission.denied` | android | warning | `permission` | |

### device (multi-device support)

Desktop multi-device events introduced by the
`docs/plans/desktop-multi-device-support.md` rollout (M.0–M.10). All
fields are short id (12 chars max) so the same correlation rule
applies as elsewhere.

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `device.active.changed` | desktop (M.0+) | info | `peer`, `reason` | Active connected device changed. `reason ∈ {test, paired, incoming, outgoing, find_device_start, find_device_incoming, ...}` per D2 of the plan |
| `device.name.normalized` | desktop (M.0) | info | `peer` | Duplicate display name found at startup; the later duplicate was renamed to `<name> <short_id>` and persisted. Should only fire once after a legacy / hand-edited `config.json` is opened |
| `file_manager.<kind>.write` | desktop (M.6) | info | `peer`, `name` | Per-pairing Nautilus / Nemo script created or refreshed. `<kind> ∈ {nautilus, nemo}` |
| `file_manager.<kind>.cleaned` | desktop (M.6) | info | `name`, `peer` | Stale managed script removed because the pairing was unpaired, renamed, or the file's pairing id no longer matches |
| `file_manager.<kind>.legacy_removed` | desktop (M.6) | info | `name` | Pre-multi-device "Send to Phone" script adopted via fingerprint and removed; per-device replacements come from the same sync pass |
| `file_manager.dolphin.written` | desktop (M.6) | info | `peers` | Dolphin service-menu file rewritten with N actions, one per paired device |
| `file_manager.dolphin.removed` | desktop (M.6) | info | `reason=no_pairs` | Dolphin file removed when the last pairing was deleted |

### config

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `config.permissions.weak` | desktop | warning | `path`, `mode`, `expected` | Existing `config.json` found with group/world bits; auto-fixed on next save. See hardening-plan H.1. |
| `config.permissions.dir_chmod_failed` | desktop | warning | `dir`, `err` | Could not tighten the config dir to 0o700 (rare; e.g. read-only mount, ACL conflict) |
| `history.permissions.weak` | desktop | warning | `path`, `mode`, `expected` | Existing `history.json` with group/world bits; auto-fixed on next write |
| `config.secrets.using_keyring` | desktop | info | `service` | Secret Service backend (libsecret / KWallet) reachable; auth_token + pairing symkeys live there. H.3+. |
| `config.secrets.fallback_to_json` | desktop | warning | `reason` | Secret Service unreachable (no D-Bus session, no daemon, package missing); secrets stay in plaintext `config.json`. The desktop keeps running — H.5 surfaces the state via stderr (CLI) and a clickable tray menu row. |
| `config.secrets.user_warned` | desktop | warning | `surface` | User-facing surface emitted the H.5 warning. `surface` ∈ {`cli`, `tray`} — `cli` once per process start when fallback is active; `tray` each time the user clicks the warning row in the menu. |
| `config.secrets.scrub.skipped` | desktop | info | `reason` | `Config.scrub_secrets()` was a no-op. `reason=insecure_store` means the JSON fallback is active and there's no secure backend to migrate into. H.6. |
| `config.secrets.scrub.result` | desktop | info | `secure`, `scrubbed`, `failed` | `Config.scrub_secrets()` ran. `scrubbed` is the count of plaintext fields removed; `failed` is the count that couldn't be migrated (left in JSON for next boot to retry). H.6. |
| `config.secrets.private_key.migrated` | desktop | info | `bytes` | One-shot move of `keys/private_key.pem` into the OS keyring. Fires once per install on the first boot after the keyring becomes reachable for an existing on-disk PEM. H.7. |
| `config.secrets.private_key.generated_to_keyring` | desktop | info | — | Fresh-install path: a new X25519 keypair was generated and written directly into the keyring (no PEM ever hit disk). H.7. |
| `config.secrets.private_key.stale_pem_removed` | desktop | info | — | Defensive cleanup: the keyring already held the live private key, but a leftover `keys/private_key.pem` was found and removed. Should only fire after a partially-completed migration. H.7. |
| `config.secrets.private_key.migration_failed` | desktop | warning | `reason` | `keyring.set_password` raised mid-migration; the PEM is left in place for next boot to retry. H.7. |
| `config.secrets.private_key.generate_to_keyring_failed` | desktop | warning | `reason` | Fresh-install fast path failed to write the new private key into the keyring; falling back to a PEM file with `chmod 0o600`. H.7. |
| `config.secrets.private_key.store_corrupt` | desktop | error | `reason` | The keyring entry exists but didn't parse as PEM. Refuses to silently regenerate — manual triage via seahorse expected. H.7. |
| `config.secrets.private_key.reset_store_failed` / `pem_unlink_failed` / `store_read_failed` / `pem_read_failed` / `pem_parse_failed` / `scrub_read_failed` | desktop | warning / error | `reason` | Localised failures in the H.7 lifecycle paths. None are fatal; the surrounding logic logs and continues. |
| `config.secrets.migrated` | desktop | info | `count`, `keys` | Migrated N plaintext secrets out of config.json into the keyring. `keys` is a comma-separated list of canonical key names (`auth_token`, `pairing_symkey:<id12>`); never the secret values. H.4. |
| `config.secrets.migration_failed` | desktop | warning | `key`, `reason` | A single migration step failed (e.g. keyring went down mid-migration). Plaintext left in place; retried on next boot. |
| `config.secrets.delete_failed` | desktop | warning | `key`, `reason` | Could not delete a keyring entry during `remove_paired_device` / `wipe_credentials`. Orphan left behind; harmless until next wipe. |

### apierror

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `apierror.caught` | server | warning | `status`, `uri`, `reason` | Router top-level catch; message must not echo request body |

### vault (T0 / T7-T17)

The vault subsystem emits a richer event vocabulary because of the
sync-engine + crypto + GC machinery. Sub-topics are atomic-write GC,
baseline + restore, eviction, sync (watcher / pending ops / two-way),
recovery test, security (rotation + reminder), purge scheduling,
clear flows, import / migration, and the tray entry points. Sensitive
material is never logged: no Vault Master Key, no recovery
passphrase, no Vault Access Secret, no plaintext filenames in the
relay log (filenames are local-only).

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `vault.atomic.sweep_failed` | desktop | error | `binding` | F-Y14 — startup sweep threw on a binding |
| `vault.atomic.sweep_removed` | desktop | info | `root`, `count` | Startup swept ≥1 orphan `*.dc-temp-*` file (T11.1) |
| `vault.atomic.sweep_stat_failed` | desktop | warning | `path`, `error` | Couldn't stat a candidate during sweep; skipped |
| `vault.atomic.sweep_unlink_failed` | desktop | warning | `path`, `error` | Sweep matched a temp file but couldn't unlink it |
| `vault.atomic.temp_unlink_failed` | desktop | warning | `path`, `error` | Live atomic-write couldn't clean up its own temp |
| `vault.baseline.skip_unsafe` | desktop | warning | `path` | Baseline refused to write a path traversing outside the binding root |
| `vault.debug_bundle.schema_dump_failed` | desktop | warning | `path`, `error` | T17.5 — schema dump failed; bundle still produced |
| `vault.download.duplicate_path` | desktop | warning | `path` | F-D09 — folder download saw two entries claiming the same relative path |
| `vault.download.entry_has_no_version` | desktop | warning | `path` | F-D09 — folder download skipped a version-less entry |
| `vault.download.skip_unsafe_path` | desktop | warning | `path`, `error` | F-D09 — folder download skipped a manifest path that escapes the root |
| `vault.eviction.no_more_candidates` | desktop | info | `vault_id` | Quota pressure ran out of evictable old versions |
| `vault.eviction.tombstone_purged_early` | desktop | info | `vault_id`, `path` | Tombstone purged before retention horizon under quota pressure |
| `vault.eviction.tombstone_purged_expired` | desktop | info | `vault_id`, `path` | Tombstone purged after retention horizon (normal) |
| `vault.eviction.version_purged` | desktop | info | `vault_id`, `path`, `version_id` | Old version evicted to free space |
| `vault.folder.cleared` | desktop | info | `remote_folder_id`, `tombstoned`, `author` | T14.1 bulk-soft-delete published |
| `vault.gc.unlink_failed` | server | warning | `plan`, `path` | F-S12 — gcExecute couldn't remove a chunk file |
| `vault.import.refused` | desktop | warning | `vault_id`, `reason` | Import refused (different vault, tampered, wrong passphrase) |
| `vault.integrity.list_revisions_unavailable` | desktop | info | `vault`, `error` | T17.3 — relay didn't expose per-revision listing; head-only |
| `vault.migration.verify.chunk_aead_failed` | desktop | warning | `chunk`, `error` | F-C05 — chunk failed AEAD during sample |
| `vault.migration.verify.chunk_fetch_failed` | desktop | warning | `chunk`, `error` | F-C05 — relay get_chunk failed during sample |
| `vault.migration.verify.chunk_truncated` | desktop | warning | `chunk` | F-C05 — chunk too short to be a valid envelope |
| `vault.migration.verify_failed` | desktop | error | `vault_id`, `reason` | Source/target hash diff during T9.4 verify |
| `vault.open.ok` | desktop | info | `vault_id` | Vault unlocked from cached grant or fresh passphrase |
| `vault.prepare.ok` | desktop | info | `vault_id`, `revision` | Vault create/preflight succeeded |
| `vault.publish.ok` | desktop | info | `vault_id`, `revision` | Manifest CAS-published successfully |
| `vault.purge.cancelled` | desktop | info | `vault`, `job_id` | User cancelled before fire |
| `vault.purge.cleared_all_on_toggle_off` | desktop | info | `count` | T14.5 toggle-OFF wiped pending purges |
| `vault.purge.executed` | desktop | info | `vault`, `job_id` | T14.4 hard-purge fired and cleaned local state |
| `vault.purge.scheduled` | desktop | info | `vault`, `job_id`, `scope`, `scheduled_for` | T14.3 hard-purge queued |
| `vault.purge.state_read_failed` | desktop | warning | `path`, `error` | Pending-purges JSON unreadable; treating as empty |
| `vault.recovery_test.*` | desktop | info | varies | Subsystem for the M1 recovery-test dialog (T3.5/T3.6) |
| `vault.repair.marked_broken` | desktop | info | `count`, `author`, `revision` | T17.4 — broken-version markers committed |
| `vault.restore.skip_symlinked_dest` | desktop | warning | `path`, `reason` | F-D28 — refused to write through a symlink in destination |
| `vault.restore.skip_unsafe` | desktop | warning | `path` | Restore refused a path traversing outside the destination |
| `vault.security.reminder_read_failed` | desktop | warning | `path`, `error` | T13.6 rotation reminder unreadable; treating as cleared |
| `vault.sync.binding_disconnect_cancelled_inflight_cycle` | desktop | info | `binding` | F-Y08 — disconnect cancelled an in-flight cycle via the registry |
| `vault.sync.binding_disconnect_noop` | desktop | info | `binding` | Disconnect on already-unbound binding |
| `vault.sync.binding_disconnected` | desktop | info | `binding`, `sync_mode`, `local_entries_preserved`, `pending_ops_dropped` | T12.5 disconnect |
| `vault.sync.binding_pause_cancelled_inflight_cycle` | desktop | info | `binding` | F-Y08 — pause cancelled an in-flight cycle via the registry |
| `vault.sync.binding_pause_noop` | desktop | info | `binding` | Pause on already-paused binding |
| `vault.sync.binding_paused` | desktop | info | `binding`, `sync_mode`, `pending_ops` | T12.4 pause |
| `vault.sync.binding_resume_noop` | desktop | info | `binding` | Resume on already-bound binding |
| `vault.sync.binding_resumed` | desktop | info | `binding`, `sync_mode`, `pending_ops` | T12.4 resume |
| `vault.sync.cycle_cancelled_between_ops` | desktop | info | `binding`, `remaining` | F-Y08 — backup-only loop bailed before the next op |
| `vault.sync.delete_cas_exhausted` | desktop | warning | `binding`, `path` | F-Y06 — tombstone retry budget exhausted |
| `vault.sync.delete_cas_retry` | desktop | info | `attempt`, `binding`, `path` | F-Y06 — tombstone publish hit CAS race; retrying |
| `vault.sync.delete_failed` | desktop | warning | `binding`, `path`, `error` | Delete op left in queue with attempts++ |
| `vault.sync.delete_refetch_failed` | desktop | warning | `binding`, `error` | F-Y06 — couldn't refetch head between retries |
| `vault.sync.file_moved_to_trash` | desktop | info | `path` | T11.4 trash-on-delete (sync flow, not user-initiated) |
| `vault.sync.file_skipped_ignored` | desktop | info | `binding`, `path`, `pattern` | T6.4 ignore-pattern match |
| `vault.sync.file_skipped_too_large` | desktop | warning | `binding`, `path`, `size`, `cap` | T6.4 size cap (default 2 GiB) |
| `vault.sync.file_stability_hung` | desktop | warning | `path`, `waited` | T10.4 stability gate hung-after cap hit |
| `vault.sync.flush_skipped_paused` | desktop | info | `binding` | F-Y01 — sync now no-op for paused binding |
| `vault.sync.local_delete_unsynced_silent` | desktop | info | `binding`, `path` | T12.2 watcher gate dropped a delete on a never-synced path |
| `vault.sync.previously_synced_check_failed` | desktop | warning | `binding`, `path` | The T12.2 predicate raised; treating as not-synced |
| `vault.sync.progress_callback_failed` | desktop | error | exception traceback | UI progress callback raised; cycle continues |
| `vault.sync.ransomware_callback_failed` | desktop | error | `binding` | F-Y27 — UI callback for trip raised |
| `vault.sync.ransomware_pause_failed` | desktop | error | `binding` | F-Y27 — pause helper threw |
| `vault.sync.ransomware_pause_triggered` | desktop | warning | `binding`, `title`, `body` | F-Y27 — detector tripped; binding paused |
| `vault.sync.ransomware_threshold_rename_ratio` | desktop | warning | `binding`, `total`, `renames`, `ratio` | T12.3 trip via rename ratio |
| `vault.sync.ransomware_threshold_total` | desktop | warning | `binding`, `total`, `window_s` | T12.3 trip via total events |
| `vault.sync.refetch_after_publish_failed` | desktop | warning | `binding` | Manifest re-fetch after our own publish failed; cycle continues |
| `vault.sync.refetch_for_next_iter_failed` | desktop | warning | `binding` | Two-way next-iter re-fetch failed |
| `vault.sync.resume_cancelled` | desktop | info | `vault`, `session`, `chunks_done`, `total` | F-Y08 — resume_upload bailed mid-chunk-loop |
| `vault.sync.resume_cancelled_pre_publish` | desktop | info | `vault`, `session` | F-Y08 — resume_upload bailed before CAS publish |
| `vault.sync.special_file_skipped` | desktop | info | `binding`, `path`, `kind` | T6.4 skipped a symlink/FIFO/socket/device |
| `vault.sync.trash_failed` | desktop | warning | `path`, `exit`, `stderr` | `gio trash` returned non-zero |
| `vault.sync.trash_fallback_unlink_failed` | desktop | error | `path`, `error` | trash fallback `unlink` also failed |
| `vault.sync.trash_invocation_failed` | desktop | error | `path`, `error` | `gio` could not be invoked at all |
| `vault.sync.trash_unavailable` | desktop | warning | `path`, `reason` | `gio` not installed; falling back to unlink |
| `vault.sync.twoway_cancelled_between_ops` | desktop | info | `binding`, `remaining` | F-Y08 — two-way Phase B bailed between ops |
| `vault.sync.twoway_cancelled_between_phases` | desktop | info | `binding` | F-Y08 — two-way bailed between Phase A and Phase B |
| `vault.sync.twoway_cancelled_pre_iteration` | desktop | info | `binding` | F-Y08 — two-way bailed before starting a new iteration |
| `vault.sync.twoway_conflict_move_failed` | desktop | warning | `binding`, `src`, `dst`, `error` | Couldn't rename local copy aside before download |
| `vault.sync.twoway_download_failed` | desktop | warning | `binding`, `path`, `error` | Two-way remote-upsert phase couldn't fetch file |
| `vault.sync.twoway_folder_no_display_name` | desktop | warning | `binding`, `folder` | Two-way phase aborted; manifest folder lacked display name |
| `vault.sync.twoway_local_fingerprint_unreadable` | desktop | warning | `binding`, `path` | F-Y04 — fingerprint failed; treated file as modified |
| `vault.sync.twoway_phase_a_cancelled` | desktop | info | `binding`, `processed` | F-Y08 — Phase A (apply remote → local) bailed mid-folder |
| `vault.sync.twoway_remote_tombstone_applied` | desktop | info | `binding`, `path` | Local file trashed after remote-tombstone applied (unmodified case) |
| `vault.sync.twoway_remote_tombstone_kept_local_modified` | desktop | info | `binding`, `path` | Local edits preserved over remote tombstone (re-upload re-enqueued) |
| `vault.sync.twoway_remote_tombstone_unreadable` | desktop | warning | `binding`, `path` | F-Y05 — fingerprint failed; deferred to next cycle |
| `vault.sync.twoway_skip_unsafe_path` | desktop | warning | `path` | Two-way refused a manifest path that traverses out of root |
| `vault.sync.twoway_trash_failed` | desktop | warning | `binding`, `path` | T12.1 remote-tombstone trash failed; row left in place |
| `vault.sync.upload_cancelled` | desktop | info | `vault`, `remote_path`, `chunks_done`, `total` | F-Y08 — upload_file bailed mid-chunk-loop |
| `vault.sync.upload_cancelled_op` | desktop | info | `binding`, `path` | F-Y08 — sync cycle translated chunk-level bail into op outcome |
| `vault.sync.upload_cancelled_pre_publish` | desktop | info | `vault`, `remote_path` | F-Y08 — upload_file bailed before CAS publish |
| `vault.sync.upload_cas_conflict` | desktop | warning | `binding`, `path` | Inner T6.3 retry budget exhausted; op stays for next cycle |
| `vault.sync.upload_failed` | desktop | warning | `binding`, `path`, `error` | Generic upload failure; op left in queue with attempts++ |
| `vault.sync.upload_path_vanished_promoted_to_delete` | desktop | info | `binding`, `path` | Watcher saw modify; sync saw missing — promoted to delete |
| `vault.sync.upload_path_vanished_silent` | desktop | info | `binding`, `path` | Upload op for never-synced path dropped |
| `vault.sync.upload_quota_exceeded` | desktop | warning | `binding`, `path`, `used`, `quota` | F-D03 — sync engine surfaced 507 to caller |
| `vault.sync.watchdog_unavailable` | desktop | warning | `reason` | python3-watchdog not installed; falling back to polling |
| `vault.sync.watcher_flush_failed` | desktop | error | `binding` | "Sync now" couldn't drain watcher events first |
| `vault.sync.watcher_runtime_boot_failed` | desktop | error | exception traceback | F-Y13 — watcher boot crashed |
| `vault.sync.watcher_runtime_init_failed` | desktop | error | exception traceback | F-Y13 — watcher init crashed |
| `vault.sync.watcher_skip_missing_root` | desktop | warning | `binding`, `path` | F-Y13 — binding root vanished |
| `vault.sync.watcher_stop_failed` | desktop | warning | `binding` | F-Y13 — observer stop raised |
| `vault.sync.watcher_tick_failed` | desktop | error | `binding` | F-Y13 — coordinator tick raised |
| `vault.sync.watchers_started` | desktop | info | `vault`, `count` | F-Y13 — boot-time watcher init |
| `vault.tray.export.notify_failed` | desktop | error | exception traceback | Tray notification for stub Export failed |
| `vault.tray.export.stub` | desktop | info | — | Tray menu Export entry placeholder pre-T8 |
| `vault.tray.import.notify_failed` | desktop | error | exception traceback | Tray notification for stub Import failed |
| `vault.tray.import.stub` | desktop | info | — | Tray menu Import entry placeholder pre-T8 |
| `vault.tray.sync_now.notify_failed` | desktop | error | exception traceback | Tray notification for stub Sync now failed |
| `vault.tray.sync_now.stub` | desktop | info | — | Tray menu Sync now entry placeholder pre-T10.6 |
| `vault.upload.completed` | desktop | info | `vault`, `revision`, `path` | A single-file upload completed (post-publish) |
| `vault.vault.cleared` | desktop | info | `total_tombstoned`, `author` | T14.2 whole-vault bulk-soft-delete published |
| `vault.vault_access_secret.encode` | desktop | info | — | Access-secret encoding helper invoked |

---

## Grep cheatsheet

```bash
# All transfer-lifecycle lines from today's session
grep -E "transfer\.(init|chunk|upload|download|delivery)\." server/data/logs/server.log

# One transfer's full path, cross-runtime
TID=2e17741b-dc32
grep "transfer_id=$TID" server/data/logs/server.log \
    ~/.config/desktop-connector/logs/desktop-connector.log \
    <(adb shell cat /data/data/com.desktopconnector/files/app.log)

# Every rate-limit fire
grep "rate_limited" server/data/logs/server.log

# Every non-info-severity line on the desktop
grep -E "\[(WARNING|ERROR)\]" ~/.config/desktop-connector/logs/desktop-connector.log
```

---

## Adding a new event

1. Pick the right category — if none fits, don't invent one without
   discussion.
2. Name it `category.subject.outcome`. Outcome from the short verb
   list above.
3. Pick the severity by "what action does an operator take?"
4. Include the correlation IDs the flow has access to, truncated to
   12 chars.
5. Confirm no sensitive field is in the message.
6. Add a row to the catalog above.
