# android-streaming-upload-plan.md

**Status: COMPLETED (2026-04-18).**
Implemented by commits `6625b91` (streaming + PREPARING state) and
`fe383d6` (5 s / 120 s per-chunk retry window). `UploadWorker` now
reads, encrypts, and uploads one chunk at a time with memory bounded
by `CHUNK_SIZE`. `KeyManager.encryptFileToChunks` was replaced by
`buildEncryptedMetadata` + `encryptChunk`. Unknown-size URIs spool to
cache first. Protocol unchanged.

## Purpose

This document defines the desired Android upload behavior for **all outgoing files**:

- stream every file
- do not full-buffer files in memory
- upload chunk by chunk
- if a chunk upload fails, retry every 5 seconds
- if the same chunk keeps failing continuously for 2 minutes, give up
- mark the transfer as `FAILED`

This plan is intentionally aligned with the current protocol and current server behavior.

---

## Decision

The Android client should move to a **fully streamed upload path for all files**, not just large files.

That means:

- no `readBytes()` for normal outgoing uploads
- no building the entire encrypted chunk list in memory first
- no “prepare whole file, then upload” behavior
- one chunk read -> one chunk encrypted -> one chunk uploaded

This should become the default upload architecture.

---

## Why this is the right direction

The current sender path marks the transfer as `UPLOADING` before actual network upload starts, then reads the whole file and encrypts the whole file before any chunk progress becomes visible. That is the core reason why large files can appear stuck with no progress. fileciteturn45file0turn46file0turn38file0

Moving to streaming for **all** files is better than maintaining two code paths:

- one for small files
- one for large files

A single streamed path is:

- simpler to reason about
- more memory-efficient
- more consistent for users
- easier to test
- and less likely to drift into two subtly different implementations

---

## Non-negotiable behavior

The desired behavior is:

### 1. Stream all files
Every outgoing file should be handled incrementally.

### 2. Retry failed chunk upload every 5 seconds
A failed chunk must not immediately fail the whole transfer.

### 3. Give up after 2 minutes of continuous failure for the same chunk
If the chunk cannot be uploaded successfully for 2 straight minutes, the transfer fails.

### 4. Mark the transfer as `FAILED`
When the retry window is exhausted, the user should see a real failure state.

### 5. Preserve the existing protocol
The first implementation should keep the current server protocol unchanged.

---

## Current protocol constraint

The current server `initTransfer(...)` flow requires the sender to provide `chunk_count` before chunk upload begins. That is already visible in the Android worker path and the server transfer init behavior. fileciteturn45file0turn15file0

That means a streamed sender still needs to know the total number of chunks before calling `initTransfer(...)`.

This is the one important design constraint.

---

## Required upload model

The upload flow should become:

1. load transfer row
2. validate prefs, recipient pairing, auth token
3. set status to `PREPARING`
4. open the source URI
5. determine `fileSize`
6. compute `chunkCount = ceil(fileSize / CHUNK_SIZE)`
7. generate `base_nonce`
8. build encrypted metadata
9. call `initTransfer(...)`
10. set status to `UPLOADING`
11. loop over plaintext chunks:
    - read next chunk from stream
    - encrypt it
    - upload it
    - if upload fails, retry every 5 seconds
    - if continuous failure reaches 2 minutes, fail transfer
    - if upload succeeds, update progress and continue
12. when the last chunk succeeds:
    - set `transferId`
    - clear upload progress fields if desired
    - set status to `COMPLETE`

This preserves the current protocol while fixing the architecture.

---

## Recommended transfer states

The upload state model should be:

- `QUEUED`
- `PREPARING`
- `UPLOADING`
- `COMPLETE`
- `FAILED`

### Why `PREPARING` is still needed
Even with streaming, there is still a real pre-upload phase:

- opening the URI
- reading file metadata
- determining chunk count
- generating encrypted metadata
- creating the server-side transfer

That phase should not be mislabeled as upload.

---

## Chunk retry policy

This is the exact policy requested.

## Rule
For each chunk:

- try upload
- if it fails, wait 5 seconds
- try again
- continue until either:
  - the chunk succeeds
  - or the chunk has been failing continuously for 2 minutes

