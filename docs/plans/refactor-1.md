# refactor-1.md

> **Status: Done** — landed on `main` in commits `e2a14b3`..`4df60f3` (7 commits: baseline test assertions, then TransferStatusService, TransferNotifyService, TransferWakeService, TransferCleanupService, TransferService, controller finalization). Wire protocol unchanged; Android and desktop clients untouched. `TransferController` went from ~480 lines to 75 as a pure HTTP adapter.

## Refactor 1 / 10
# Split the transfer domain into smaller server services without changing the protocol

## Why this is the first refactor

This is the highest-value first refactor because it targets the part of the system that already has the highest concentration of responsibilities and where every future change will become more expensive.

The current `TransferController` handles all of the following at once:

- transfer initialization,
- chunk upload,
- reading pending transfers,
- chunk download,
- ACK,
- delivery status,
- long polling,
- FCM wake,
- cleanup.

That is functionally fine, but architecturally it is already too many roles in one class.

The goal of this refactor is therefore to:

- **reduce the cost of future changes,**
- **separate responsibilities,**
- **keep the current API and protocol unchanged,**
- **prepare the server for further extension without continuing to inflate the controller.**

---

## Position within the full sequence of 10 refactors

This is the planned order of the next refactors by value:

1. **Split the transfer domain into smaller server services without changing the protocol**
2. Introduce an explicit server layer for auth, request context, and input validation
3. Introduce a repository layer above SQLite access
4. Formalize internal transfer states and state transitions
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. Separate desktop core from Linux-specific backends
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 1**.

---

## Core principle of this refactor

### This is not a protocol change
This refactor **must not change**:

- endpoints,
- request/response formats,
- field meanings,
- behavior timing observable by clients,
- delivery semantics,
- long-poll semantics,
- FCM wake semantics.

In other words:

**`protocol.md` remains authoritative and unchanged.**  
Only the internal structure of the server implementation changes.

---

## Main goal

Move the server from this shape:

- controller as a mix of route-level logic,
- business logic,
- persistence,
- cleanup,
- delivery-state computation,
- FCM wake behavior,

to this shape:

- controller = thin HTTP layer,
- services = business logic,
- repository/helper layers = DB and storage access,
- separate objects/services for long and risky flows.

---

## What should be introduced

### 1. Thin `TransferController`
After this refactor, the controller should only handle:

- reading input from the HTTP layer,
- basic mapping of parameters,
- calling the appropriate service,
- translating the result into an HTTP response.

The controller should no longer be the place where the whole domain is implemented.

---

### 2. `TransferService`
The main domain service for the regular transfer lifecycle.

It should handle:

- transfer init,
- chunk-count validation,
- duplicate transfer ID detection,
- storage limits,
- upload progress,
- marking transfer as complete,
- pending transfer listing,
- ACK completion.

---

### 3. `TransferChunkService`
A dedicated service for chunk handling.

It should handle:

- storing chunks to storage,
- writing chunk metadata into the DB,
- idempotent chunk upload,
- loading chunks for download,
- deleting chunks and storage directories.

This separates chunk storage logic from transfer orchestration.

---

### 4. `TransferStatusService`
A dedicated service for delivery and sent-status views.

It should handle:

- computing `status`,
- computing `delivery_state`,
- exposing `chunks_downloaded`,
- building responses for `/api/transfers/sent-status`,
- generating inline `sent_status` for long poll responses.

This is important because status logic is sensitive and should exist in exactly one place.

---

### 5. `TransferNotifyService`
A dedicated service for long polling.

It should handle:

- timeout logic,
- the polling loop,
- detection of `pending`,
- detection of `delivered`,
- detection of `download_progress`,
- building the response for `/api/transfers/notify`.

This is its own responsibility and should not remain embedded inside a general-purpose controller.

---

### 6. `TransferCleanupService`
A dedicated service for expiration and cleanup of old or incomplete transfers.

It should handle:

- cleanup of expired transfers,
- cleanup of incomplete transfers,
- deletion of chunk records,
- deletion of files,
- deletion of pairing requests if that remains in the same area.

---

### 7. `TransferWakeService`
A dedicated service for FCM wake after upload completion.

It should handle:

- finding the recipient token,
- deciding whether FCM is available,
- sending the wake notification,
- preserving the rule that FCM failure must never break the transfer flow.

---

## Proposed target structure

A possible target structure on the server side:

```text
server/src/
  Controllers/
    TransferController.php

  Services/
    TransferService.php
    TransferChunkService.php
    TransferStatusService.php
    TransferNotifyService.php
    TransferCleanupService.php
    TransferWakeService.php

  Repositories/
    TransferRepository.php
    ChunkRepository.php
    PairingRepository.php
    DeviceRepository.php

  Storage/
    ChunkStorage.php
```

It is not necessary to introduce all of this in the first iteration, but this is the target direction.

---

## Minimal first-iteration version

To avoid making the refactor unnecessarily large, the first iteration should stay disciplined.

### Required in the first iteration
At minimum, the following should be introduced:

- `TransferService`
- `TransferStatusService`
- `TransferNotifyService`
- `TransferCleanupService`
- `TransferWakeService`

And `TransferController` should be reduced so that it no longer contains the main domain logic.

### Optional for a later step
These can be deferred if the refactor grows too large:

- a full repository layer,
- full chunk-storage separation into `ChunkStorage`,
- complete normalization of all helper methods.

---

## What should not change

This refactor **should not** address:

- changes to `protocol.md`,
- new API endpoints,
- new response fields,
- changes to state names,
- changes to FCM behavior,
- schema changes without a strong reason,
- client-side behavior changes,
- performance optimization as the primary goal,
- rewriting the server in another language.

