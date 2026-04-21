# Streaming improvement — relay pass-through chunks

**Status:** PLAN (not implemented). App-agnostic; the desktop and Android
clients will each land it afterward against this shared scaffolding.

## Motivation

Today the relay is a **store-then-forward** buffer: sender uploads every
chunk → then recipient downloads every chunk → then acks. End-to-end
latency for a `N`-chunk transfer is roughly `N × upload_rtt +
N × download_rtt` even on a fast link on both sides. Peak on-disk use on
the server is the full transfer size.

New model: **streaming relay**. As soon as chunk 0 lands on the server,
the recipient is notified and starts pulling. After a chunk has been
delivered (and ack'd) it is deleted on the server. The storage footprint
for a single transfer collapses to the in-flight window between the
sender's write head and the recipient's read head — typically 1–few
chunks. End-to-end time approaches `max(upload_time, download_time) +
one chunk of pipeline slack`, roughly halving the wall clock for
symmetric links and much more for slow-writer / fast-reader asymmetry.

---

## What the user asked for (summarised)

1. **Streaming delivery.** First chunk arrival wakes the recipient; it
   pulls sequentially; each delivered chunk is deleted on the server;
   remaining chunks stack FIFO.
2. **"Sending X→Y" progress on the sender** when delivery overlaps
   upload, where `X = chunks uploaded`, `Y = chunks delivered`. Falls
   back to classic **Uploading N/M → Delivering N/M** when the recipient
   is offline at init and comes online only after upload finishes.
3. **Clever chunk-level retry** on the recipient side that can recover
   from transient failures but must not drain the battery indefinitely.
   After a bounded give-up window the transfer fails and the server is
   wiped.
4. **Either side can abort** by removing the history row. The other
   side's row shows **"Aborted"** in orange and the relay is wiped.
5. **Quota handling splits two paths:**
   - Target offline at init → same rules as today: oversized file →
     terminal 413; queue-full → transient 507, classic WAITING.
   - Target online at init → allow init even if the full projected size
     would not fit, but the **next chunk** must wait for space to free
     up if the quota is full. While waiting, the sender row turns yellow
     **"Waiting X→Y"**. Standard waiting window still applies; on expiry
     the transfer fails.

---

## Gaps I flagged in the spec (and my proposed resolutions — feedback welcome)

The user asked to be told what might be missing. Each of these is a
decision point; the rest of the plan assumes the resolution given here.

1. **"Chunk deleted after successful download" — when exactly?**
   If the server deletes the byte blob when it finishes streaming the
   response, a network loss after `write()` but before the recipient
   persists the plaintext can lose the chunk forever. **Proposed:** the
   server keeps the chunk until the recipient ACKs that index. Per-chunk
   ACK replaces the single transfer-level ACK. Storage is still FIFO;
   the extra latency is one short round-trip that overlaps the next
   chunk's GET anyway.

2. **"First chunk arrives → target is notified" — by what mechanism?**
   Today FCM wake fires on `upload.completed`. **Proposed:** fire a new
   wake event `transfer.stream.ready` on the first stored chunk, exactly
   once per transfer. Long-poll `/api/transfers/notify` also surfaces
   the transfer as soon as chunk 0 is stored (not when all chunks are
   stored). Clients without FCM still converge via long poll.

3. **"Target tries next chunk; if not ready, tries again once ready."**
   Needs a distinct "not yet uploaded" response separate from 404
   "gone / unknown". **Proposed:** `GET /chunks/{i}` returns **425 Too
   Early** with `Retry-After` header when the transfer exists and is not
   aborted but chunk `i` has not been stored yet. Recipient treats 425
   as "wait and retry" with short backoff (see retry policy below). 404
   stays reserved for genuinely unknown / wiped transfers — terminal.

4. **"Retry cleverly but don't drain battery."** Two retry budgets are
   needed, not one: transient-polling retry (waiting on upstream
   producer) and error retry (real network failure). Concrete numbers
   in §5 below.

5. **Abort propagation to the other side.** Today only the sender can
   `DELETE /api/transfers/{id}`. **Proposed:** extend
   `DELETE /api/transfers/{id}` to accept either the sender or the
   recipient. The server marks the transfer `aborted` (new terminal
   state), wipes chunks, and the other party sees `aborted` on its next
   poll / long-poll / chunk fetch. Clients render the row as
   **"Aborted"** (orange, terminal).

6. **Upload failure mid-stream.** If the sender gives up on chunk K
   after exhausting its upload window, the recipient already has 0..K-1.
   **Proposed:** sender-initiated failure walks the same path as abort:
   `DELETE /api/transfers/{id}` with `reason=sender_failed`. Recipient
   sees `aborted` with sender-side reason and shows **"Aborted"** too
   (no "Failed" split — from the recipient's view the cause is the same:
   upstream went away). Sender's own row shows **"Failed"** with the
   chunk-index detail as today.

7. **Target goes offline mid-stream after a successful start.**
   **Resolved:** sender continues uploading until chunks stack up to
   the quota wall (recipient isn't draining), then flips to yellow
   **"Waiting X→Y"**. Standard 30-min waiting window applies; on expiry
   the transfer is a true failure — sender DELETEs with
   `reason=sender_failed`, local row shows **"Failed: quota_timeout"**,
   recipient side (when it eventually comes back) sees **"Aborted"**.
   Same UI path as any other backpressure-then-timeout failure.

8. **Recipient crash / restart mid-stream.** Existing `pending` list
   already covers discovery on restart; with streaming, a restart after
   chunks 0..K were ack'd (and wiped) should continue from K+1 cleanly.
   Server-held state is the source of truth — it already knows which
   index the recipient is at (see §3 schema).

9. **Ordering.** User said "sequentially next", so strict in-order. That
   is the recipient's responsibility; the server stores and serves any
   index that exists. The sender is free to upload chunks in parallel —
   the server's ACK-deletes-chunk model still works (the recipient just
   skips past any out-of-order GET by sticking to sequential indices).

10. **Backward compatibility + operator kill-switch.** Old clients +
    new server must keep working; and new clients against old servers
    must not corrupt data. **Proposed:** advertise `stream_v1` in
    `/api/health` capabilities; init request carries
    `mode: "classic" | "streaming"`. Server defaults to classic if
    `mode` is missing. New clients check the capability before
    requesting streaming. If absent, fall back to classic. Both modes
    coexist in the code; classic is untouched.
    Also add a **server-side config knob** `streamingEnabled` (bool,
    default `true`) in `server/data/config.json` — when `false`, the
    server drops `stream_v1` from `/api/health` capabilities AND
    always returns `negotiated_mode: "classic"` from init, so an
    operator can force classic behaviour fleet-wide without a code
    change. Same self-healing `Config::*` pattern as `storageQuotaMB`.

11. **"Target online at init"** — how is online defined? **Proposed:**
    server checks `last_seen_at >= now - 15 s` for the recipient. The
    sender does not decide — it just asks for `mode: "streaming"` and
    the server replies with `negotiated_mode: "streaming" | "classic"`
    in the init response, downgrading when the recipient is clearly
    offline so the sender doesn't waste time on a stream that can't
    flow. (The sender's own ping cache, which is already 30 s, is not
    authoritative enough for this decision.)

12. **Maximum number of concurrent streaming transfers per recipient.**
    Not mentioned, but the FIFO invariant relies on the recipient
    actually draining. **Proposed:** no hard cap initially; the quota
    wall self-limits concurrency. Add a cap later if we see thrash.

13. **Concurrency note (localhost dev only).** The PHP *built-in*
    server (`php -S`) is single-threaded, which matters only for
    `test_loop.sh` and hand-testing. Real deployments (Apache +
    mod_php, nginx + php-fpm) handle concurrent requests fine, so
    streaming upload + concurrent streaming download against the same
    recipient is safe in production. The dev-server limitation is
    already documented; no new note needed. For streaming-specific
    concurrency validation, test against the real remote server.

---

## Architecture overview

### End-to-end flow (streaming mode)

```
SENDER                    SERVER                    RECIPIENT
──────                    ──────                    ─────────
init(mode=streaming)  ──► negotiate → mode=streaming
                          (recipient online)
                      ◄── {transfer_id, negotiated_mode:"streaming"}

upload chunk 0        ──► stored                ─► FCM wake {type:"stream_ready"}
                                                ─► /notify returns transfer
upload chunk 1        ──►                       ◄── GET chunk 0
                                                ─── streamed back
                                                ─► decrypt + write .part
                                                ─► ACK chunk 0
                          delete chunk 0 blob
upload chunk 2        ──►                       ◄── GET chunk 1
                                                ...
upload chunk N-1      ──►
                                                ◄── GET chunk N-1
                                                ─► rename .part → final
                                                ─► ACK chunk N-1
                          delete chunk N-1 blob
                          mark transfer delivered
                      ◄── sent-status: delivered   (sender sees "Delivered")
```

### Classic mode (recipient offline at init)

Unchanged from today. `init`, `chunks/*` upload, `chunks/*` download,
single transfer-level `ack`. Chunks live on the server from upload
completion until recipient ack. Quota logic unchanged.

### Mode selection (server-side at init)

```
negotiated_mode =
  classic   if client sent mode=classic OR client omitted mode (old client)
  classic   if recipient.last_seen_at < now - 15s
  classic   if caller is not streaming-capable (no stream_v1 in capabilities exchange)
  streaming otherwise
```

Sender receives `negotiated_mode` in the init response and drives the
right state machine.

---

## 1. Server protocol changes

### 1.1 New / changed endpoints

| Method | Path | Change |
|---|---|---|
| GET | `/api/health` | Add `"capabilities": ["stream_v1", ...]` to response |
| POST | `/api/transfers/init` | Accept `mode: "classic" \| "streaming"` (default `"classic"` for old clients). Return `negotiated_mode`. |
| POST | `/api/transfers/{id}/chunks/{i}` | Behaviour change (streaming only): return **507** transiently when the quota is full and *this* chunk doesn't fit. Sender should retry with backoff. Classic transfers keep current quota semantics. |
| GET | `/api/transfers/{id}/chunks/{i}` | New transient response **425 Too Early** with `Retry-After` when chunk not yet uploaded and transfer is still streaming. 404 stays for unknown / wiped. |
| POST | `/api/transfers/{id}/chunks/{i}/ack` | **New.** Per-chunk ack. Deletes the chunk blob + row. Idempotent. Streaming-only; classic ignores it. |
| POST | `/api/transfers/{id}/ack` | Unchanged semantics for classic. For streaming, becomes an alias for "ack all remaining chunks" (last-resort terminator for a recipient that wants to drop out cleanly). |
| DELETE | `/api/transfers/{id}` | Accept both sender AND recipient as caller. Optional body `{reason: "recipient_abort" \| "sender_abort" \| "sender_failed"}`. Marks transfer `aborted`, wipes chunks. |
| GET | `/api/transfers/sent-status` | Extend row with `chunks_uploaded` (distinct from `chunks_downloaded`), `negotiated_mode`, and a `state` enum that includes `aborted`. |
| GET | `/api/transfers/notify` | Include streaming transfers as soon as chunk 0 is stored (not on upload.completed). Inline payload gets the same new fields. |

### 1.2 FCM payloads

Two new `type` values (the opaque envelope keeps today's "no content
leaked" rule):

- `type: "stream_ready"` — fired once per streaming transfer when the
  first chunk is stored.
- `type: "abort"` — fired on the opposite party when `DELETE` lands.
  Also reclaims idle long-polls so the aborter doesn't wait for the
  25 s tick.

### 1.3 Error envelopes

Reuse existing `ApiError` subclasses; add:

- `TooEarlyError` (425) — "Chunk not yet uploaded; retry after N
  seconds". Has a typed `retry_after_ms` hint.
- `AbortedError` (410 Gone) — "Transfer has been aborted". Returned to
  whichever side polls / GETs / ACKs after the other side aborted.

### 1.4 Compat classification

- **Preserving** for old clients: `mode` defaults to `classic`; classic
  path unchanged byte-for-byte; new endpoints are additive.
- **Additive** for new clients: streaming path requires `stream_v1`
  capability; falls back cleanly if the server is old.

Update `docs/protocol.compatibility.md` and `docs/protocol.examples.md`
accordingly.

---

## 2. Server schema changes

Migration `002_streaming.sql`:

```sql
ALTER TABLE transfers ADD COLUMN mode TEXT NOT NULL DEFAULT 'classic';
                                           -- 'classic' | 'streaming'
ALTER TABLE transfers ADD COLUMN aborted INTEGER NOT NULL DEFAULT 0;
                                           -- 1 after DELETE by either party
ALTER TABLE transfers ADD COLUMN abort_reason TEXT;
                                           -- 'recipient_abort' | 'sender_abort' | 'sender_failed'
ALTER TABLE transfers ADD COLUMN aborted_at INTEGER;
ALTER TABLE transfers ADD COLUMN stream_ready_at INTEGER;
                                           -- monotonic stamp when first chunk stored
ALTER TABLE transfers ADD COLUMN last_served_at INTEGER;
                                           -- stall-detection reference

-- Rename clarification: keep the historical column name `chunks_downloaded`
-- so we don't break any external tooling; document that for streaming mode
-- it still means "highest-index + 1 that the recipient has ACK'd".
-- Add a separate counter for upload progress visibility:
ALTER TABLE transfers ADD COLUMN chunks_uploaded INTEGER NOT NULL DEFAULT 0;
                                           -- streaming only; matches chunks_received
                                           -- for the sender's "Sending X→Y" label.

CREATE INDEX IF NOT EXISTS idx_transfers_aborted ON transfers(aborted);
```

### Invariants (updated)

- **Classic mode (unchanged):** `chunks_downloaded == chunk_count ⇔
  downloaded == 1 ⇔ delivery_state == "delivered"`. Chunks live on
  disk from upload-store to transfer-ack.
- **Streaming mode (new):** 
  - `0 ≤ chunks_downloaded ≤ chunks_uploaded ≤ chunk_count`
  - After chunk `i` ACK: `chunk blob i deleted AND chunks_downloaded
    becomes max(chunks_downloaded, i+1)`.
  - `chunks_downloaded == chunk_count ⇔ downloaded == 1 ⇔ delivered`.
  - `aborted == 1` is terminal. `complete`, `downloaded`, future ACKs
    are frozen. `GET/POST/DELETE` all return 410.
- **Bytes-on-disk at any moment** for a streaming transfer
  `≤ (chunks_uploaded - chunks_downloaded) × CHUNK_SIZE`.

### Repository / service surface

New methods (keeping the repo-owns-SQL rule from `CLAUDE.md`):

- `TransferRepository::markStreamReady(id, ts)`
- `TransferRepository::markAborted(id, reason, ts)`
- `TransferRepository::incrementChunksUploaded(id)` (splits from
  `incrementChunksReceived`, though they move in lockstep for streaming)
- `TransferRepository::isAborted(id)`
- `ChunkRepository::deleteChunkByIndex(transfer_id, index)` — used by
  per-chunk ACK
- `TransferRepository::bytesOnDiskForTransfer(id)` — for the in-stream
  quota check

New service calls:

- `TransferService::ackChunk(db, deviceId, transferId, chunkIndex)`:
  asserts recipient, asserts chunk exists, deletes chunk row + blob
  file, updates `chunks_downloaded` (idempotent with `MAX(old, i+1)`),
  flips `downloaded=1` if the final chunk was acked.
- `TransferService::abort(db, callerId, transferId, reason)`: replaces
  the current sender-only `cancel`. Either party can call.
- `TransferService::init` gains a `mode` parameter; streaming init
  **skips the projected-size quota check** against `chunk_count` and
  relies on the runtime chunk-upload quota gate instead. Classic init
  is unchanged.
- `TransferService::uploadChunk`: in streaming mode, re-checks quota at
  each chunk: `bytesOnDiskForTransfer(recipient)+len(blob)` vs
  `Config::storageQuotaBytes()` — throws `StorageLimitError` if full.
  Aborted transfer → `AbortedError`.
- `TransferService::downloadChunk`: returns `TooEarlyError` when chunk
  missing but transfer neither complete-in-classic nor aborted nor
  fully-wiped-in-streaming. Bumps `last_served_at`.
- `TransferStatusService::computeStatus` extended with `aborted` and
  the streaming sub-state `sending` (chunks uploading AND some already
  downloaded).
- `TransferWakeService::wake` splits into `wakeTransferComplete` (classic
  path) and `wakeStreamReady` (streaming path, fires on first chunk).
- `TransferCleanupService`: unify sender-cancel / recipient-abort
  cleanup paths.

---

## 3. Client-side state machine

State names are protocol-level; each client maps them to its own UI
string table.

### 3.1 Sender-side state machine

```
           ┌─────────────┐
           │  preparing  │  (encrypt metadata, init)
           └──────┬──────┘
                  │ init OK; negotiated_mode = ?
          ┌───────┴────────┐
classic → │                │ ← streaming
          ▼                ▼
   ┌────────────┐   ┌──────────────┐
   │  uploading │   │   sending    │ label:"Sending X→Y"  (X=uploaded,Y=delivered)
   │    N/M     │   │              │
   └─────┬──────┘   └──┬────────┬──┘
         │ all chunks  │ chunk  │ quota 507
         │ uploaded    │ N→N+1  │
         ▼             ▼        ▼
   ┌────────────┐  (stay in   ┌─────────────────┐
   │ delivering │   sending)  │ waiting_stream  │ label:"Waiting X→Y" yellow
   │    N/M     │              │                 │
   └─────┬──────┘              └────┬────────────┘
         │                          │ space freed → back to sending
         │                          │ quota window expired → failed
         ▼                          ▼
   ┌────────────┐             (ends same way)
   │ delivered  │
   └────────────┘

From any state: `DELETE /api/transfers/{id}` observed OR own abort →
  aborted (local row shows "Aborted", orange)
Sender gave up on a chunk → send DELETE reason=sender_failed → row:
  "Failed" (local, with chunk index)
```

Key rules for the sender:

- **Switch from `uploading` to `sending`** the first time a
  sent-status poll (or a long-poll inline payload) reports
  `chunks_downloaded > 0` for this transfer. After that, the label is
  `Sending X→Y` until `X == Y == chunk_count`.
- **Waiting state** on chunk upload: receive 507 → set UI to yellow
  `Waiting X→Y`, start the standard waiting timer (`STORAGE_FULL_MAX_WINDOW_S`,
  30 min default), retry uploads with exponential backoff (same curve
  as today). On success → drop back to `sending`. On timeout → DELETE
  the transfer with `reason=sender_failed`, flip row to `failed`.
- **Abort on recipient side**: a chunk upload returning `410 Gone`
  (`AbortedError`) flips the row to `aborted`. Stop uploading.
- **Delivery stall safeguard** (existing 2-min no-chunks-downloaded-
  advancement give-up) still applies but in streaming mode it ONLY
  stops the tracker; it does NOT abort the transfer — the sender
  continues uploading until quota gates it. Matches existing semantic.

### 3.2 Recipient-side state machine

```
wake (FCM stream_ready OR long-poll) → pending list returns transfer
                                       │
                                       ▼
                               ┌───────────────┐
                               │ downloading   │  label:"Downloading X/Y"
                               │  (streaming)  │    X=chunks acked
                               └───────┬───────┘
                                       │
                       GET chunk i ── 200 ──┐
                       GET chunk i ── 425 ──┤ retry w/ Retry-After (see §5)
                       GET chunk i ── 410 ──┤ aborted by sender / sender_failed
                                       │    │
                       decrypt + write .part│
                       POST ack chunk i     │
                                       │    ▼
                       i < N-1 ? ─ next ─── aborted → clean up .part, show "Aborted"
                       i == N-1 ? ─ rename → delivered
                                            
On local removal of history row → send DELETE reason=recipient_abort
                                  then clean up .part, show "Aborted".
On fatal download error (after retry budget) → DELETE reason=recipient_abort
                                                then show "Failed".
```

### 3.3 Progress field shape (applies to both clients)

Sender-side row in local history:

- `status ∈ {preparing, uploading, sending, waiting_stream, delivering,
  delivered, failed, aborted}`
- `chunks_uploaded` (new field; replaces the overloaded
  `chunks_downloaded` for sender's "Sending X" numerator)
- `chunks_delivered` (reuses today's `recipient_chunks_downloaded` /
  `deliveryChunks` from the three-phase design)
- `chunk_count`
- `mode ∈ {classic, streaming}`
- `abort_reason` when applicable

Recipient-side row:

- `status ∈ {downloading, complete, failed, aborted}`
- `chunks_downloaded_local` (how many the recipient has ack'd)
- `chunk_count`

Clients should label their UI as:

| Status | Label | Color |
|---|---|---|
| uploading | `Uploading X/N` | amber / yellow (current) |
| sending | `Sending X→Y` | same blue as delivering today |
| waiting_stream | `Waiting X→Y` | yellow, same as current WAITING |
| delivering | `Delivering X/N` | blue (current) |
| delivered | `Delivered` | success blue (current) |
| downloading (recipient) | `Downloading X/N` | same as current |
| failed | `Failed: <reason>` | orange |
| aborted | `Aborted` | orange |

---

## 4. Quota handling (concrete rules)

Two code paths, both already partially in place:

### 4.1 `init`

```
if mode == classic:
    # unchanged from today
    if chunk_count * PROJECTED_CHUNK_SIZE > quota_bytes: 413
    if reserved_projected + chunk_count * PROJECTED_CHUNK_SIZE > quota_bytes: 507
elif mode == streaming:
    # target is online by definition (we negotiated to streaming)
    # still refuse terminally-too-big files so the sender doesn't
    # waste its bandwidth for something that can never fit one chunk
    if chunk_count * PROJECTED_CHUNK_SIZE > quota_bytes AND
       quota_bytes < PROJECTED_CHUNK_SIZE:
        413  # pathological server config
    # otherwise: allow. Per-chunk upload enforces the real gate.
```

Practically: 413 only fires on truly absurd cases for streaming
(server quota smaller than one chunk). The real gate moved to upload
time.

### 4.2 `uploadChunk` (streaming only)

```
if aborted: 410 AbortedError
current_bytes_on_disk_across_all_transfers_to_recipient
    + len(blob)
    > quota_bytes
        → 507 StorageLimitError   (retry-after hint = 2s)
```

Sender treats 507 exactly like the existing WAITING flow: yellow UI,
exponential backoff, 30-min window, then fail.

### 4.3 Why quota_bytes is now "live bytes on disk" for streaming

In classic mode the server had to project forward because chunks were
never deleted mid-transfer. In streaming mode chunks are deleted on ack,
so measuring the actual on-disk byte count across the recipient's
outstanding chunks is tight and honest. Projected-size reservation is
unnecessary and in fact harmful — it would block a legitimate streaming
transfer whose peak on-disk footprint is just 1–2 chunks.

Classic reservations (`reserved_projected`) still apply for
coexisting classic transfers to the same recipient — they share the
same quota bucket.

---

## 5. Retry policies (concrete numbers)

Pick conservative numbers to start; tune after testing. All in the
**protocol docs**, not hardcoded into one client.

### 5.1 Recipient, waiting on chunk to appear (HTTP 425)

- Honour server's `Retry-After` header; default 1 s.
- Own backoff ramp: 1 s → 2 s → 4 s → 8 s → **cap 10 s**.
- Per-chunk "chunk hasn't appeared" budget: **5 min of continuous 425s**
  without any other signal → treat as upstream dead → recipient aborts
  the transfer with `reason=recipient_abort` and surfaces **"Failed"**
  locally. This is distinct from the stall safeguard on the sender side
  — the recipient's budget is about "server has nothing new for me at
  all", not about "sender is slow".
- When the screen is off / app in doze (mobile), the OS already throttles
  these polls; don't layer extra backoff. FCM `stream_ready` keeps the
  pipeline warm.

### 5.2 Recipient, real network failure (non-425)

- 3 attempts with 2 s × attempt backoff (matches today's
  `downloadChunkWithRetry`).
- On exhaustion → abort with `reason=recipient_abort`, UI
  **"Failed: download error at chunk i"**.

### 5.3 Sender, chunk upload failure

- Matches today: retry every 5 s; after 120 s continuous failure on the
  same chunk, abort with `reason=sender_failed`, UI
  **"Failed: chunk i/N"**.

### 5.4 Sender, 507 mid-stream quota

- Retry with exponential backoff: 2 s → 4 s → 8 s → 16 s → **cap 30 s**.
- Total window: `STORAGE_FULL_MAX_WINDOW_S` (30 min; already defined
  client-side) → on expiry, abort with `reason=sender_failed`, UI
  **"Failed: quota_timeout"** (matches today's existing code path).
- UI label while retrying: yellow **"Waiting X→Y"**.

### 5.5 Sender, delivery tracker stall

- Unchanged: existing 2-min no-advancement window puts the sender's
  per-transfer tracker into `gaveUp` / clears its delivery progress
  fields so the UI falls back to `Sending X→?` or `Sent` (classic).
- The transfer row does NOT fail from this alone. It fails only when
  the sender itself gives up on an upload (5.3 / 5.4) or when an
  abort signal arrives from the recipient.

---

## 6. UI rendering rules (both clients)

These are the only visible changes the user sees:

1. **Sender row during streaming**:
   - Before any `chunks_downloaded > 0`: **"Uploading X/N"** (amber).
     This handles the brief moment after init where chunk 0 is uploading
     but the recipient hasn't fetched it yet.
   - First delivery observed: flip to **"Sending X→Y"** (blue). Keeps
     two counters; both bars visible OR a single bar showing `Y/N`
     progress with `X/N` as a secondary tick mark. Leave the exact
     widget to the client; the data is `{X, Y, N}`.
   - 507 received mid-stream: flip to yellow **"Waiting X→Y"** until
     next successful upload.
   - Transfer wiped (all chunks delivered AND ack'd): **"Delivered"**.

2. **Sender row during classic** (unchanged): **"Uploading N/M"** → 
   **"Delivering N/M"** → **"Delivered"**.

3. **Recipient row** (new or classic — same UI): **"Downloading X/N"**
   with per-chunk progress, as today.

4. **Aborted** (either side): **"Aborted"** (orange, terminal, no spinner,
   no progress bar).

5. **Failed**: **"Failed: <reason>"** (orange). Local to each side; the
   two sides can see different strings ("Failed: chunk 7/20" on sender,
   "Aborted" on recipient who saw the sender drop out).

6. **History-row swipe/delete while streaming**: this is the "abort"
   gesture. Clients should confirm via existing confirm-dialog pattern
   and then fire `DELETE`. The row stays visible in history marked
   **"Aborted"** (does NOT disappear entirely) so the user sees
   confirmation.

---

## 7. Diagnostic logging — new event names

Add to `docs/diagnostics.events.md`:

- `transfer.init.negotiated_streaming` — includes `recipient_online=bool`
- `transfer.stream.ready` — first chunk stored, wake fired
- `transfer.chunk.served_and_pending_ack` — chunk served, waiting ack
- `transfer.chunk.acked_and_deleted` — chunk ack'd, blob removed
- `transfer.stream.waiting_quota` — quota full on chunk upload
- `transfer.stream.stalled_no_advance` — tracker 2-min stall
- `transfer.abort.sender` / `transfer.abort.recipient` /
  `transfer.abort.sender_failed` — DELETE calls with each reason
- `transfer.chunk.too_early` — recipient got 425 (debug level only,
  don't spam at info)
- `apierror.caught` already covers the 410/425 return codes centrally;
  no per-throw instrumentation needed.

Privacy rule still applies: log `transfer_id` (truncated 12 chars),
chunk indices, byte counts; never log blob bytes, keys, filenames,
or FCM payload contents.

---

## 8. Rollout plan

Phased so we always have a green `test_loop.sh`:

### Phase A — server (additive, no client change)

1. **Migration 002** — schema columns added, no behavioural change.
2. **`mode` parameter accepted at init** (default classic) — server
   routes everything through classic path for now. Advertises
   `stream_v1` in `/api/health`.
3. **New endpoints wired through** (`/chunks/{i}/ack`, `DELETE` by
   recipient, 425/410 error envelopes) — classic transfers ignore the
   per-chunk ACK endpoint (transfer-level ack still works).
4. **Server switches to streaming path when `mode=streaming`** —
   streaming path enforces per-chunk quota, deletes chunks on ACK,
   fires `stream_ready` wake on first chunk. Classic path untouched.

### Phase B — protocol tests

5. Extend `tests/protocol/test_server_contract.py` to cover:
   - init with `mode=streaming`, online recipient → `negotiated_mode=streaming`
   - init with `mode=streaming`, offline recipient → `negotiated_mode=classic`
   - GET chunk that hasn't uploaded → 425 + Retry-After
   - DELETE by recipient → 200 for recipient, subsequent GETs → 410
   - Per-chunk ack deletes the blob file
   - 507 on mid-stream chunk upload when quota is full
6. Update `docs/protocol.compatibility.md` and `docs/protocol.examples.md`.

### Phase C — desktop client (Python)

7. Plumb the new fields through `api_client`, `poller`, `history`,
   `windows`.
8. Implement sender streaming state machine.
9. Implement recipient streaming receive loop (425 polling, per-chunk
   ack, abort handling).
10. Add UI labels and yellow/orange rendering for `sending`,
    `waiting_stream`, `aborted`.
11. Desktop integration test: large file, slow-drain recipient,
    verify peak server bytes ≤ a few chunks.

### Phase D — Android client (Kotlin)

12. Same scope as C on the Android side. Reuse the shared message model
    where possible; new `TransferState` enum values.

### Phase E — integration + cleanup

13. Update `test_loop.sh` to exercise both modes.
14. Concurrent streaming test (two transfers overlapping to the same
    recipient) — validates the quota wall and FIFO drain.
15. Power-loss / mid-stream crash test on both clients — verify the
    `.part` sweep + abort signaling still converge.
16. Load test note in `CLAUDE.md` about PHP built-in server / switch to
    php-fpm for any real production use of streaming.

Each phase ends on a green `test_loop.sh`. No phase requires the
next one to also land (old client + new server and new client + old
server both keep working by falling back to classic).

---

## 9. Explicit non-goals

- **Random-access / out-of-order receive.** The user said "sequentially
  next". Keep it strict. Future work could allow parallel chunk
  downloads for bandwidth reasons, but not now.
- **Resumable transfers across app restarts in streaming mode.** Nice
  to have; the server already has `chunks_downloaded` which resumes
  naturally, but we're not building a formal resume UI. If the app
  restarts mid-stream, the `/pending` list will still show the
  transfer and the recipient continues from the next un-ack'd index.
- **Chunk-parallelism on the sender side.** Sender uploads sequentially;
  any perf gain from multi-chunk upload is not in scope.
- **Streaming for `.fn.*` command transfers.** They're tiny (usually
  single-chunk commands like clipboard push, unpair), so streaming has
  nothing to pipeline and just adds per-chunk ACK round-trips plus a
  `stream_ready` FCM wake that fires milliseconds before the only
  chunk is fetched. Zero latency win, extra round-trips, more code
  paths. Client guard: if `filename.startswith(".fn.")`, force
  `mode=classic` regardless of server capability.
- **A new "connecting / negotiating" status.** The preparing → uploading
  transition is already fast; adding a micro-state adds UI flicker with
  no real benefit.

---

## 10. Risks

1. **Asymmetric 425 / quota budgets (resolved).** Recipient's per-chunk
   "upstream has nothing" budget is **5 min**; sender's quota-waiting
   window is **30 min**. This is intentional asymmetry: 5 min covers
   normal network blips and small quota-pressure episodes on the
   recipient side, while 30 min lets the sender survive longer
   backpressure bursts. If the sender is still stuck after the
   recipient's 5 min window (meaning chunks literally stopped arriving
   for 5 straight minutes), the recipient aborts; the sender then sees
   a recipient-side abort on its next chunk upload (`410 Gone`) and
   flips to `failed`. This is the desired behaviour — the recipient
   should not sit idle on a dead stream just because the sender
   theoretically *might* recover within 30 min.

2. **Per-chunk ACK cost.** N small POSTs replace 1 POST + delete batch
   at end. For a 500-chunk transfer on mobile, that's 500 extra POSTs
   — fine on WiFi, possibly noisy on cellular. Mitigation: ack can
   piggyback on the next GET (ack in query param on `GET chunk i+1`).
   Leave as a follow-up; first pass uses a dedicated ack endpoint for
   clarity.

3. **`chunks_downloaded` field name overload.** Historical name already
   meant recipient progress on sender's side. Keep it; add
   `chunks_uploaded` as the new field. Document the name in the schema
   comment. No rename — avoids breaking logs and external tooling.

4. **Abort race.** Sender and recipient both hit DELETE within the
   same tick. Server uses row-level state machine: first DELETE wins,
   second returns 410. Cleanup is idempotent. Low risk.

5. **FCM `stream_ready` fires but recipient wakes with stale auth.**
   Auth recovery already handles this — 401/403 streak flips the
   re-pair banner. No new work needed.

6. **WAITING/WAITING-STREAM state confusion.** Two distinct flows:
   - Classic WAITING = init returned 507, no chunks uploaded yet.
   - Streaming WAITING = chunk upload returned 507 mid-stream.
   Both render as yellow with a waiting label, but the counters differ
   (`N/N unknown` vs `X→Y`). Clients must not conflate them in the
   state machine.

7. **Existing three-phase fields name mismatch between Android and
   desktop.** Android uses `chunksUploaded / totalChunks /
   deliveryChunks / deliveryTotal`; desktop uses
   `chunks_downloaded / chunks_total / recipient_chunks_downloaded /
   recipient_chunks_total`. New streaming state adds `chunks_uploaded`
   on the **sender** side (distinct from existing overloaded naming).
   Take the opportunity to align names — but do it as a separate
   refactor commit per-client, *not* bundled into this plan, so this
   plan stays purely additive.

---

## 11. Summary of added files / changed surface

**Server** (Phase A):
- `server/migrations/002_streaming.sql` — new
- `server/src/Http/ApiError.php` — add `TooEarlyError`, `AbortedError`
- `server/src/Services/TransferService.php` — `init` (mode param),
  `uploadChunk` (streaming branch + quota-on-chunk), `downloadChunk`
  (425), `ackChunk` (new), `abort` (supersedes `cancel`)
- `server/src/Services/TransferStatusService.php` — emit `aborted`,
  `sending` sub-state, new fields
- `server/src/Services/TransferWakeService.php` — `wakeStreamReady`
- `server/src/Services/TransferCleanupService.php` — unified wipe
- `server/src/Repositories/TransferRepository.php` — new methods
- `server/src/Repositories/ChunkRepository.php` — `deleteChunkByIndex`
- `server/src/Controllers/TransferController.php` — wire new routes
- `server/src/Router.php` — route `POST /chunks/{i}/ack`
- `server/public/index.php` — front-controller registration

**Docs**:
- `docs/diagnostics.events.md` — new event names (§7)
- `docs/protocol.compatibility.md` — stream_v1 classification
- `docs/protocol.examples.md` — streaming request/response examples

**Tests**:
- `tests/protocol/test_server_contract.py` — §B coverage

**Clients**: specced here, implementation tracked in per-client plans
that will live at `docs/plans/desktop-streaming-relay-plan.md` and
`docs/plans/android-streaming-relay-plan.md` once the server side is
green.
