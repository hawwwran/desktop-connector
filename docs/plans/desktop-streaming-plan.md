# desktop-streaming-plan.md

**Status: COMPLETED (2026-04-18).**
Implemented by commit `aa5ac20` (streaming sends and receives
chunk-by-chunk) — the full plan landed as one commit rather than the
suggested 7-commit sequence. `crypto.py` gained `generate_base_nonce`,
`build_encrypted_metadata`, `encrypt_chunk`, `decrypt_chunk`;
`encrypt_file_to_chunks` and `decrypt_chunks_to_file` were deleted.
`ApiClient.send_file` now streams with 5 s / 120 s per-chunk retry.
`Poller._receive_file_transfer` streams to
`{save_dir}/.parts/.incoming_{tid}.part`, finalizes via atomic
`os.link` (with cross-FS `shutil.move` fallback), ACKs after durable
write, and keeps the file if ACK fails. `_sweep_stale_parts` runs on
poller start with a 24 h TTL. `.fn.*` transfers keep the tiny
in-memory path. The optional §5 item — propagating a per-chunk
failure reason into the history entry rather than only the log — was
left as log-only, which the plan explicitly allowed.

Mirrors the completed Android plans (`android-streaming-upload-plan.md`
and `android-streaming-receive-plan.md`) on the desktop (Python) side.
Desktop used to load every sent and received file fully into RAM on
both sides; it now streams both, matching Android.

Protocol, metadata schema, nonce derivation, AES-GCM format, and chunk
size stay unchanged. This is a desktop-client-only refactor.

---

## Problem summary

Two in-memory blowups exist on desktop today:

### Send path — `desktop/src/crypto.py:130-165` `encrypt_file_to_chunks`
- Line 136: `data = filepath.read_bytes()` — entire plaintext in RAM.
- Lines 139-148: builds a `list[bytes]` with every encrypted chunk.
- Returns that list to `api_client.send_file` (`desktop/src/api_client.py:120`).

Peak RAM for a 2 GB file ≈ 2 GB plaintext + ~2 GB encrypted list
simultaneously.

### Receive path — `desktop/src/poller.py:225-241` and `desktop/src/crypto.py:167-202`
- `encrypted_chunks = []` accumulates every downloaded encrypted chunk.
- `decrypt_chunks_to_file` builds `plaintext_parts`, does
  `b"".join(plaintext_parts)`, then `save_path.write_bytes(data)`.

Peak RAM ≈ 2× file size. Unlike Android (`DesktopConnector/.parts/`),
desktop never touches disk until the very end.

Both paths fail exactly the way the Android versions used to before
their respective refactors. The fix is the same shape.

---

## Goals (both paths)

- memory bounded by `CHUNK_SIZE` (2 MB) regardless of file size
- safer for large files on disk and on wire
- failure-tolerant: chunk-level retries with an explicit give-up window
- partial-transfer cleanup — no misleading final files
- honest progress: progress starts after chunk 1 is really on the wire
  (send) / written to disk (receive), not after a fake pre-phase
- resume-ready structure later, but not blocked on it
- protocol and server unchanged

---

# Part A — send path refactor

Mirrors `android-streaming-upload-plan.md`.

## Target model

1. load recipient symmetric key
2. stat file size → compute `chunk_count = max(1, ceil(size / CHUNK_SIZE))`
3. generate `base_nonce`
4. build encrypted metadata (no chunks yet)
5. `init_transfer(...)` with the correct `chunk_count`
6. loop over plaintext chunks, for each `index`:
   - read exactly the next `CHUNK_SIZE` bytes (last chunk may be shorter)
   - encrypt that chunk with `make_chunk_nonce(base_nonce, index)`
   - upload; on failure retry every 5 s; after 120 s continuous failure
     for the same chunk, mark transfer `failed` and return
   - on success: fire `on_progress` callback, continue
7. return `transfer_id` on success

All files use this path (no two code paths), matching the Android
decision.

## Retry policy

Per chunk:

```
first_failure_at = None
while True:
    if upload_chunk(...) succeeds: break
    if first_failure_at is None:
        first_failure_at = monotonic()
    elif monotonic() - first_failure_at >= 120:
        fail with "Chunk {i+1}/{N} failed continuously for 120s"
    sleep(5)
```

Reset timer on each successful chunk. Same interpretation as Android.

## Code changes (send)

### 1. Replace `encrypt_file_to_chunks` with two primitives in `crypto.py`
Mirroring `KeyManager.buildEncryptedMetadata` + `encryptChunk`:

```python
def generate_base_nonce() -> bytes: ...
def build_encrypted_metadata(filename, mime, size, chunk_count,
                             base_nonce, key) -> str: ...
def encrypt_chunk(plaintext: bytes, base_nonce: bytes,
                  index: int, key: bytes) -> bytes: ...
```

