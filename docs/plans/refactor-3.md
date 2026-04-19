# refactor-3.md

> **Status: Done** — landed on `main` in commits `b9d8cef`..`f28917c` (6 commits:
> DeviceRepository, PairingRepository, TransferRepository, ChunkRepository,
> FasttrackRepository, PingRateRepository). New `server/src/Repositories/` owns
> all SQL touching devices, pairings/pairing_requests, transfers, chunks,
> fasttrack_messages, and ping_rate. Every service and controller now expresses
> intent through repository methods; zero raw `$db->query*` / `$db->execute`
> calls remain outside `Repositories/` and `Database.php`. The atomic UPSERT in
> `PingRateRepository::tryClaimCooldown` preserves the `INSERT ... ON CONFLICT
> ... WHERE cooldown_until <= :now` shape and its `$db->changes() === 1`
> semantics verbatim. Dashboard's correlated subquery split into
> `TransferRepository::listPendingForDashboard` + per-row
> `ChunkRepository::sumChunkBytesForTransfer`. Wire protocol unchanged; Android
> and desktop clients untouched. Verified by `./test_loop.sh` after each commit
> (including ping rate-limit 429, long-poll, sent-status, delivered invariant,
> 10 MB streaming roundtrip). Net across 6 commits: 17 files, +832 / −366 lines.

## Refactor 3 / 10
# Introduce a repository layer above SQLite access

## Why this is the third refactor

After refactor 1 and refactor 2, the next highest-value step is to separate persistence access from business logic.

At the moment, server code still mixes:

- domain decisions,
- SQL queries,
- row-shape assumptions,
- persistence-side effects,
- and HTTP-facing behavior.

This is workable while the codebase is still relatively small, but it becomes expensive as soon as:

- more services start sharing the same data,
- state rules become stricter,
- migrations become more frequent,
- or storage behavior needs to change without changing protocol behavior.

The purpose of this refactor is to introduce a **repository layer** that makes persistence explicit and keeps SQL access out of service orchestration.

---

## Relation to refactor 1 and refactor 2

This refactor depends on the previous two:

- refactor 1 pulls transfer business logic out of overloaded controllers,
- refactor 2 cleans up the HTTP input boundary,
- refactor 3 then makes persistence access a separate concern.

That order matters.

If repositories are introduced too early, they often become just a thin wrapper around SQL while the rest of the architecture is still mixed together.  
At that point they add ceremony without creating real leverage.

After the first two refactors, however, repositories become useful because they sit under cleaner service boundaries.

---

## Position within the full sequence of 10 refactors

The full sequence remains:

1. Split the transfer domain into smaller server services without changing the protocol
2. Introduce an explicit server layer for auth, request context, and input validation
3. **Introduce a repository layer above SQLite access**
4. Formalize internal transfer states and state transitions
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. Separate desktop core from Linux-specific backends
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 3**.

---

## Main goal

Move the server from the current shape:

- services or controllers issuing raw SQL directly,
- row arrays being interpreted in multiple places,
- domain behavior depending on specific SQL fragments,
- DB access logic duplicated across endpoints,

to this shape:

- services express intent,
- repositories own SQL,
- row mapping is centralized,
- persistence access is explicit,
- business logic no longer depends on low-level query details.

---

## What must not change

This refactor **must not change the protocol**.

That means no change to:

- endpoints,
- request/response shapes,
- field meanings,
- authenticated behavior,
- long-poll semantics,
- transfer lifecycle semantics,
- fasttrack semantics,
- FCM wake semantics.

This is an internal persistence refactor only.

It also does **not** require a change of storage engine.  
SQLite remains the current storage backend.

---

## Why repositories are useful here

A repository layer is worth introducing here for four practical reasons:

### 1. SQL should not define domain structure
Business services should decide **what** needs to happen.  
Repositories should decide **how** data is read and written.

Without that separation, domain rules slowly start living inside SQL fragments spread across the codebase.

---

### 2. Row-shape assumptions should exist in one place
Today, multiple parts of the server assume specific row shapes such as:

- device rows,
- transfer rows,
- chunk rows,
- pairing rows,
- fasttrack message rows,
- ping-rate rows.

If those assumptions are duplicated, even a small schema evolution becomes risky.

Repositories centralize those assumptions.

---

### 3. Query reuse becomes safe and explicit
Queries such as:

- find paired device,
- load pending transfers,
- compute delivery status,
- locate chunk path,
- read FCM token,
- update `last_seen_at`,
- check rate limit slot,

will otherwise keep reappearing in slightly different forms.

Repositories allow these operations to be named once and reused.

---

### 4. Storage changes become cheaper later
Even if SQLite remains the backend, future changes may still happen:

