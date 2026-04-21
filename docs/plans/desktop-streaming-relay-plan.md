# Phase C — Desktop streaming client

Companion to `docs/plans/streaming-improvement.md`. Server Phase A + B
are landed and deployed at
`https://hawwwran.com/SERVICES/desktop-connector/`. Classic transfers
still work byte-for-byte. This plan turns the Python desktop client
into a streaming-capable sender AND recipient.

Correctness over speed. Each sub-phase ends on a green `test_loop.sh`
and is independently landable. Old client (classic) + new server keeps
working; new client (streaming) + old server falls back to classic via
the existing `/api/health` capability probe.

---

## Where today's code is (relevant entry points)

- `desktop/src/api_client.py`
  - `init_transfer(...)` → `"ok" | "storage_full" | "too_large" | "failed"`
  - `upload_chunk(...)` → `{chunks_received, complete} | None`
  - `download_chunk(...)` → raw bytes or None
  - `ack_transfer(...)` → bool (transfer-level)
  - `cancel_transfer(...)` → bool (DELETE; sender-only today)
  - `send_file(...)` drives the whole upload pipeline with
    `_init_transfer_with_retry` + `_upload_chunk_with_retry` and takes
    a single `on_progress(transfer_id, uploaded, total_chunks)`
    callback, with sentinel values `0` / `-1` (WAITING) / `-2` (413).
- `desktop/src/poller.py`
  - `_download_transfer` picks the branch between `.fn` and a normal
    file based on the filename after decrypting metadata.
  - `_receive_file_transfer` streams chunks to
    `{save_dir}/.parts/.incoming_<tid>.part`, fsyncs, `os.link`s to the
    final name, then sends one `ack_transfer`.
  - `_delivery_tracker_loop` paints `recipient_chunks_downloaded` /
    `recipient_chunks_total` on outgoing rows.
- `desktop/src/history.py`
  - JSON file, fcntl-serialised. Known fields today:
    `status ∈ {uploading, downloading, complete, failed, waiting}`,
    `chunks_downloaded`, `chunks_total`,
    `recipient_chunks_downloaded`, `recipient_chunks_total`,
    `delivered`, `failure_reason`, `waiting_started_at`.
  - `get_undelivered_transfer_ids()` excludes `status == "failed"`.
- `desktop/src/windows.py`
  - Send-files window's `upload_progress` callback consumes the
    sentinel protocol above and writes history.
  - History window's `_compute_status` renders the status-text +
    progress-bar pair. WAITING is yellow `#FDD00C`, failure is
    `DC_ORANGE_700`. The history-row DELETE path already calls
    `api.cancel_transfer(tid)` for in-flight sent rows.
- `desktop/src/runners/send_runner.py`
  - One-shot `--send` mode: same callback shape as windows.py.
- `desktop/src/connection.py`
  - `ConnectionManager.request()` returns the raw `Response`, keeps
    4xx out of the backoff state machine, only 5xx and RequestException
    cause `_on_failure()`. 425 is 4xx so backoff is already correct.

---

## Non-goals (carried from streaming-improvement.md §9)

- No chunk-parallel upload. Sequential send, sequential receive.
- No streaming for `.fn.*` transfers — guard `filename.startswith(".fn.")`
  and force `mode=classic`.
- No "connecting/negotiating" UI micro-state.
- No formal resume UI across app restarts (server-side
  `chunks_downloaded` already gives us natural resume on restart).

---

## Sub-phase plan

Each sub-phase has a **what changes**, **why in this order**, and
**acceptance criteria**. Everything below is desktop-only — Phase D
(Android) is a separate plan once C is landed.

### C.1 — Protocol plumbing in `api_client.py` (pure additive)

**What changes:**

1. Introduce a `ChunkUploadResult` / `ChunkDownloadResult` enum (or
   plain constants) so callers can distinguish the new statuses:
   `ok | storage_full | aborted | too_early | network_error |
   auth_error | server_error`. No behaviour change yet — classic
   callers still treat everything that's not `ok` as retry-or-fail.