If the chunk fails continuously for 2 minutes:
- stop the transfer
- set status to `FAILED`
- record which chunk failed
- do not silently keep retrying forever

---

## Important interpretation of “fails straight for 2 minutes”

This should mean:

- the 2-minute failure window belongs to the **current chunk**
- the failure timer resets as soon as that chunk uploads successfully
- then the next chunk starts with a fresh retry window

So:

- chunk 7 fails for 70 seconds, then succeeds -> continue normally
- chunk 8 fails for 125 seconds continuously -> fail the transfer

This is the cleanest interpretation.

---

## Recommended retry algorithm

For a given chunk:

### Variables
- `chunkStartFailureTime = null`
- `retryDelayMs = 5000`
- `maxContinuousFailureMs = 120000`

### Behavior
- on first failure, set `chunkStartFailureTime = now`
- on each next failure:
  - if `now - chunkStartFailureTime >= 120000`, fail transfer
  - otherwise wait 5000 ms and retry
- on success:
  - clear `chunkStartFailureTime`
  - move to next chunk

This is simple and deterministic.

---

## Recommended worker behavior

## Before upload starts
The worker should:
- load DB row
- load recipient symmetric key
- open URI
- determine file size
- compute chunk count
- generate metadata
- call `initTransfer(...)`

Only after successful init:
- set status to `UPLOADING`

This prevents the current misleading “uploading before upload exists” behavior.

---

## During upload
For each chunk:
- read plaintext chunk directly from the input stream
- encrypt only that chunk
- upload only that chunk
- update DB progress immediately after success

That means progress starts after the first successful chunk instead of after the entire file has been preprocessed.

---

## On chunk failure
When `uploadChunk(...)` fails:
- log the chunk index
- log the elapsed continuous failure time
- wait 5 seconds
- retry
- after 2 minutes, stop and mark transfer failed

The worker should not rely on “retry the whole job later” as the main mechanism for chunk upload robustness.

---

## On final failure
If a chunk reaches 2 minutes of continuous failure:
- update DB status to `FAILED`
- store a useful error message such as:
  - `Chunk 8/125 failed for 120s`
- return failure from the worker

The failure should be visible and explicit.

---

## On success
After the last chunk uploads successfully:
- store `transferId`
- move status to `COMPLETE`
- let delivery tracking remain exactly as it is today

This preserves the current sender -> relay -> recipient lifecycle.

---

## Handling chunk count up front

Because the current protocol needs `chunk_count` before upload starts, the sender must know `fileSize`.

## Preferred approach
Use the already captured `sizeBytes` from the queued transfer when it is valid.

That is already part of the Android transfer row. fileciteturn44file0turn38file0

Then compute:

`chunkCount = max(1, ceil(sizeBytes / CHUNK_SIZE))`

---

## If file size is unknown
This is the one real complication in a “stream all files” design under the current protocol.

If the source URI does not provide a usable size, the sender has three options:

### Option A — fail clearly
Reject the upload with a useful error such as:
- `Cannot determine file size for streamed upload`

### Option B — spool to temp file first
Copy the source stream to a temp file, determine real size, then stream from that temp file.

### Option C — change protocol later
Allow unknown chunk count up front and finalize later.

### Recommendation
For the first implementation, use:

- **known size -> stream directly**
- **unknown size -> spool to temp file, then stream**

That keeps protocol compatibility while still supporting more URI types.

Do **not** reintroduce full in-memory buffering as the fallback.

---

## Required code changes

## 1. Add `PREPARING` state
Update:
- enum
- DB converters
- UI rendering
- worker logic

Current transfer states are too coarse for honest upload UX. fileciteturn38file0

---

## 2. Replace full-buffer encryption helper for uploads
The current Android helper reads the entire file and creates all encrypted chunks before upload. That path should no longer be the normal upload path. fileciteturn46file0

Instead, introduce a streamed upload helper that:
- generates metadata once
- reads one chunk at a time
- encrypts one chunk at a time
- uploads one chunk at a time

The existing nonce derivation and chunk encryption semantics must remain unchanged. fileciteturn47file0

---

## 3. Refactor `UploadWorker`
The worker should be rewritten so it no longer depends on:

- `encryptFileToChunks(...)` returning a whole encrypted chunk list

