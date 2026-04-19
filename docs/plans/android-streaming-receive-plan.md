# android-streaming-receive-plan.md

**Status: COMPLETED (2026-04-18).**
Implemented by commits `243ae78` (split `.fn.*` from normal file
receive), `62d61b2` (stream chunks to temp file, finalize by rename),
`9ae09d5` (keep file on ACK failure after durable write), and
`430f911` (move partials to `DesktopConnector/.parts/`).
`receiveFileTransfer` now streams each chunk directly to
`DesktopConnector/.parts/.incoming_<id>.part` and atomic-renames on
finalize. `.fn.*` transfers keep the tiny in-memory path. The optional
`RECEIVING`/`FINALIZING` state split was deferred — the UI already
renders the current `UPLOADING+INCOMING` combination honestly as
"Downloading X/Y".

## Purpose

This document describes the robust long-term solution for **incoming file transfers on Android**.

It focuses on the Android receiver path and solves the current problem that large files are handled in a memory-heavy way instead of being streamed safely to disk.

The goal is to make incoming transfers:

- memory-efficient
- safer for large files
- more failure-tolerant
- more resumable where practical
- and more honest in terms of delivery semantics

This plan is designed to preserve the existing protocol in its first implementation step.

---

## Problem summary

The current Android receive path downloads all encrypted chunks into memory, then decrypts all chunks into memory, then concatenates the final plaintext into one large in-memory byte array, and only then saves the file. fileciteturn37file0

That means for a large incoming file, the receiver may hold:

- all encrypted chunks
- all decrypted chunks
- the final merged plaintext
- and extra temporary copies during concatenation

This is a structurally weak design for large files.

It is better than the current sender path in one respect:

- the receiver at least updates visible progress while downloading chunks fileciteturn37file0

But in terms of memory usage and large-file robustness, it is still not a good long-term solution.

---

## Main conclusion

The correct long-term receive architecture is:

**download one chunk -> decrypt one chunk -> append it to a temp file -> update progress -> continue**

Not:

**download all chunks -> decrypt all chunks -> build full file in memory -> save at the end**

That is the core design change.

---

## Current receive lifecycle

At a high level, the current Android receive path does this:

1. desktop sender uploads encrypted chunks to relay
2. Android `PollService` sees a pending transfer
3. Android decrypts metadata
4. Android downloads each chunk
5. Android stores every chunk in `chunks: MutableList<ByteArray>`
6. Android decrypts every chunk into `plainParts: MutableList<ByteArray>`
7. Android concatenates all plaintext into one `data` byte array
8. Android saves the resulting file to disk
9. Android sends ACK to the server
10. Android finalizes history/notification state fileciteturn37file0

This is workable for smaller files, but not robust for large files.

---

## What makes the current design fragile

### 1. It stores the whole transfer in memory
That is the central problem.

### 2. It performs expensive repeated byte-array copying
The final plaintext is built using repeated byte-array concatenation, which is especially wasteful for large files. fileciteturn37file0

### 3. It ties decryption and persistence too late
A successfully downloaded and decrypted chunk is still not durably stored until the whole transfer is assembled.

### 4. It makes large-file failure more painful
If the process dies late in the transfer, all that in-memory work is effectively lost.

### 5. It does not create a strong partial-transfer recovery story
There is progress in the DB, but the actual file assembly model is still all-at-end.

---

## Primary design goals

The robust receive solution should achieve all of the following:

### 1. Stream to disk
The receiver should write decrypted data incrementally to disk.

### 2. Minimize memory usage
At any given time, the receiver should ideally hold only:
- current encrypted chunk
- current decrypted chunk
- small local metadata/state

### 3. Preserve delivery correctness
The server ACK must still mean:
- Android has successfully and durably received the transfer

### 4. Avoid partial-file corruption
If a transfer fails midway, the user should not end up with a misleading final file that looks complete.

### 5. Preserve current protocol initially
The first version should work with the current relay/server behavior.

### 6. Prepare for better resume behavior later
The new structure should make resume/recovery easier, even if full resumable receive is not implemented in the first pass.

---

## Recommended target lifecycle

