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

### auth

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `auth.missing` | server | warning | `uri` | No X-Device-ID / Bearer headers |
| `auth.invalid` | server | warning | `device_id`, `uri` | Credentials didn't match |

### pairing

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `pairing.qr.generated` | desktop | info | `device_id` | QR material never logged |
| `pairing.qr.scanned` | android | info | — | User scanned QR; no identifiers yet |
| `pairing.request.sent` | android (new) | info | `desktop_id` | Pairing request posted |
| `pairing.request.received` | server (new) | info | `desktop_id`, `phone_id` | Row inserted in `pairing_requests` |
| `pairing.request.claimed` | server (new) | info | `desktop_id`, `count` | Desktop polled and claimed N requests |
| `pairing.confirm.accepted` | server (new), desktop, android (new) | info | `device_a`, `device_b` | Pairing row created |
| `pairing.unpair.received` | desktop, android | info | `peer_id` | `.fn.unpair` consumed |
| `pairing.unpair.sent` | desktop, android | info | `peer_id` | Local user unpaired, notifying peer |

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

### apierror

| Event | Where | Severity | Context | Notes |
|---|---|---|---|---|
| `apierror.caught` | server | warning | `status`, `uri`, `reason` | Router top-level catch; message must not echo request body |

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