Delete `encrypt_file_to_chunks` in the same commit once no caller
remains (per repo style — no compat shims).

### 2. Rewrite `ApiClient.send_file`
- no `read_bytes()`
- open file once, iterate with `read(CHUNK_SIZE)`
- compute chunk_count from `filepath.stat().st_size` upfront
- init transfer first, then stream chunks
- call `upload_chunk_with_retry` per chunk (5 s / 120 s)
- close file in a `try/finally` (or use `with open(...)`) on both
  success and every failure path

### 3. Progress callback semantics (unchanged contract)
Callers (`tray._do_send_clipboard`, `windows._send_files`,
`main.run_send_file`) currently do:

```python
if uploaded == 0:
    history.add(..., transfer_id=transfer_id, status="uploading",
                chunks_downloaded=0, chunks_total=total_chunks)
else:
    history.update(transfer_id, chunks_downloaded=uploaded,
                   chunks_total=total_chunks)
```

Keep that exact contract. `send_file` must still emit the initial
`uploaded=0` callback after `init_transfer` succeeds, then one
callback per successful chunk.

### 4. (Optional) Introduce a `preparing` status
Android added `PREPARING` because real upload could take seconds after
the user clicked send. On desktop the pre-phase is milliseconds
(stat + single metadata encrypt + init_transfer), so the honesty
problem is smaller. Recommended: **skip the `preparing` status for the
first pass** — keep the initial history.add with `status="uploading"`
and `chunks_downloaded=0/chunks_total=N` which already paints an
honest 0/N bar. Revisit only if the init_transfer retry policy grows.

### 5. Failure messages
Propagate chunk-specific errors to the caller so history `status="failed"`
can be paired with a human-readable note:

- `Cannot read source file`
- `Failed to initialize transfer on server`
- `Chunk {i+1}/{N} failed continuously for 120s`

Currently `send_file` returns `None` on failure. Either extend to
return `(tid, None)` / `(None, reason)` or set a new `error` history
field. Keep the change minimal — either is acceptable.

### 6. Do not break `busy_outgoing` gating
`desktop/src/poller.py:113` treats any `upload_active.json` marker OR
any undelivered history row as busy and skips long-polling for PHP's
single-threaded sake. Longer retries (up to 2 min per chunk) can
extend `busy_outgoing` — that is acceptable and already the Android
model. No change needed here.

### 7. Thread safety
Per-send retries are synchronous in the caller's thread. That matches
current `send_file` behavior. No new threads required.

## Send acceptance criteria

1. RAM during send of a 2 GB file is bounded by ~a few × `CHUNK_SIZE`.
2. Progress fires after chunk 1 uploads — not after the whole file
   has been encrypted.
3. A transient network blip mid-upload retries every 5 s and recovers.
4. Persistent failure flips history row to `failed` with a message
   that names the failed chunk index.
5. Clipboard send (`.fn.clipboard.*`), unpair (`.fn.unpair`), bulk
   send (send-files window), and CLI `--send=...` all work through
   the single new streamed path.
6. `test_loop.sh` still passes end-to-end.

---

# Part B — receive path refactor

Mirrors `android-streaming-receive-plan.md`.

## Target model

1. see pending transfer (`_poll_once`)
2. decrypt metadata, extract filename, `chunk_count`, `base_nonce`
3. branch on `filename.startswith(".fn.")`:
   - **command transfers (`.fn.*`)**: keep the existing tiny
     in-memory path (accumulate + `decrypt_chunks_to_file` into a tmp
     path, then dispatch clipboard/unpair). These are bounded by
     protocol to tiny payloads; no need to burden them with temp-file
     machinery.
   - **normal files**: go through the streamed path below.
4. decide final save path (collision-resolve up front with the
   existing `save_dir / filename` + `_{counter}` logic)
5. allocate `{save_dir}/.parts/.incoming_{transfer_id}.part` on the
   **same filesystem** so the finalize rename is atomic
6. insert/update history row (`status="downloading"`, chunks 0/N)
7. for each chunk index 0..N-1:
   - download encrypted chunk (with existing retry wrapper)
   - decrypt immediately
   - append plaintext to the open `.part` file
   - update `chunks_downloaded = i + 1`
   - on any failure: close stream, delete `.part`, mark `failed`,
     return without ACKing
8. close and flush stream
9. atomic rename `.part` → final path
10. ACK the transfer (see ACK rule below)
11. mark history `status="complete"`, set `content_path` to final
    path, clear progress counters

## ACK-after-durable-write rule