2. `init_transfer()` gains a `mode: str = "classic"` parameter and
   returns `(outcome, negotiated_mode)` where `negotiated_mode ∈
   {"classic", "streaming"}` (always `"classic"` when we pass
   `mode="classic"`; we only start passing `"streaming"` in C.4).
3. New `ack_chunk(transfer_id, chunk_index) -> bool`.
4. New `abort_transfer(transfer_id, reason: str) -> bool` —
   POST-body-less DELETE `{reason: "..."}` body. Keep
   `cancel_transfer` as a thin back-compat wrapper that calls
   `abort_transfer(tid, "sender_abort")`.
5. New `get_capabilities() -> set[str]` — unauthenticated probe of
   `/api/health`, cached with a short TTL (e.g. 60 s) on the
   `ApiClient` instance. Result set may contain `"stream_v1"`.
6. Thread 425 + 410 through `upload_chunk` / `download_chunk` so the
   body + `Retry-After` header are surfaced to callers when present.

**Why first:** pure additive surface, unit-testable in isolation,
can be exercised against the deployed server without any history /
UI changes. If something is off in the protocol reading (shape of
`negotiated_mode`, 425 header format, etc.) we catch it before
anything else depends on it.

**Acceptance:**
- `test_loop.sh` still green (no sender / receiver changes yet).
- Ad-hoc script against the deployed server: probe capabilities →
  `{"stream_v1"}`, init with `mode="streaming"` on an online
  recipient → `negotiated_mode == "streaming"`, GET a chunk that
  doesn't exist yet → `too_early` with integer retry hint.
- Protocol contract test in `tests/protocol/test_server_contract.py`
  stays green (no change required).

### C.2 — History schema + status vocabulary (no runtime switch-on yet)

**What changes:**

1. `history.add()` / new rows gain optional fields
   `mode` (default `"classic"`), `chunks_uploaded` (int),
   `abort_reason` (str | None), `negotiated_mode` (str | None).
2. `history.py`: introduce a `TransferStatus` string-constant module
   listing the full enum: `uploading`, `sending`, `waiting` (classic
   init-waiting, kept as-is), `waiting_stream`, `delivering`,
   `complete`, `delivered`, `downloading`, `failed`, `aborted`.
   Just the constants — nothing writes the new ones yet.
3. `get_undelivered_transfer_ids()` grows to also exclude
   `status == "aborted"` — same rationale as `failed`.
4. `_compute_status` in `windows.py`: add pass-through branches for
   `sending`, `waiting_stream`, `aborted` that render visibly
   distinct labels + colours (blue for `sending`, yellow for
   `waiting_stream`, orange for `aborted`). The branches exist but
   nothing in the wild produces these statuses yet.

**Why second:** wiring the data-model additions in before any
client state machine writes to them means C.3 / C.4 can't
accidentally write a field the reader doesn't understand.

**Acceptance:**
- `test_loop.sh` green.
- Unit: hand-written history entries with each new status value
  render a readable row in the history window (manual smoke).
- `get_undelivered_transfer_ids()` still behaves the same for every
  existing row shape.

### C.3 — Recipient streaming receive loop

**What changes:**

1. `_download_transfer(transfer)` reads the new `mode` field from the
   pending-list row and branches:
   - `mode == "classic"` → existing `_receive_file_transfer` path.
   - `mode == "streaming"` → new `_receive_streaming_transfer`.