Instead it should:
- create encrypted metadata first
- initialize server transfer
- stream chunks directly to `uploadChunk(...)`

Current `UploadWorker` is the main place that needs restructuring. fileciteturn45file0

---

## 4. Add chunk-level retry loop
The worker should own the retry timer for each chunk.

Do not make the primary retry mechanism “WorkManager retries the whole job”.

That is too coarse once streaming exists.

---

## 5. Improve progress updates
The DB progress update should happen after each successful uploaded chunk:

- `uploaded = index + 1`
- `total = chunkCount`

That part already exists conceptually and should stay, but now it will start much earlier and behave honestly. fileciteturn45file0turn38file0

---

## 6. Improve failure messages
Marking a transfer `FAILED` should include useful context.

Recommended examples:
- `Failed to open source URI`
- `Failed to initialize transfer on server`
- `Chunk 14/125 failed continuously for 120s`
- `Upload cancelled`
- `Cannot determine source file size`

These messages should be actionable.

---

## Suggested implementation sequence

### Commit 1
`refactor(android-upload): add PREPARING state`

### Commit 2
`refactor(android-upload): split preparation from upload execution`

### Commit 3
`refactor(android-upload): add streamed chunk reader`

### Commit 4
`refactor(android-upload): add streamed chunk encryption path`

### Commit 5
`refactor(android-upload): upload chunks incrementally`

### Commit 6
`refactor(android-upload): add per-chunk retry window (5s retry, 120s fail)`

### Commit 7
`refactor(android-upload): improve failure reporting and UI state display`

---

## What should not change in the first implementation

This first implementation should **not** change:

- server protocol
- chunk size
- metadata schema
- nonce derivation
- AES-GCM format
- delivery tracking
- desktop receiver behavior

This is an Android sender refactor only.

---

## Acceptance criteria

The implementation is successful if all of the following are true:

### 1. All files use the streamed upload path
No normal outgoing file upload relies on whole-file in-memory buffering.

### 2. Transfers no longer appear stuck in fake `UPLOADING`
Users see `PREPARING` before real upload starts.

### 3. Progress starts after first uploaded chunk
Visible progress begins as soon as actual network upload succeeds for chunk 1.

### 4. Failed chunk retry matches requested behavior
A failed chunk retries every 5 seconds and gives up after 2 minutes of continuous failure.

### 5. A failed transfer becomes explicitly `FAILED`
No indefinite pseudo-stuck state remains.

### 6. Server compatibility is preserved
The existing relay and desktop client continue to work unchanged.

---

## Test plan

## Small file
- 1 MB file uploads successfully
- state transitions are correct
- no behavior regression

## Medium file
- 25 MB file uploads successfully
- chunk progress is visible
- no full-buffer behavior remains

## Large file
- 250 MB file uploads successfully
- progress appears incrementally
- memory usage remains stable enough
- no long fake “uploading” stall before first chunk

## Failure simulation
- simulate temporary network failure during chunk upload
- verify retries every 5 seconds
- verify eventual success if network returns before 2 minutes

## Continuous failure simulation
- force a chunk to fail for >2 minutes
- verify transfer becomes `FAILED`
- verify useful error message is stored

## Unknown-size URI case
- if supported via temp file fallback, verify it works
- if unsupported in first pass, verify it fails clearly and honestly

---

## Risks

### 1. Unknown-size URIs
This is the main protocol-related complication because chunk count is required up front.

### 2. Retry loop must not accidentally duplicate chunk indexes
Chunk retry must re-upload the same chunk index only until success.

### 3. Stream handling must close resources correctly
Input streams must always be closed cleanly on success and failure.

### 4. WorkManager interaction
Top-level worker retry behavior should not fight with the new chunk-level retry logic.

The worker should probably use chunk-level retry as the primary resilience mechanism and return failure cleanly when the retry window is exhausted.

---

## Final recommendation

Implement exactly this model:

- stream every file
- add `PREPARING`
- compute chunk count up front
- initialize transfer
- read -> encrypt -> upload one chunk at a time
- retry failed chunk every 5 seconds
- after 2 minutes of continuous failure for that chunk, mark transfer `FAILED`

That is the cleanest fix and the right long-term upload architecture for the Android client.