The receive lifecycle should become:

1. detect pending transfer
2. decrypt metadata
3. allocate a temp file path
4. mark transfer as receiving/downloading
5. for each chunk:
   - download chunk
   - decrypt chunk
   - append plaintext directly to temp file
   - flush/update progress
6. when all chunks are written successfully:
   - finalize file safely
   - send ACK
   - mark transfer complete
   - show notification / update history

This is the robust baseline.

---

## Core architectural change

## Streamed write-to-temp-file model

The receiver should no longer collect the whole transfer in memory.

Instead, for each chunk:

- download encrypted blob
- decrypt it immediately
- append the plaintext bytes directly to a temp file
- release per-chunk memory
- continue with the next chunk

This turns memory usage from “grows with file size” into “roughly bounded by chunk size.”

That is the most important improvement.

---

## Temp-file-first rule

The receiver should never write directly to the final user-visible file path while the transfer is incomplete.

Instead it should:

- create a temp file in a controlled location
- write all decrypted chunks there
- only once the transfer is fully written and validated, move/rename it into the final destination

This protects users from incomplete files being mistaken for completed ones.

### Recommended temp naming
Examples:
- `.incoming_<transferId>.part`
- or similar hidden/partial naming convention

The exact name is less important than the behavior.

---

## ACK timing rule

This is critical.

The ACK should still be sent **only after** all of the following are true:

1. all chunks were downloaded successfully
2. all chunks were decrypted successfully
3. all plaintext was written successfully
4. the temp file was finalized successfully into the final destination

Only then should Android send the server ACK.

That preserves the current meaning of ACK as final delivery confirmation.

The current implementation already ACKs only after data handling is done, and that behavior should remain conceptually correct. fileciteturn37file0

---

## Recommended new receive states

The existing status model is very coarse, and incoming transfers reuse `UPLOADING` in a slightly awkward way even though they are downloads. fileciteturn38file0turn37file0

A more robust and honest model would be:

- `QUEUED`
- `PREPARING`
- `RECEIVING`
- `FINALIZING`
- `COMPLETE`
- `FAILED`

### Meaning

#### `PREPARING`
Metadata decrypted, output path prepared, temp file allocated.

#### `RECEIVING`
Chunks are actively being downloaded/decrypted/written.

#### `FINALIZING`
All chunks are written, final rename/move/scan/ACK is in progress.

This is more honest than collapsing everything into one state.

---

## Recommended file-write model

## Step 1 — choose output destination
Determine:
- final user-visible filename
- final target directory
- actual collision-safe final path

This should happen early, but the actual write target should still be the temp file.

## Step 2 — create temp file
Create a temp file in the same directory if possible.

### Why
Rename/move becomes safer and more atomic when temp and final file are on the same filesystem.

## Step 3 — append decrypted chunks to temp file
Open the temp file once and append each plaintext chunk in order.

### Recommended implementation detail
Use a single output stream for the duration of the transfer rather than opening/closing the file for every chunk unless there is a clear reason not to.

## Step 4 — flush and close cleanly
After the final chunk:
- flush
- close
- ensure output stream is fully completed

## Step 5 — finalize
Move/rename temp file to final destination.

If media scanning is needed, do it after finalization.

---

## Recommended chunk handling model

Each chunk should follow this pipeline:

1. request chunk from relay
2. if request fails, retry according to receive retry policy
3. decrypt chunk immediately
4. if decryption fails, fail transfer
5. append plaintext to temp file
6. update DB progress
7. continue

At no point should previously completed chunks remain accumulated in memory.

---

## Receive retry policy

The current receiver retries chunk download up to 3 times with increasing delay, then fails. fileciteturn37file0

That is a reasonable simple baseline, but the more robust architecture should define the retry policy explicitly.

## Recommended immediate policy
Keep a simple per-chunk retry model first:

- try chunk
- retry failed chunk
- if the chunk still cannot be downloaded after the defined retry window, fail the transfer

This can remain simple in the first streamed implementation.

## Optional stronger later policy
Later, align receive retries more closely with the sender retry model:
- fixed retry interval
- maximum continuous failure window
- clearer logging of stalled chunks