2. `_receive_streaming_transfer`:
   - Open `.incoming_<tid>.part` for append-write (sequential).
   - For each chunk `i` in `0..chunk_count-1`:
     - `download_chunk(tid, i)`:
       - `ok` → decrypt → write → fsync every N chunks → `ack_chunk(tid, i)`.
         Update history `chunks_downloaded = i+1`.
       - `too_early` → sleep for server-suggested `Retry-After`
         (default 1 s), own ramp 1 s → 2 s → 4 s → 8 s cap 10 s. Reset
         the ramp on any other outcome.
       - `aborted` (410) → mark history `status="aborted"`, wipe
         the `.part`, stop. (Server already wiped the blobs.)
       - `network_error` → classic 3-attempt 2 s-per-attempt retry
         (same as today's `_download_and_decrypt_chunk`). On
         exhaustion → client-side `abort_transfer(tid, "recipient_abort")`,
         then mark history `status="failed"` with a reason.
   - Per-chunk "no data for N minutes" budget: `5 min` of continuous
     `too_early` without any advancement → `abort_transfer(tid,
     "recipient_abort")`, history `failed`.
   - After the final chunk's `ack_chunk`: os.link the `.part` to
     its final name, delete the `.part`. Do NOT send the
     transfer-level `ack_transfer` — the per-chunk ACKs already
     finalised delivery server-side.
3. Short helper `_download_chunk_streaming` replaces the classic
   `_download_and_decrypt_chunk`'s retry loop for the streaming
   branch. Classic helper stays untouched.
4. Poller's orphan-part sweep (`_sweep_stale_parts`) stays unchanged
   — streaming uses the same `.incoming_<tid>.part` naming.

**Why third:** even without a streaming desktop sender, we can test
this against the deployed server from an Android client or a
hand-written curl test, and against a future C.4 sender. Receiving
first also proves the abort / per-chunk-ACK plumbing in the more
naturally-controllable direction.

**Acceptance:**
- `test_loop.sh` (classic) still green.
- Hand-run: a server-side `curl`-driven streaming upload (or once
  Android ships D.X, a phone streaming upload) to the desktop →
  `.part` grows incrementally, history shows `Downloading X/N`
  advancing, final file lands under `save_directory`, server blobs
  deleted after each ACK (observable from server logs /
  `bytesOnDiskForTransfer`).
- Force-abort from sender side → desktop row flips to `Aborted`,
  `.part` cleaned.
- Recipient-side abort (user deletes the downloading row from the
  history window) → `DELETE` with `reason=recipient_abort` fired,
  `.part` cleaned, row shows `Aborted`.
  (Row-delete wiring lives in C.5; stub the recipient abort for
  this phase by exposing an internal helper we call from a test.)

### C.4 — Sender streaming state machine

**What changes:**

1. `send_file(...)` gains `streaming: bool = True`. When true AND
   `"stream_v1"` is in the cached capabilities AND the filename is
   NOT `.fn.*`, request `mode="streaming"` at init.
2. `init_transfer` returns `negotiated_mode`. Behaviour branches:
   - `negotiated_mode == "classic"` → existing
     `_upload_chunk_with_retry` loop (unchanged).
   - `negotiated_mode == "streaming"` → new `_upload_stream` loop.
3. `_upload_stream`:
   - Sequential chunk upload, same encryption path as today.
   - `upload_chunk` outcomes:
     - `ok` → bump `chunks_uploaded`, fire `on_progress(tid, i+1,
       chunk_count, chunks_delivered=?)` — see callback signature
       below.
     - `storage_full` (507) → enter `waiting_stream` state. Set row
       status, stamp `waiting_started_at`, backoff 2 → 4 → 8 → 16 →
       cap 30 s. Total window `STORAGE_FULL_MAX_WINDOW_S` (30 min).
       On expiry → `abort_transfer(tid, "sender_failed")`, row
       `failed` with `failure_reason=quota_timeout`.
     - `aborted` (410) → recipient aborted. Stop upload, row flips
       to `aborted` with `abort_reason="recipient_abort"`.
     - `network_error` / other → classic 5 s cadence, 120 s budget,
       then `abort_transfer(tid, "sender_failed")`, row `failed`.
4. Delivery observation for "Sending X→Y":
   - Sender polls `get_sent_status` on the same cadence that's
     already running (the delivery tracker loop). In streaming mode
     the row gains an additional field `chunks_uploaded_server`
     (mirror of our local `chunks_uploaded`) for display — but the
     canonical X for the label is our local count (we just sent it),
     and Y is the server's `chunks_downloaded`.
   - The existing delivery-tracker stall safeguard (2-min
     no-advancement) still fires, but in streaming mode it ONLY
     clears the Y display — it does NOT abort. Same text as in
     `streaming-improvement.md §5.5`.
5. `on_progress` callback extended to a new signature
   `on_progress(tid, uploaded, delivered, chunk_count, state)` where
   `state ∈ {"uploading", "sending", "waiting_stream"}`. The
   existing callers in `windows.py` and `runners/send_runner.py`
   are rewritten to consume the new signature; classic path passes
   `delivered=0` and `state="uploading"`.
   Keep the old sentinels (`-1`, `-2`) internal to `api_client.py`;
   map them to the new `state` field at the boundary so history
   writes never see raw sentinel integers.

**Why fourth:** depends on C.1 (new ack / abort / capability API)
and C.2 (history has fields to write). Doing it after C.3 means the
recipient side is already proven end-to-end, so any issue at C.4 is
definitely on the sender side, not a protocol ambiguity.

**Acceptance:**
- `test_loop.sh` (classic) still green.
- New integration test (see C.6): large file, streaming mode, phone
  drains slower than desktop uploads, verify server's peak
  on-disk bytes for that transfer stays within a few chunks.
- Sender UI shows the three distinct phases over time: brief
  `Uploading 0/N` → `Sending X→Y` → `Delivered`. 507 injection
  (tight server quota) makes it flip to yellow `Waiting X→Y` and
  recover when space frees.

### C.5 — Send-files UI + history row actions

**What changes:**

1. `_compute_status` + progress-bar rendering in `windows.py`:
   - `sending` → blue bar showing `Y/N` with a secondary tick for X
     (or two stacked thin bars; pick the simpler widget — document
     the choice in the commit). Text: `Sending X→Y`.
   - `waiting_stream` → yellow bar (pulse) + text `Waiting X→Y` in
     `#FDD00C`.
   - `aborted` → no bar, orange text `Aborted` with optional
     `abort_reason` suffix when set.
2. `upload_progress` callback in `windows.py` rewritten for the new
   `on_progress(tid, uploaded, delivered, chunk_count, state)`
   signature. Still sets `chunks_downloaded` (the historical sender
   upload counter name) for backward compatibility with older
   readers, PLUS the new `chunks_uploaded` field.
3. `runners/send_runner.py`: same treatment.
4. History-row DELETE: the history window's existing "cancel
   delivery" dialog currently calls `api.cancel_transfer(tid)`.
   Migrate to `api.abort_transfer(tid, "sender_abort")`. The
   back-compat `cancel_transfer` wrapper (C.1) means old builds
   still see `status: "cancelled"` on the wire.
5. Recipient-side row deletion during streaming: when the user
   deletes a `status == "downloading"` row whose `mode ==
   "streaming"`, the window calls
   `api.abort_transfer(tid, "recipient_abort")` FIRST, then
   removes the row locally. Poller's streaming download loop also
   reads the row and aborts cleanly on the next chunk attempt.
6. Zombie-WAITING scrub (`_scrub_zombie_waiting`) in `windows.py`
   grows a sibling pass for `status == "waiting_stream"` using the
   same `waiting_started_at` / `STORAGE_FULL_MAX_WINDOW_S` rule.
   Scrubbed rows get `failure_reason="quota_timeout"` just like
   today.

**Why fifth:** UI depends on C.2 (statuses exist) + C.3 + C.4
(something actually writes them). Deliberately separate from C.4
so the sender state machine can be reviewed / bisected without
GTK churn in the same diff.

**Acceptance:**
- Manual UI sweep: each new status renders readably, progress bars
  animate correctly in both directions, history-row delete on an
  in-flight streaming transfer propagates to the server and the
  other side sees `Aborted`.

### C.6 — Integration test + test_loop.sh coverage

**What changes:**

1. Extend `tests/protocol/test_server_contract.py` with a
   desktop-simulator test if needed (may already be covered by the
   server contract tests — re-check after C.4).
2. New `tests/desktop_streaming_loop.sh` (or flag in the existing
   `test_loop.sh`): starts a hermetic PHP server with
   `streamingEnabled=true`, runs one desktop sender + one desktop
   receiver against it (headless mode), transfers a ~20 MB file in
   streaming mode, checks:
   - File content identical to source.
   - `server/storage/` peak size stays below `4 × CHUNK_SIZE = 8 MB`
     during the transfer.
   - No classic reservation path was hit (log-grep for
     `transfer.init.accepted mode=streaming`).
3. Abort-from-sender test: desktop receiver starts download, desktop
   sender aborts at chunk K/N, assert receiver row shows `aborted`
   and `.part` is cleaned.
4. Abort-from-recipient test: mirror.
5. Quota-gate test: tight `storageQuotaMB=2`, verify mid-stream 507
   flips sender to `waiting_stream` and drains once receiver catches
   up.

**Acceptance:**
- All three scripts pass on a clean checkout.
- `test_loop.sh` (classic) still green. Running both classic and
  streaming is now a meaningful cross-check; document the two-way
  matrix in a short note at the top of each script.

### C.7 — Cleanup + plan status

**What changes:**

1. Update `docs/plans/streaming-improvement.md` Phase C status block
   (DONE / LANDED after commit X).
2. Add a short entry under CLAUDE.md's "Key design decisions"
   describing the streaming state machine and the per-chunk ACK
   contract on the desktop side (mirrors the existing
   "Three-phase transfer state" entry).
3. Sweep for dead code: the sentinel-integer callbacks (`-1`, `-2`)
   that C.1 hides behind `state` can now be removed from
   `_init_transfer_with_retry` once C.4/C.5 have migrated all
   callers. Delete them in one commit so the next reader never
   sees them.

---

## Risks + mitigations specific to Phase C

1. **`upload_active.json` / `has_live_outgoing()` gate.** The poller
   today suppresses long-poll while any outgoing transfer is live.
   With streaming the outgoing transfer stays live much longer
   (covers both upload + delivery). Long poll for INCOMING
   transfers is still useful during that window. Audit this in C.4
   before enabling streaming-sender by default — we may need to
   split "live outgoing" from "long poll suppressed".

2. **Multiple processes writing history.** `send_runner.py`, the
   send-files subprocess, and the tray process all write to the
   same `history.json` through `fcntl` locks. New streaming writes
   add more lock churn (every chunk ACK bumps `chunks_uploaded`).
   Batch the writes: the sender loop only updates history every
   ~500 ms OR on state transition, whichever comes first. Same
   cadence as today's delivery tracker.

3. **`cancel_transfer` wrapper may drift.** Keeping the old name
   means callers keep using it, which is fine, but the DELETE body
   shape differs (`reason` field added). Make sure the server's
   back-compat handler still accepts a body-less DELETE (Phase A
   shipped this — verify with a protocol test).

4. **CSS classes for new bars.** `upload-bar`, `download-bar`,
   `delivery-bar` exist; add `stream-sending-bar`,
   `stream-waiting-bar` with the brand palette constants already
   in `windows.py`.

5. **Delivery tracker concurrency.** Existing tracker uses
   `get_sent_status(timeout=0.75)` every 500 ms. Streaming needs
   `chunks_uploaded` + `chunks_downloaded` in the same response —
   verify the server's `formatSent` returns both in the same row
   (the streaming-improvement plan §1.1 says it does; re-check
   after C.1).

6. **Capability probe order.** `check_capabilities` must run before
   the first streaming `init`. In the tray flow the startup
   `check_connection()` already hits the server; piggyback on it.
   In the `--send` one-shot runner, do a dedicated GET before init.

---

## Sequencing summary

```
C.1 api_client.py (protocol)   → additive, server-only reviewable
C.2 history fields + statuses  → additive, no runtime writer
C.3 recipient streaming loop   → depends on C.1/C.2
C.4 sender streaming loop      → depends on C.1/C.2; parallel to C.3 in theory
C.5 UI + row actions           → depends on C.3 + C.4
C.6 integration tests          → ties it all together
C.7 docs + dead-code sweep     → final pass
```

Each commit is reviewable in isolation. No sub-phase leaves the
tree in a state where classic transfers regress. Old server +
new client: capability probe comes back empty, streaming disabled
at source. New server + old client: no `mode` field in init,
server defaults to classic.

After Phase C is green end-to-end against the deployed server,
Phase D (Android) mirrors this plan sub-phase-by-sub-phase with
the same split.
