# android-pipelining-plan.md

**Status: NOT NEEDED (2026-04-18).**
Benchmarked on a real device (Vivo V2145, 8-core arm64-v8a) during
two back-to-back ~250 MB uploads over WiFi. The prerequisite
measurement step showed the phone is nowhere near CPU-bound on the
crypto/network path, so pipelining has no bottleneck to relieve and
the plan stays on the shelf.

## Benchmark evidence

**Run 2 (55 samples over 79 s of active upload, two-pass `top -H` with 1 s deltas):**

| Thread           | Mean CPU | Peak | Role                            |
|------------------|---------:|-----:|---------------------------------|
| RenderThread     |   29 %   | 41 % | UI / progress-bar rendering     |
| main (`esktopconnector`) | 24 % | 31 % | Activity thread          |
| DefaultDispatcher worker | 9.9 % | 18 % | **crypto + network path** |
| OkHttpTaskRunner |  2.8 %   |  5 % | HTTP client thread              |
| HeapTaskDaemon   |  2.7 %   | 10 % | GC                              |

Total-CPU-across-all-threads distribution per 1 s sample:

```
  0– 24 %:  0
 25– 49 %:  6
 50– 74 %: 22    ← dominant bucket
 75– 99 %: 24
100–149 %:  3
150+    :  0
```

Run 1 (68 samples over 98 s) produced substantively identical numbers
(RenderThread 33 % mean / 53 % peak; DefaultDispatcher 4.3 % mean / 10 %
peak). Both runs agree.

## Interpretation

- Out of 8 cores, the phone averages well under one busy core during
  upload. Seven cores sit idle. The wire is the bottleneck.
- The worker dispatcher that actually runs `encryptChunk` + `uploadChunk`
  peaks at 18 % of a single core. OkHttp peaks at 5 %. Pipelining
  overlaps two things that are both ≥ 80 % idle.
- The dominant CPU consumer is **UI rendering** (RenderThread + main
  ≈ 53 % combined mean) — the phone draws the progress bar more than
  it does crypto. If a phone-side optimization is ever worth doing,
  the lever is progress-bar update coalescing, not a channel-based
  pipeline.

The plan's own decision rule applies:

> If CPU is idle and throughput is close to the measured raw WiFi / LTE
> rate, the wire is the bottleneck and this plan is a no-op. Skip it.

So this refactor is skipped. Leave the design below intact as
reference — if the bottleneck profile ever changes (CPU-bound codec,
larger chunks, radically faster uplink), re-run the benchmark and
reconsider.

---

## Purpose

Define an optional follow-up to the streaming upload and streaming receive
refactors that overlaps CPU work (crypto) with network I/O — without
introducing request-level parallelism.

The goal is to make transfers fully use the wire while the phone is
encrypting/decrypting the next chunk, keeping exactly one HTTP request
in flight at a time.

This plan is intentionally **pipelining**, not **parallelism**.

---

## Why not just upload N chunks in parallel

Parallel request-level upload (multiple concurrent HTTP requests for
different chunk indexes) was considered and rejected for this codebase:

### 1. The server is PHP
Per `CLAUDE.md`:

> PHP single-threaded: The built-in PHP server handles one request at a time.
> For production, use nginx + php-fpm or Apache + mod_php.

On the dev server, parallel requests are serialized. On shared Apache
hosting they do scale, but they contend on the same SQLite DB and the
same storage directory. Concurrent 2 MB writes to one SQLite file are
not a good idea.

### 2. Chunks are already 2 MB
Parallel HTTP connections mostly help when chunks are small enough that
per-request overhead and TCP slow-start dominate. A 2 MB chunk has
already finished slow-start and is close to saturating a single TCP
connection on a typical mobile uplink.

### 3. It regresses the memory guarantee
The streaming refactor bounded memory to roughly `1 × CHUNK_SIZE` on
each side. Five concurrent chunks would push that to `~5 × CHUNK_SIZE`
of read buffers plus `~5 × CHUNK_SIZE` of encrypted ciphertexts, plus
more on the receive side. Still manageable, but it undoes the property
we just landed.

### 4. It grows the error surface
The per-chunk "retry every 5 s, fail after 120 s" model assumes one
chunk is being worked on at a time. With N concurrent chunks we have
to cancel siblings when one fails, reconcile per-chunk timers across
threads, and define progress ("7 of 125") that can move non-monotonically.

### 5. It does not address the likely bottleneck
On a modern phone the AES-GCM throughput of ~500 MB/s+ easily outruns
the upload bandwidth. If the phone isn't CPU-bound, adding concurrent
TCP connections gains little.

---

## Why pipelining is still worth considering

Even without parallel requests, the current streamed path is serial
between stages:

- read N → encrypt N → upload N → read N+1 → encrypt N+1 → upload N+1

While `upload N` is blocked on the network, the CPU is idle. While
`encrypt N+1` is running, the network is idle.

Pipelining overlaps those stages:

- while chunk N is uploading, chunk N+1 is being read and encrypted
- while chunk N is downloading, chunk N−1 is being decrypted and written