This is purely a **structural server refactor**.

---

## Concrete execution plan

## Phase 1 — extract status logic
First separate the most consistency-sensitive logic:

- logic for `sent-status`,
- mapping of `downloaded`, `complete`, and `chunks_downloaded`,
- computation of `delivery_state`,
- generation of inline `sent_status` for long poll.

This should live in one service so that two nearly identical implementations do not exist.

### Result
- the controller just calls `TransferStatusService`
- `/api/transfers/sent-status` and inline status from `/api/transfers/notify` share the same computation

---

## Phase 2 — extract long polling
Next separate the blocking `notify` logic into its own service.

### Goal
So that `notify` is no longer procedural code embedded in a controller, but a separately readable flow with inputs:

- `deviceId`
- `since`
- `isTest`

and outputs:

- `pending`
- `delivered`
- `download_progress`
- optionally `sent_status`

---

## Phase 3 — extract wake logic
FCM wake should live outside the controller.

### Reason
It is a side effect, not part of the HTTP layer.

That means:
- upload flow marks the transfer complete,
- it calls the wake service,
- the wake service may fail or succeed,
- but it does not break the business flow.

---

## Phase 4 — extract cleanup
Cleanup already exists, but is hidden as an internal helper.

### Goal
Turn it into an explicit service with a clear responsibility:
- expiry policy,
- incomplete-expiry policy,
- deletion of storage artifacts.

That improves readability and future retention-policy changes.

---

## Phase 5 — extract main transfer orchestration
Finally move the main init/upload/download/ack orchestration into `TransferService`.

This will be the most demanding part, which is why it should happen only after the specialized areas have been extracted.

---

## Recommended commit order

### Commit 1
`refactor(server): extract TransferStatusService from TransferController`

Contents:
- new `TransferStatusService`
- unified sent-status computation
- no API change

### Commit 2
`refactor(server): extract TransferNotifyService for long-poll flow`

Contents:
- new `TransferNotifyService`
- notify loop and response building moved out
- controller only forwards inputs

### Commit 3
`refactor(server): extract TransferWakeService`

Contents:
- FCM wake outside the controller
- fire-and-forget semantics preserved

### Commit 4
`refactor(server): extract TransferCleanupService`

Contents:
- cleanup policy moved into its own service
- random cleanup trigger in pending flow preserved

### Commit 5
`refactor(server): extract TransferService orchestration`

Contents:
- init/upload/download/ack orchestration moved out
- controller remains thin

### Commit 6
`refactor(server): simplify TransferController to HTTP adapter only`

Contents:
- controller without domain logic
- readable route -> service mapping

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. The protocol has not changed
- same endpoints,
- same request/response shapes,
- same state meanings,
- same auth behavior.

### 2. Clients work unchanged
- the Android client requires no modification,
- the desktop client requires no modification.

### 3. `TransferController` is significantly smaller
- the controller does not contain the main domain logic,
- it is readable as a route adapter.

### 4. Status logic exists in one place only
- there are not two independent implementations of delivery-state mapping.

### 5. Long poll is isolated
- the notify flow has its own service and can be read independently.

### 6. Side effects are separated
- FCM wake is not glued into the controller,
- cleanup is not glued into the controller.

---

## Test checklist

After each major phase, verify manually or automatically:

### Registration and pairing
- device registration still works the same,
- pairing still works the same,
- verification-code flow still behaves the same.

### Upload/download
- transfer init works,
- chunk upload works,
- transfer is marked complete,
- the recipient sees the pending transfer,
- chunk download works,
- ACK deletes chunks,
- delivered state propagates correctly.

### Delivery status
- `not_started`
- `in_progress`
- `delivered`

still evaluate exactly as before the refactor.

### Long poll
- `?test=1` still works,
- pending events wake the client,
- delivery events wake the client,
- download-progress events wake the client,
- inline `sent_status` matches the standard status endpoint.

### FCM wake
- wake is still sent when a transfer becomes complete,
- FCM failure does not break upload flow.

### Cleanup
- old transfers are cleaned up the same way,
- incomplete transfers are cleaned up the same way,
- storage artifacts are removed the same way.

---

## Risks

### 1. Silent change of status semantics
The biggest risk is accidentally changing the meaning of:
- `pending`,
- `delivered`,
- `delivery_state`,
- `chunks_downloaded`.

That is why status logic should be extracted first and checked very carefully.

### 2. Divergence between `/sent-status` and inline `sent_status`
Today these two paths should mean the same thing.  
This refactor must not create two different variants.

### 3. Breaking long-poll behavior
Because `notify` is time-sensitive and blocking, it must be extracted carefully and without changing observable behavior.

### 4. Scope growth
This can easily turn into an oversized rewrite.  
The scope must stay strict:
- no protocol change,
- no client change,
- no forced repository architecture everywhere.

---

## What the benefit will be after completion

Once this refactor is done, it will become easier to:

- add new transfer-related features,
- change retention rules,
- extend long poll behavior,
- audit delivery-status logic,
- test server behavior in smaller pieces,
- avoid `TransferController` becoming an unmaintainable center of everything.

That is why this refactor is first.

---

## Practical definition of done

Done does not mean:
- "the code looks nicer".

Done means:

- `TransferController` is small and readable,
- business logic lives in services,
- the protocol is unchanged,
- clients do not notice anything,
- future server changes are cheaper than they are today.

---

## Note about the next step

After this refactor, the next one should be:

**introduce an explicit server layer for auth, context, and request validation**

But only after the transfer domain has been pulled out of the controller.  
There is no point tightening the request pipeline first when the main structural problem still lives inside the transfer flow.