- schema evolution,
- indexing changes,
- cleanup strategies,
- mapping improvements,
- or partial replacement of one area of storage behavior.

A repository layer reduces how many higher-level services need to care.

---

## What should be introduced

### 1. Repository classes for the main persistence areas
At minimum, the server should gain repositories for the main aggregates or storage areas.

Recommended initial set:

- `DeviceRepository`
- `PairingRepository`
- `TransferRepository`
- `ChunkRepository`
- `FasttrackRepository`
- `PingRateRepository`

These names do not need to be final, but the separation should reflect actual persistence boundaries.

---

### 2. Explicit persistence methods with domain meaning
Repositories should expose methods with domain meaning, not generic DB utility names.

Good examples:

- `findByDeviceId(...)`
- `findByCredentials(...)`
- `updateLastSeen(...)`
- `findPairing(...)`
- `createPairing(...)`
- `createTransfer(...)`
- `markTransferComplete(...)`
- `markTransferDelivered(...)`
- `listPendingTransfersForRecipient(...)`
- `storeChunk(...)`
- `findChunk(...)`
- `deleteChunksForTransfer(...)`
- `insertFasttrackMessage(...)`
- `listPendingFasttrackMessages(...)`
- `claimPingCooldown(...)`

Bad examples:

- `runTransferQuery(...)`
- `getData(...)`
- `querySomething(...)`

The repository API should describe intent, not implementation vagueness.

---

### 3. Optional lightweight row mappers or record constructors
At first, repositories may still return associative arrays if that is the least risky migration path.

However, row-to-shape conversion should happen inside the repository, not spread across services.

At minimum, repositories should normalize:

- field types,
- integer casting,
- nullable values,
- consistent naming where needed.

Full domain objects are optional for this refactor.  
Consistency is not optional.

---

### 4. Shared transaction boundary strategy
Even if most operations stay simple, repositories should make transaction boundaries easier to reason about.

This does **not** mean introducing a full unit-of-work abstraction immediately.  
It does mean that multi-step write flows should stop depending on unrelated service code manually orchestrating raw SQL.

The first step may simply be:

- make repository methods granular but meaningful,
- identify operations that should later become transactional groups,
- avoid scattering write dependencies across multiple files.

---

## Suggested target structure

A practical target shape:

```text
server/src/
  Repositories/
    DeviceRepository.php
    PairingRepository.php
    TransferRepository.php
    ChunkRepository.php
    FasttrackRepository.php
    PingRateRepository.php

  Services/
    ...

  Database.php
```

Optional later additions:

```text
server/src/
  Repositories/
    Mapping/
      DeviceMapper.php
      TransferMapper.php
      ...
```

This mapping layer is optional in the first iteration.  
The important part is not the number of files but the separation of responsibilities.

---

## Repository boundaries by area

## DeviceRepository
Should own persistence related to:

- device lookup by ID,
- lookup by `(device_id, auth_token)`,
- registration insert,
- `last_seen_at` update,
- FCM token update,
- reading FCM token,
- loading device metadata for stats.

### Should not own:
- ping business decisions,
- authentication policy,
- stats response formatting.

---

## PairingRepository
Should own persistence related to:

- finding a pairing between two devices,
- creating normalized pairings,
- listing pairings for a device,
- writing pairing stats such as bytes transferred / transfer count,
- storing and reading pairing requests,
- marking pairing requests as claimed,
- deleting obsolete pairing requests.

### Should not own:
- verification-code logic,
- pairing UX flow,
- meaning of trusted identity.

---

## TransferRepository
Should own persistence related to:

- transfer creation,
- transfer lookup,
- duplicate transfer detection,
- transfer completion state,
- sender-side and recipient-side transfer queries,
- delivery status queries,
- transfer progress updates,
- delivery acknowledgement state,
- transfer expiry queries.

### Should not own:
- storage path deletion,
- file I/O,
- FCM wake behavior.

---

## ChunkRepository
Should own persistence related to:

- chunk metadata inserts,
- chunk lookup by `(transfer_id, chunk_index)`,
- chunk existence checks,
- chunk-size aggregation,
- deleting chunk records,
- listing chunk rows for a transfer.

### Should not own:
- physical file writing to storage,
- encryption,
- transfer lifecycle decisions.

---

## FasttrackRepository
Should own persistence related to:

- inserting fasttrack messages,
- deleting expired messages,
- counting pending messages,
- listing pending messages,
- deleting acknowledged messages,
- loading message recipient metadata where needed.

### Should not own:
- FCM wake decisions,
- fasttrack command meaning,
- payload interpretation.

---

## PingRateRepository
Should own persistence related to:

- atomic cooldown claim,
- cooldown lookup,
- retry-after calculation input,
- any future cleanup of stale rate-limit rows.

### Should not own:
- liveness semantics,
- online/offline interpretation,
- FCM ping sending.

---

## Main architectural principle

The service layer should say:

- "create a transfer",
- "mark chunk uploaded",
- "load pending transfers",
- "acknowledge delivery",
- "check pair relationship",
- "insert fasttrack message",
- "claim ping slot".

The repository layer should say:

- "here is the SQL and row mapping needed to do that safely."

The service layer should no longer say:

- "SELECT ...",
- "JOIN ...",
- "UPDATE ...",
- "cast this field here",
- "if this row column is missing do X".

---

## What the first iteration should include

To keep the refactor controlled, the first iteration should be practical.

### Required in the first iteration
At minimum, extract repositories for:

- devices,
- pairings,
- transfers,
- chunks.

These four already cover the most important and most reused persistence logic.

### Strongly recommended in the same iteration
Also extract:

- `FasttrackRepository`
- `PingRateRepository`

These are smaller and will complete the persistence separation pattern.

### What can remain simple
The first iteration does **not** need:

- full entity objects,
- ORM-like behavior,
- generic base repository inheritance,
- query builders,
- automatic hydration frameworks,
- repository interfaces for the sake of interfaces.

The aim is to make persistence explicit, not to imitate a framework.

---

## Concrete execution plan

## Phase 1 — extract `DeviceRepository`
Start with devices because auth and stats depend on it heavily.

### Why first
- device lookup patterns are frequent,
- auth depends on them,
- `last_seen_at` is central,
- FCM token access lives there,
- it creates a good pattern for the rest.

### Expected methods
For example:

- `findById(string $deviceId)`
- `findByCredentials(string $deviceId, string $token)`
- `insertDevice(...)`
- `updateLastSeen(string $deviceId, int $now)`
- `updateFcmToken(string $deviceId, ?string $token)`
- `findFcmToken(string $deviceId)`

---

## Phase 2 — extract `PairingRepository`
Next move all pairing-related SQL into one place.

### Why second
Pairing logic is used by:

- pairing endpoints,
- transfer authorization assumptions,
- fasttrack authorization,
- ping authorization,
- stats.

This makes it a high-reuse persistence area.

### Expected methods
For example:

- `findPairing(string $a, string $b)`
- `createPairing(string $a, string $b, int $createdAt)`
- `listPairingsForDevice(string $deviceId)`
- `incrementPairingStats(string $a, string $b, int $bytes)`
- `insertPairingRequest(...)`
- `listUnclaimedRequestsForDesktop(...)`
- `markRequestClaimed(int $id)`
- `deleteUnclaimedRequestFromPhoneToDesktop(...)`

---

## Phase 3 — extract `TransferRepository`
Then move all transfer-table SQL into one place.

### Why third
This is the largest persistence area and should come after patterns are established with smaller repositories.

### Expected methods
For example:

- `existsById(string $transferId)`
- `insertTransfer(...)`
- `findTransferById(string $transferId)`
- `incrementChunksReceived(string $transferId)`
- `markComplete(string $transferId)`
- `listPendingTransfersForRecipient(string $recipientId)`
- `listSentTransfersForSender(string $senderId, int $limit = 50)`
- `updateDownloadProgress(string $transferId, int $progress)`
- `markDelivered(string $transferId, int $deliveredAt)`
- `findExpiredTransfers(int $cutoff)`
- `findExpiredIncompleteTransfers(int $cutoff)`

---

## Phase 4 — extract `ChunkRepository`
After transfer rows are separated, isolate chunk metadata queries.

### Expected methods
For example:

- `findChunk(string $transferId, int $chunkIndex)`
- `insertChunk(...)`
- `chunkExists(string $transferId, int $chunkIndex)`
- `listChunksForTransfer(string $transferId)`
- `sumChunkBytesForTransfer(string $transferId)`
- `sumPendingBytesForRecipient(string $recipientId)`
- `deleteChunksForTransfer(string $transferId)`

---

## Phase 5 — extract `FasttrackRepository`
Then separate fasttrack message persistence.

### Expected methods
For example:

- `deleteExpiredMessagesForRecipient(...)`
- `countPendingMessagesForRecipient(...)`
- `insertMessage(...)`
- `listPendingMessagesForRecipient(...)`
- `findMessageById(...)`
- `deleteMessageById(...)`

---

## Phase 6 — extract `PingRateRepository`
Finally separate ping-rate persistence.

### Expected methods
For example:

- `tryClaimCooldown(...)`
- `findCooldown(...)`

This is small, but important because the atomic cooldown claim is subtle and benefits from being isolated.