This is useful, but not required for the first robust streaming receive pass.

---

## Partial-transfer cleanup policy

A robust receiver must decide what happens when the transfer fails halfway through.

## Required behavior
If the transfer fails before completion:

- do not leave the partial file under the final visible filename
- either:
  - delete the temp file
  - or keep it as an explicit partial file only if resume support is intentionally planned

### Recommendation for first implementation
Delete the temp file on failure unless you are explicitly implementing resume support in the same phase.

This keeps user-visible behavior cleaner.

---

## Resume support strategy

The robust architecture should be built so that resumable receive becomes possible later, even if it is not fully implemented now.

### Why
Once chunks are written incrementally to a temp file, it becomes much easier to imagine later support for:
- resuming from chunk N
- verifying already-written length
- continuing the same temp file

### Recommendation
For the first pass:
- structure the code so resume would be possible later
- but do not overcomplicate the first implementation if resume is not required immediately

The first goal is robust streaming, not full resumable download.

---

## Required code changes

## 1. Replace in-memory chunk accumulation
Remove the pattern of:

- `chunks: MutableList<ByteArray>`
- `plainParts: MutableList<ByteArray>`
- final `ByteArray` concatenation

for normal file receive flow.

This is the central fix. fileciteturn37file0

---

## 2. Introduce temp-file writer
Add a helper that:
- creates temp file
- appends plaintext chunk data
- finalizes the file on success
- deletes or cleans up on failure

This helper should own the persistence behavior cleanly.

---

## 3. Restructure `handleIncomingTransferInner(...)`
The current receive function should be refactored into clear phases:

### Phase A — metadata + setup
- decrypt metadata
- choose final path
- allocate temp path
- insert or reuse DB row

### Phase B — streamed chunk loop
- download chunk
- decrypt chunk
- write chunk
- update progress

### Phase C — finalization
- finalize temp file
- ACK transfer
- update DB to complete
- notify user

This will make the receive path much easier to reason about.

---

## 4. Improve receive statuses
Introduce or at least plan for:
- `PREPARING`
- `RECEIVING`
- `FINALIZING`

This is not the deepest technical fix, but it makes the UI and debugging much more honest.

---

## 5. Improve failure reporting
Failure states should distinguish:
- chunk download failed
- chunk decrypt failed
- temp file write failed
- finalization failed
- ACK failed after successful write

The last one is especially important because ACK failure after successful file write is a different kind of problem than download failure.

---

## Recommended robust ACK/finalization order

The safest general order is:

1. finish writing temp file
2. close output stream
3. move temp file to final destination
4. update any media scan/indexing if needed
5. send ACK
6. mark transfer complete in DB
7. show success notification

This keeps the meaning of success and delivery strong.

### Important note
If ACK fails after final file write succeeds, you must decide how to represent that.

Recommended first behavior:
- keep the file
- mark transfer as locally complete but sync/ack uncertain
- log it clearly
- optionally retry ACK in a later improvement

Do not delete a fully received file just because ACK failed.

---

## Special handling for `.fn.*` transfers

Command-style `.fn.*` transfers are different from normal file transfers. The current implementation handles them directly in memory and executes their behavior after decrypting the payload. fileciteturn37file0

That is acceptable because those payloads are tiny by design.

### Recommendation
Do **not** force the exact same temp-file persistence path for tiny `.fn.*` command transfers.

Instead:
- keep command-style transfers on a lighter path
- but keep normal files on the robust streamed path

This distinction is reasonable because the problem is large binary payload handling, not tiny command payloads.

---

## Suggested implementation phases

## Phase 1 — separate command transfers from normal file transfers
Clarify the receive code path so `.fn.*` and normal file transfers are handled through different internal branches.

### Goal
Avoid overcomplicating tiny command receives while cleaning up normal file persistence.

---

## Phase 2 — add temp-file writer and finalizer
Create a dedicated write/finalize helper.

### Goal
Move file persistence out of the large receive function and make it explicit.

---

## Phase 3 — replace chunk accumulation with streamed write
For normal files:
- download one chunk
- decrypt one chunk
- write one chunk
- update progress