ACK only after:
1. every chunk downloaded and decrypted
2. every plaintext byte written and flushed
3. the `.part` file successfully renamed to the final destination

If ACK then fails:
- **keep the file** — do not delete a fully received file because of
  a network hiccup
- log `"ACK failed for {tid} after durable write — keeping file"`
- mark history `status="complete"`, `delivered=True` client-side; the
  server will naturally retry delivery (sender still sees
  "delivering"); the sender's stall safeguard or a future
  app-restart-ack retry picks it up

This matches the Android implementation exactly (`PollService.kt:624-639`).

## Temp file layout

```
{save_dir}/
    (final user-visible files)
    .parts/
        .incoming_{transfer_id}.part
```

Same convention as Android. Benefits:
- rename is atomic (same filesystem as final dir)
- hidden from normal file browsers
- `{save_dir}/.parts/` can be swept for orphans on desktop start

## Orphan sweep

Add a once-per-startup sweep in the poller (or main), matching
Android `sweepStaleParts`:

- list `{save_dir}/.parts/.incoming_*.part`
- delete any with `mtime < now - 24h` (pick a TTL — Android uses a
  constant `STALE_PART_TTL_MS`; match it)
- log count

This covers force-quits, OOM kills, and power loss.

## Per-chunk download retry

Keep the existing 3-attempt backoff currently implemented in
`ApiClient.download_chunk` usage at `poller.py:228-233`. The plan is
**not** to widen this to a 2-minute continuous-failure window on the
receive side in this first pass — Android kept its 3-attempt wrapper
too (`downloadChunkWithRetry` with 2 s × attempt backoff). Document
the option to upgrade later to a 5 s / 120 s model that mirrors send,
but do not do it in this commit.

## Cancellation

Android checks `db.transferDao().exists(dbRowId) == 0` per chunk to
detect user-deleted rows and abort with ACK. Desktop has no
equivalent "cancel by deleting the row mid-download" gesture today
— skip this feature; note it as a future option. If it ever gets
added, the natural hook is to check the history entry's presence per
chunk and exit the loop cleanly (delete `.part`, ACK, return).

## Code changes (receive)

### 1. Replace `decrypt_chunks_to_file` in `crypto.py`
Introduce a streaming-oriented API:

```python
def decrypt_chunk(blob: bytes, base_nonce: bytes,
                  index: int, key: bytes) -> bytes: ...
```

Delete `decrypt_chunks_to_file` in the same commit once
`_download_transfer` no longer calls it. Keep `decrypt_metadata` and
`decrypt_blob` untouched — they're still needed.

### 2. Refactor `poller._download_transfer` into phases
Split into helpers so each phase is clear:

- `_decrypt_metadata_and_prepare(transfer)` → returns `(filename,
  final_path, temp_path, base_nonce)` or `None` on prep failure
- `_stream_chunks_to_temp(temp_path, chunk_count, base_nonce, key,
  transfer_id)` → returns `True` on success, `False` on failure (cleans
  up temp on failure)
- `_finalize_and_ack(temp_path, final_path, transfer_id)` → returns
  `True` on success

### 3. Branch `.fn.*` vs normal files early
Keep `.fn.*` on the tiny in-memory path. Normal files go through the
streamed path. Mirrors Android's split and keeps command transfers
simple.

### 4. Startup sweep
Call `_sweep_stale_parts(save_dir)` once in `Poller.run()` (or main
after poller is constructed). Log removed count.

### 5. Failure taxonomy in logs
Distinguish in log lines:
- `download failed at chunk i/N`
- `decrypt failed at chunk i/N`
- `write failed at chunk i/N`
- `rename failed`
- `ACK failed after durable write`

## Receive acceptance criteria

1. Receiving a 2 GB file uses RAM bounded by ~a few × `CHUNK_SIZE`.
2. The final file never appears in `save_dir` while incomplete — only
   the `.part` file in `.parts/` does.
3. On any mid-transfer failure, the `.part` file is deleted and the
   history entry is `failed`; no misleading file in `save_dir`.
4. If ACK fails after the rename, the file is kept; history shows
   `complete`; log line makes the situation diagnosable.
5. Orphan `.part` files from prior aborts are cleaned up on startup.
6. `.fn.clipboard.text`, `.fn.clipboard.image`, and `.fn.unpair` all
   continue to work through the untouched in-memory path.
7. Collision suffix logic (`file_1.ext`, `file_2.ext`) is preserved.
8. `test_loop.sh` still passes end-to-end.

---

## Suggested commit sequence

Modeled after Android's commit cadence.

### Commit 1
`refactor(desktop-send): split metadata build from chunk encryption in crypto helpers`

### Commit 2
`refactor(desktop-send): stream chunks through send_file (no full-buffer encrypt)`