One HTTP request remains in flight at a time, so server concurrency
and retry semantics are unchanged. Only the local compute/I/O phases
overlap.

Expected speedup:

- On a CPU-bound device, up to the fraction of total time spent in
  crypto (often 10–20 % for a big file on WiFi).
- On a network-bound device, near zero — pipelining cannot create
  bandwidth that the wire does not have.

This is why the plan says "measure first" before implementing.

---

## Prerequisites — measure before building

The refactor only helps when CPU/disk work is non-trivial relative to
network time. Before investing:

### 1. Baseline a large transfer
Send a 500 MB file from the phone while collecting:

```
adb shell dumpsys cpuinfo | grep DesktopConnector
adb shell top -n 1 | grep DesktopConnector
```

### 2. Interpret
- If the main thread / crypto thread shows > ~50 % CPU during the upload
  and network utilization is below link capacity, pipelining will help.
- If CPU is idle and throughput is close to the measured raw WiFi/LTE
  rate, the wire is the bottleneck and this plan is a no-op. Skip it.

Only continue if the numbers justify the complexity.

---

## Scope

### In scope
- Android upload path (`UploadWorker`)
- Android receive path (`PollService.receiveFileTransfer`)
- A bounded `Channel<...>` between a producer coroutine and the
  existing consumer loop on each side

### Out of scope
- Multi-request concurrency on either direction
- Any server changes
- Any protocol changes
- Changes to `.fn.*` handling (those transfers are tiny, pipelining
  would be pure overhead)
- Resume / partial-retry changes

---

## Upload pipeline design

### Current flow
```
loop:
  plaintext  = readFullChunk(input)       // disk / URI
  ciphertext = encryptChunk(plaintext)    // CPU
  uploadChunkWithRetry(ciphertext)        // network
  updateProgress()
```

### Pipelined flow
```
Channel<EncryptedChunk>(capacity = 2)

producer coroutine:
  for index in 0 until chunkCount:
    plaintext  = readFullChunk(input)
    ciphertext = encryptChunk(plaintext)
    channel.send(EncryptedChunk(index, ciphertext))
  channel.close()

consumer (main worker coroutine):
  for chunk in channel:
    uploadChunkWithRetry(chunk)        // existing 5s / 120s logic
    updateProgress(chunk.index)
```

### EncryptedChunk data class
```kotlin
private data class EncryptedChunk(val index: Int, val ciphertext: ByteArray)
```

### Channel capacity
`capacity = 2` is enough to keep the network busy while the next chunk
is being encrypted. Larger capacities increase peak memory without
increasing throughput (one chunk is always in the uploader anyway).

### Cancellation
- If `uploadChunkWithRetry` returns a terminal failure, the consumer
  must cancel the producer and close its input stream.
- If the producer throws (read error, encrypt error), the consumer
  must stop and mark the transfer FAILED with the specific phase.

Use a `coroutineScope { ... }` so producer + consumer share a parent
scope and either failure cancels both.

### Memory
Peak bytes in flight: `~3 × CHUNK_SIZE` (one in the upload, two
encrypted chunks buffered in the channel, bounded by capacity + the
chunk currently being produced/consumed). Still bounded, still small.

### Progress semantics
Unchanged: `updateProgress(index + 1, chunkCount)` still fires after
each successful upload, in index order (the channel preserves order).

### Retry semantics
Unchanged: the per-chunk 5 s / 120 s window still lives in the consumer
around `api.uploadChunk`. Retry does not re-read or re-encrypt — the
ciphertext stays in the channel item.

### What the producer owns
- Source `InputStream` lifetime
- Read buffer
- Encryption invocation

### What the consumer owns
- Network call
- Per-chunk retry timer
- DB progress writes
- Final status transitions (COMPLETE / FAILED)

---

## Receive pipeline design

### Current flow
```
loop:
  ciphertext = downloadChunkWithRetry(index)  // network
  plaintext  = decryptBlob(ciphertext)        // CPU
  tempOut.write(plaintext)                    // disk
  updateProgress()
```

### Pipelined flow
```
Channel<DownloadedChunk>(capacity = 2)

producer coroutine:
  for index in 0 until chunkCount:
    ciphertext = downloadChunkWithRetry(index)
    if (ciphertext == null) { fail; break }
    channel.send(DownloadedChunk(index, ciphertext))
  channel.close()

consumer (main coroutine):
  for chunk in channel:
    plaintext = decryptBlob(chunk.ciphertext)
    tempOut.write(plaintext)
    updateProgress(chunk.index)
```

### DownloadedChunk data class
```kotlin
private data class DownloadedChunk(val index: Int, val ciphertext: ByteArray)
```

### Cancellation
- If the producer hits a terminal download failure, the consumer must
  mark FAILED and delete the temp `.part` file.
- If the user deletes the DB row mid-transfer, the consumer detects
  it (as today), signals cancellation to the producer, ACKs the
  server, and deletes the temp file.

### Memory
Peak: `~3 × CHUNK_SIZE` — one downloaded but not yet decrypted, one
being decrypted/written, and one buffered.