### Goal
Eliminate whole-transfer buffering.

---

## Phase 4 — improve status model
Introduce:
- `PREPARING`
- `RECEIVING`
- `FINALIZING`

### Goal
Make UI and logs reflect reality better.

---

## Phase 5 — improve failure and cleanup behavior
Add clear handling for:
- write failure
- finalization failure
- ACK failure after durable write

### Goal
Make the receive path operationally robust rather than merely memory-improved.

---

## Phase 6 — optional groundwork for future resume
If desired, add enough structure that resume can be added later without redesigning the receive path again.

---

## Suggested commit sequence

### Commit 1
`refactor(android-recv): split .fn transfer handling from normal file receive path`

### Commit 2
`refactor(android-recv): add temp-file writer and finalization helper`

### Commit 3
`refactor(android-recv): replace in-memory chunk accumulation with streamed write-to-disk`

### Commit 4
`refactor(android-recv): add PREPARING / RECEIVING / FINALIZING states`

### Commit 5
`refactor(android-recv): improve failure handling and temp-file cleanup`

### Commit 6
`refactor(android-recv): clarify ACK timing and post-write success semantics`

### Commit 7
`refactor(android-recv): prepare structure for future resumable receive`

---

## What should not change in the first implementation

The first robust receive implementation should **not** require:

- protocol changes
- relay server changes
- desktop sender changes
- chunk format changes
- AES-GCM changes
- metadata format changes
- chunk size changes

This should be an Android receiver architecture refactor only.

---

## Acceptance criteria

The implementation is successful if all of the following are true:

### 1. Normal file receives are streamed to disk
The receiver no longer stores the whole encrypted and decrypted file contents in memory before saving.

### 2. Memory usage is roughly bounded by chunk size
Large incoming files no longer cause memory growth proportional to full file size in the same way as before.

### 3. Partial transfers do not produce misleading final files
Failed receives do not leave a normal-looking completed file in the destination folder.

### 4. ACK still means real successful receipt
ACK is sent only after the file is durably written and finalized.

### 5. Existing server and sender remain compatible
No protocol or server change is required.

### 6. `.fn.*` command transfers still work correctly
Small command transfers continue to behave correctly.

---

## Test plan

## Small file
- a small incoming file still works exactly as before
- no regression in notification/history behavior

## Medium file
- a medium file receives successfully
- progress behaves as expected
- memory usage remains stable

## Large file
- a 250 MB file receives successfully
- no large in-memory accumulation occurs
- final file is correct

## Failure while downloading
- chunk retry/failure path works
- no misleading final file appears
- transfer becomes `FAILED`

## Failure while writing
- temp file is cleaned up or handled correctly
- transfer becomes `FAILED`

## Failure after final write but before ACK
- final file remains safe
- state/logging makes the situation diagnosable

## `.fn.*` transfer
- clipboard text still works
- clipboard image still works
- unpair still works

---

## Risks

### 1. Finalization semantics must stay correct
Moving ACK timing carelessly could change delivery meaning.

### 2. Temp-file cleanup must be reliable
Otherwise failed transfers may leave clutter or confusing partial artifacts.

### 3. `.fn.*` transfers should not be overcomplicated
The robust file path should solve the large-file problem, not burden tiny command payloads with unnecessary machinery.

### 4. Future resume behavior may tempt over-design
The first step should be robust streamed receive, not an elaborate resume engine.

---

## Recommended final direction

The robust receive solution should be:

- streamed
- temp-file-based
- ACK-after-finalization
- memory-bounded
- protocol-compatible
- ready for future resume, but not blocked by it

That is the correct long-term architecture for large-file receive on Android.

---

## Final conclusion

Yes, the Android receiver has the same core architectural weakness as the sender in one important sense:

**it handles the entire file in memory instead of streaming it safely.** fileciteturn37file0

The robust solution is to refactor normal file receive into:

- download chunk
- decrypt chunk
- write chunk to temp file
- update progress
- finalize
- ACK

while leaving tiny `.fn.*` command transfers on a simpler path.

That gives the best combination of:

- correctness
- large-file safety
- protocol compatibility
- and future maintainability