### Commit 3
`refactor(desktop-send): add per-chunk retry window (5s retry, 120s fail)`

### Commit 4
`refactor(desktop-recv): split .fn transfer handling from normal file receive path`

### Commit 5
`refactor(desktop-recv): stream chunks to .parts/.incoming_{tid}.part and atomic rename`

### Commit 6
`refactor(desktop-recv): keep file on ACK failure after durable write`

### Commit 7
`chore(desktop-recv): sweep stale .parts on poller start`

Each commit should keep `test_loop.sh` green.

---

## What does not change

- server protocol
- chunk size (`CHUNK_SIZE = 2 * 1024 * 1024`)
- metadata schema (keys: `filename`, `mime_type`, `size`,
  `chunk_count`, `chunk_size`, `base_nonce`)
- nonce derivation (`make_chunk_nonce(base_nonce, index)`)
- AES-GCM blob format (`nonce ‖ ciphertext ‖ tag`)
- delivery tracker logic
- long-poll / busy-outgoing gating
- Android receiver or sender (Android is already correct)
- history.json schema (new values may appear in `status` but the field
  is already free-form)

---

## Test plan

### Small file (< 2 MB, single chunk)
- send and receive work end-to-end
- history state transitions are normal
- `.part` file appears briefly in `.parts/` then disappears after
  rename

### Medium file (~50 MB)
- progress bar advances smoothly per chunk on both sides
- RAM stays flat
- final file matches SHA-256 of source

### Large file (≥ 1 GB)
- RAM stays flat on both sides
- no swap thrash, no OOM
- finalize rename is atomic (between poll iterations it either
  exists or doesn't — never a half-written file under the final name)
- transfer succeeds and SHA-256 matches

### Network interruption mid-upload
- pull the network cable during chunk 5 of 20
- sender retries every 5 s
- restore network within 2 min → upload completes normally
- keep it dead for > 2 min → sender marks `failed` with the exact
  chunk index in the error string

### Network interruption mid-download
- pull the network cable during chunk 5 of 20
- receiver retries per-chunk (existing 3-attempt policy)
- after exhaustion → `failed`; `.part` file gets deleted; no file in
  `save_dir`

### ACK failure simulation
- block only the ack endpoint after the last chunk is written
- file remains in `save_dir`
- log shows `"ACK failed ... keeping file"`
- history shows `complete`

### Orphan sweep
- crash the desktop during a large receive (kill -9)
- confirm `.parts/.incoming_*.part` exists
- restart → sweep removes it (assuming TTL elapsed; for manual test,
  temporarily lower TTL)

### `.fn.*` transfers
- clipboard text, clipboard image, unpair all still function
- no `.part` file is created for these

### Mixed concurrent traffic
- while a large file is uploading, send a `.fn.clipboard.text` from
  the phone — it should arrive and dispatch without waiting for the
  upload to finish

---

## Risks

### 1. Rename atomicity depends on same filesystem
If the user configures `save_directory` to a mount point that differs
from `.parts/` (unlikely because `.parts` is a subdir of `save_dir`,
but on FUSE/NFS mounts odd behavior exists), rename can fail.
Mitigation: detect `OSError` on rename, fall back to `shutil.move`
with explicit copy-then-delete, log the degradation.

### 2. ACK-after-write behavior must not regress delivery UX
If ACK fails but the file is kept, the sender will keep seeing
"delivering" until the stall safeguard trips at 2 min. That is the
same behavior Android accepts. Document it — do not "fix" it by
deleting the file.

### 3. Backwards-compat pressure
Tempting to keep `encrypt_file_to_chunks` / `decrypt_chunks_to_file`
alongside the new streamed helpers. Don't — delete them in the same
commit once no caller remains, per repo style.

### 4. 2-minute retry stretches busy_outgoing
`busy_outgoing` blocks long-polling while any outgoing work is
in-flight. A pathologically flaky upload that retries for 2 min will
also block the long poll for 2 min. Acceptable — matches Android.

### 5. Clipboard/unpair paths still go through `send_file`
A bug in the new streamed send path would break clipboard + unpair
too. Keep them in the test pass.

### 6. `test_loop.sh` visibility
If the integration test only checks end-to-end success of a tiny
file, it won't exercise streaming. Consider adding a ≥ 10 MB case
to `test_loop.sh` (optional; separate commit).

---

## Final recommendation

Implement the two sub-refactors (send, receive) in the commit sequence
above. End state: desktop and Android use structurally identical
streamed file transfer paths — bounded memory, temp-file-first
receives, chunk-level retries with explicit give-up, ACK-after-durable-
write, and orphan `.part` cleanup — all with zero protocol change.