### Retry semantics
Unchanged: `downloadChunkWithRetry` keeps its 3-attempt-with-backoff
policy, inside the producer. Commit 5's ACK-after-durable-write
behavior stays on the consumer after the final `flush()` and rename.

---

## Failure policy reference

| Failure                                | Producer action | Consumer action                                         |
|----------------------------------------|-----------------|---------------------------------------------------------|
| read/encrypt error (upload)            | throw           | cancel producer, FAILED with phase + chunk index        |
| 120 s retry window elapsed (upload)    | cancel          | FAILED with `Chunk X/Y failed continuously for 120s`    |
| 3 download attempts fail (receive)     | signal + close  | FAILED with `Download failed at chunk X/Y`, delete part |
| decrypt error (receive)                | n/a             | FAILED with `Decryption failed at chunk X/Y`, delete part |
| write error (receive)                  | n/a             | FAILED with `Write failed`, delete part                 |
| user cancels (receive)                 | cancel          | ACK server, delete part                                 |
| ACK fails after durable write (receive)| n/a             | log and keep file (commit 5 behavior)                   |

All per-chunk behavior is a strict superset of today's — no error path
is relaxed.

---

## Suggested implementation phases

### Phase 1 — upload pipeline
- Introduce `EncryptedChunk` private data class in `UploadWorker`
- Wrap the streaming loop in `coroutineScope { ... }`
- Producer coroutine owns reading and encryption
- Consumer keeps `uploadChunkWithRetry` and progress updates
- Verify with 1 MB, 250 MB, 500 MB transfers

### Phase 2 — receive pipeline
- Introduce `DownloadedChunk` private data class in `PollService`
- Wrap `receiveFileTransfer` streaming section in `coroutineScope`
- Producer owns downloading + download retry
- Consumer keeps decrypt, write, progress, finalize

### Phase 3 — measurement
- Re-run the same 500 MB benchmark from the prerequisite step
- Compare CPU utilization and wall-clock time against the baseline
- If gain < ~5 %, revert — the complexity is not paying for itself

---

## Suggested commit sequence

### Commit 1
`refactor(android-upload): pipeline chunk read/encrypt with upload`

### Commit 2
`refactor(android-recv): pipeline chunk download with decrypt/write`

Two commits total. Each is testable on its own: upload pipelining is
observable from a single phone → desktop transfer, receive pipelining
from desktop → phone.

---

## What must not change

- Server API, chunk format, metadata format, nonce derivation
- Chunk size (`CryptoUtils.CHUNK_SIZE = 2 MB`)
- Per-chunk retry semantics (5 s interval, 120 s window on upload;
  3 attempts with backoff on download)
- Progress reporting semantics (still index + 1 / total, monotonic)
- ACK timing (after durable write on receive, after last chunk on upload)
- `.fn.*` path — remains serial, no pipelining

---

## Acceptance criteria

### 1. Throughput increases measurably on a CPU-bound device
A 500 MB transfer completes faster than the current serial streaming
implementation on a device where the baseline benchmark showed
non-trivial CPU usage.

### 2. Memory stays bounded
Peak heap usage during a 500 MB transfer does not exceed
`~5 × CHUNK_SIZE` (10 MB), leaving plenty of headroom.

### 3. No regression in error behavior
All failure modes from the existing test plan still produce the same
user-visible outcome:
- continuous 120 s upload failure → FAILED with specific message
- decrypt failure → FAILED, partial file removed
- user cancel → ACK, partial file removed
- network blip under 120 s → recovers silently

### 4. `.fn.*` transfers unaffected
Clipboard text, clipboard image, and unpair all behave exactly as they
did before the pipelining change.

### 5. ACK semantics unchanged
ACK still fires only after the durable write on the receive side.

---

## Risks

### 1. Premature optimization
If the benchmark shows no CPU bottleneck, this refactor buys nothing
and adds a coroutine scope, a channel, and two data classes. The
measurement phase is there to catch this before code is written.

### 2. Coroutine cancellation leaks
If the producer isn't cancelled cleanly on consumer failure, the
`InputStream` or network request can leak. Using `coroutineScope { }`
with structured concurrency and `.use { }` on streams mitigates this,
but the plan should be reviewed specifically for this hazard.

### 3. Channel capacity tuning
Capacity 2 is chosen to hit the overlap sweet spot; raising it buys
nothing and costs memory. Test with capacity 1 as well — if there is
no measurable difference, use 1 for the lowest memory footprint.

### 4. Reordering surprises
`Channel` preserves send order, so consumer sees indexes in ascending
order. Any refactor that introduces non-ordered delivery (e.g., a
`Flow.flatMapMerge` with concurrency) would break progress monotonicity
and must be avoided.

---

## Final recommendation

Do not implement this yet.

Land the current streaming refactors, test real large-file transfers on
the phone, and measure where the time actually goes. If the phone's
CPU is saturated during a 500 MB transfer, return to this plan. If the
network is the bottleneck — which is the likely outcome on mobile data
or a busy WiFi — leave the code at the simpler single-threaded form.

Pipelining is a good tool, but only in response to a specific bottleneck
that has actually been observed.