---

## Recommended commit order

### Commit 1
`refactor(server): introduce DeviceRepository`

Contents:
- move device SQL into repository
- auth and FCM-related code consume it

### Commit 2
`refactor(server): introduce PairingRepository`

Contents:
- move pairing and pairing-request SQL into repository

### Commit 3
`refactor(server): introduce TransferRepository`

Contents:
- move transfer SQL into repository
- services stop issuing transfer-table SQL directly

### Commit 4
`refactor(server): introduce ChunkRepository`

Contents:
- move chunk metadata SQL into repository

### Commit 5
`refactor(server): introduce FasttrackRepository`

Contents:
- move fasttrack SQL into repository

### Commit 6
`refactor(server): introduce PingRateRepository`

Contents:
- move ping cooldown SQL into repository

### Commit 7
`refactor(server): remove remaining raw SQL from services`

Contents:
- cleanup pass
- eliminate direct DB querying outside repository boundaries where practical

---

## What should not be addressed here

This refactor **should not** address:

- protocol changes,
- endpoint redesign,
- service-layer redesign beyond what is needed to consume repositories,
- replacing SQLite,
- introducing an ORM,
- interface-first abstractions everywhere,
- automatic entity hydration,
- generic repository inheritance,
- transaction framework design as a major scope item.

Those would inflate the refactor without increasing near-term value.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. SQL is no longer spread across business services
Core business services no longer construct raw SQL for their main persistence flows.

### 2. Controllers do not know SQL
Controllers remain above the repository layer entirely.

### 3. Row-shape mapping is centralized
Associative-array assumptions are no longer duplicated widely.

### 4. Device, pairing, transfer, and chunk persistence are explicit
The main storage areas each have named repository access paths.

### 5. Clients do not notice any change
Android and desktop behavior remain unchanged.

### 6. `protocol.md` remains unchanged
No protocol-level edit is needed for this refactor.

---

## Test checklist

After each major repository extraction, verify:

### Device-related behavior
- register still works,
- auth by `(device_id, auth_token)` still works,
- `last_seen_at` updates still work,
- FCM token update still works,
- stats still show expected device information.

### Pairing-related behavior
- pairing request still works,
- pairing poll still works,
- pairing confirm still works,
- pairing existence checks still gate transfer / fasttrack / ping correctly.

### Transfer-related behavior
- transfer init still works,
- duplicate transfer ID still returns the same conflict behavior,
- pending-transfer listing still works,
- sent-status still works,
- delivery-state behavior remains unchanged,
- ACK still marks transfer delivered and cleans up storage correctly.

### Chunk-related behavior
- chunk upload still works,
- idempotent chunk upload still works,
- chunk download still works,
- byte aggregation still works,
- cleanup still deletes chunk records correctly.

### Fasttrack behavior
- send still works,
- pending still works,
- ack still works,
- max-pending enforcement still works,
- expiry cleanup still works.

### Ping-rate behavior
- atomic cooldown claim still works,
- retry-after behavior still works,
- concurrent ping prevention still works.

---

## Risks

### 1. Repositories becoming fake wrappers
A repository layer that only renames `db->query(...)` without defining meaningful boundaries provides little value.

The methods must express domain-relevant persistence operations.

---

### 2. Repositories becoming too generic
If repositories become vague utility bags, the result will be no better than the current state.

Each repository should map to a real persistence area with clear ownership.

---

### 3. Service logic leaking back into repositories
Repositories should not become mini-services.

They should handle SQL, mapping, and storage persistence concerns — not business decisions.

---

### 4. Silent row-shape changes
Centralizing row mapping is one of the goals, but it also means mistakes there become shared everywhere.

That is why extraction should happen incrementally, with testing after each repository.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have a layered enterprise persistence framework."

The correct result is:

- persistence is explicit,
- SQL is centralized,
- services express intent,
- repository methods are meaningful,
- future schema or storage changes become cheaper.

---

## Practical definition of done

Done looks like this:

- business services call repositories,
- repositories own SQL,
- row mapping is centralized,
- controllers stay above the persistence layer,
- protocol behavior is unchanged,
- future changes to queries affect fewer files than today.

That is the main purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the server will be:

- easier to read,
- easier to modify safely,
- less repetitive at the SQL layer,
- better prepared for stricter state modeling in refactor 4,
- better prepared for future schema evolution,
- less likely to spread persistence details into unrelated services.

That is why this refactor is third.

---

## Note about the next step

After this refactor, the next one should be:

**formalize internal transfer states and state transitions**

That becomes much easier once persistence access is explicit.  
There is little value in building a stricter state model while the rules are still coupled to scattered SQL access.
