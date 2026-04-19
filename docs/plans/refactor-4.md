# refactor-4.md

> **Status: Done** — landed on `main` in commit `25a405b` (PR #10). New `server/src/Domain/Transfer/` with `TransferState`, `TransferInvariants`, `TransferLifecycle`, and `TransferStatusMapper`. `TransferService` upload/download/ack paths and `TransferCleanupService::deleteTransferFiles` now route through named transition helpers (`onChunkStored`, `onRecipientProgress`, `onAckReceived`, `onTransferExpired`) with an explicit transition table. Invariants throw `ApiError(500)` on violation. Wire contract unchanged — `status` and `delivery_state` values preserved via `TransferStatusMapper`. `test_loop.sh` passes end-to-end.

## Refactor 4 / 10
# Formalize internal transfer states and state transitions

## Why this is the fourth refactor

After refactor 1, 2, and 3, the next highest-value step is to formalize the server's internal transfer state model.

At the moment, transfer behavior is defined implicitly through combinations of fields such as:

- `complete`
- `downloaded`
- `chunks_received`
- `chunk_count`
- `chunks_downloaded`
- `delivered_at`

This works, but it creates an important long-term risk:

the system's real state model exists mostly as **inferred behavior**, not as an explicit domain model.

That makes it harder to:

- reason about correctness,
- audit invariants,
- extend delivery behavior safely,
- test state transitions directly,
- and prevent subtle inconsistencies between code paths.

The goal of this refactor is to make the internal transfer lifecycle explicit while **keeping the external protocol unchanged**.

---

## Relation to the previous refactors

This refactor only becomes worth doing after the previous three:

- refactor 1 separates the transfer domain from overloaded controllers,
- refactor 2 cleans up the HTTP boundary,
- refactor 3 separates persistence access into repositories.

Only after that does it make sense to formalize the domain state model itself.

If state modeling is attempted earlier, it usually ends up buried inside controller code and SQL details, which defeats the point.

---

## Position within the full sequence of 10 refactors

The full sequence remains:

1. Split the transfer domain into smaller server services without changing the protocol
2. Introduce an explicit server layer for auth, request context, and input validation
3. Introduce a repository layer above SQLite access
4. **Formalize internal transfer states and state transitions**
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. Separate desktop core from Linux-specific backends
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 4**.

---

## Main goal

Move the transfer domain from the current shape:

- state inferred from field combinations,
- invariants enforced indirectly,
- state semantics spread across services and query logic,
- transition rules hidden in procedural code,

to this shape:

- internal transfer states are explicitly named,
- allowed transitions are defined centrally,
- invariants are documented and checked in one place,
- status derivation becomes a consequence of the state model,
- services operate on clear lifecycle rules instead of field combinations alone.

---

## What must not change

This refactor **must not change the external protocol**.

That means no change to:

- endpoints,
- request/response shapes,
- public `status` values,
- public `delivery_state` values,
- delivery semantics,
- ACK semantics,
- long-poll semantics,
- chunk upload/download API behavior.

This is a **domain-model refactor**, not a protocol redesign.

The internal model can become more explicit as long as externally observable behavior remains identical.

---

## The problem with the current model

The current transfer lifecycle is effectively represented through a mix of booleans and counters.  
For example:

- a transfer may exist but not yet be complete,
- a transfer may be complete but not yet downloaded,
- a transfer may be partially downloaded,
- a transfer may be fully delivered only after ACK,
- sender-visible progress depends on specific combinations of fields.

That means the true lifecycle is something like a state machine already — but it is not declared as one.

As a result:

- transition rules are harder to audit,
- invalid combinations are harder to detect,
- state logic tends to get duplicated,
- future changes risk creating impossible or ambiguous states.

---

## Core architectural idea

The transfer domain should have an **explicit internal state machine**.

That does **not** necessarily mean adding a new DB enum column immediately.  
It means that at the domain level, the server should define:

- what internal states exist,
- what each state means,
- what invariants must hold in each state,
- what transitions are legal,
- what side effects happen during those transitions.

The implementation may still persist state using the current columns, but the model should become explicit.

---

## Recommended internal state model

A practical internal model could use these states:

1. `initialized`
2. `uploading`
3. `uploaded`
4. `delivering`
5. `delivered`
6. `expired`
7. `failed` *(optional internal-only state if needed later, not required now)*

This does **not** mean these values must become public API values.  
They are internal lifecycle concepts.

---

## Proposed meaning of each internal state

### `initialized`
Transfer row exists, but no chunks have been stored yet.

Typical invariants:
- `complete = 0`
- `chunks_received = 0`
- `downloaded = 0`

---

### `uploading`
Transfer row exists and at least one chunk has been received, but not all chunks are present.

Typical invariants:
- `complete = 0`
- `0 < chunks_received < chunk_count`
- `downloaded = 0`

---

### `uploaded`
All chunks have been uploaded and the recipient may fetch them, but the recipient has not started visible delivery progress yet.

Typical invariants:
- `complete = 1`
- `chunks_received == chunk_count`
- `downloaded = 0`
- `chunks_downloaded == 0`

---

### `delivering`
The recipient has started downloading chunks, but ACK has not yet been received.

Typical invariants:
- `complete = 1`
- `downloaded = 0`
- `0 < chunks_downloaded < chunk_count`

---

### `delivered`
Recipient has ACKed the transfer.

Typical invariants:
- `complete = 1`
- `downloaded = 1`
- `chunks_downloaded == chunk_count`
- `delivered_at > 0`

---

### `expired`
Transfer has been removed by cleanup policy.

This may not need to be persisted as a row-state if expiry is modeled as deletion, but it is still a useful conceptual lifecycle state for reasoning and documentation.

---

### `failed` *(optional future internal state)*
This does not need to be introduced in the current schema unless the server later wants to preserve terminal failure outcomes explicitly rather than deleting or leaving transfers incomplete.

For this refactor, it is acceptable to keep this only as a conceptual future extension.

---

## Allowed transitions

The internal lifecycle should explicitly allow only these transitions:

- `initialized -> uploading`
- `initialized -> uploaded` *(single-chunk fast path if all chunks arrive immediately)*
- `uploading -> uploading`
- `uploading -> uploaded`
- `uploaded -> delivering`
- `uploaded -> delivered` *(possible if delivery is effectively completed before any observable progress snapshot)*
- `delivering -> delivering`
- `delivering -> delivered`
- `initialized -> expired`
- `uploading -> expired`
- `uploaded -> expired`
- `delivering -> expired`

Transitions that should be treated as invalid or impossible include, for example:

- `delivered -> uploading`
- `delivered -> uploaded`
- `delivered -> delivering`
- `uploading -> initialized`
- `uploaded -> initialized`

The code should stop relying on "it probably cannot happen" and instead define what is valid.

---

## Key invariant groups

This refactor should explicitly define and centralize invariants such as the following.

### Upload invariants
- `0 <= chunks_received <= chunk_count`
- `chunk_count >= 1`
- `complete == 1` implies `chunks_received == chunk_count`

### Delivery invariants
- `0 <= chunks_downloaded <= chunk_count`
- `downloaded == 1` implies `chunks_downloaded == chunk_count`
- `downloaded == 1` implies `delivered_at > 0`

### State derivation invariants
- `downloaded == 1` implies internal state `delivered`
- `complete == 1 && downloaded == 0 && chunks_downloaded == 0` implies internal state `uploaded`
- `complete == 1 && downloaded == 0 && chunks_downloaded > 0` implies internal state `delivering`
- `complete == 0 && chunks_received == 0` implies internal state `initialized`
- `complete == 0 && chunks_received > 0` implies internal state `uploading`

### Safety invariants
- recipient-visible chunk serving must never produce a state equivalent to final delivery without ACK
- `chunks_downloaded == chunk_count` must remain equivalent to final delivery acknowledgement, not mere last-chunk serving

These invariants already exist implicitly in the system.  
The purpose of this refactor is to make them explicit and central.

---

## What should be introduced

### 1. `TransferLifecycle` or `TransferStateMachine`
A dedicated domain object or service that knows:

- how to derive internal state from persisted fields,
- what transitions are valid,
- what each transition means,
- what invariants must hold.

This object becomes the single point of truth for transfer lifecycle semantics.

---

### 2. Explicit state-derivation logic
There should be one central mechanism for deriving internal state from persisted transfer data.

For example:

- `deriveState(TransferRecord $transfer): TransferState`

This must replace scattered state inference in multiple places.

---

### 3. Explicit transition methods
Rather than letting services mutate transfer-related fields ad hoc, transition intent should become explicit.

For example:

- `onTransferInitialized(...)`
- `onChunkStored(...)`
- `onUploadCompleted(...)`
- `onRecipientProgress(...)`
- `onAckReceived(...)`
- `onTransferExpired(...)`

These do not all need to mutate storage directly.  
They may coordinate with repositories or return transition decisions.  
The important part is that transitions become named and explicit.

---

### 4. Centralized invariant checks
The lifecycle object should either:

- assert invariants directly,
- or expose validation helpers used in critical paths.

This is especially useful in sensitive operations such as:

- marking complete,
- updating recipient progress,
- marking delivered,
- computing sent-status,
- building notify responses.

---

### 5. Public-status mapping from internal lifecycle state
The public protocol currently exposes values such as:

- `status: uploading | pending | delivered`
- `delivery_state: not_started | in_progress | delivered`

That mapping should be explicitly derived from internal lifecycle state, not scattered across multiple code paths.

This keeps the protocol stable while improving internal clarity.

---

## Suggested target structure

A practical target shape could be:

```text
server/src/
  Domain/
    Transfer/
      TransferState.php
      TransferLifecycle.php
      TransferInvariants.php
      TransferStatusMapper.php
```

This does not need to be overengineered.  
Even a small set of files is enough if the responsibilities are clear.

Possible roles:

- `TransferState.php` — internal state definitions
- `TransferLifecycle.php` — derivation + transition rules
- `TransferInvariants.php` — explicit invariants if separation is useful
- `TransferStatusMapper.php` — mapping to public protocol values

These may also be combined if the first iteration should stay smaller.

---

## What the first iteration should include

To keep the refactor disciplined, the first iteration should be practical.

### Required in the first iteration
At minimum, introduce:

- explicit internal transfer-state definitions,
- one central state-derivation function,
- one central mapping from internal state to public status fields,
- one place where the key invariants are defined.

### Strongly recommended
Also introduce:

- named transition methods or transition helpers for key lifecycle events,
- test coverage for legal and illegal state combinations.

### Not required yet
The first iteration does **not** need:

- a new DB column storing state explicitly,
- an event-sourcing model,
- a workflow engine,
- a generic state-machine library,
- full-blown domain entities for every record.

The point is explicit lifecycle semantics, not infrastructure complexity.

---

## Concrete execution plan

## Phase 1 — define internal states
Introduce a small explicit model of internal transfer states.

### Goal
So the system can say "this transfer is in `uploaded` state" rather than inferring that only indirectly from a query branch somewhere.

### Deliverable
A central `TransferState` definition.

---

## Phase 2 — centralize state derivation
Introduce one function or service that derives internal state from persisted fields.

### Goal
Eliminate scattered logic like:
- if downloaded then ...
- else if complete and chunks_downloaded > 0 then ...
- else if complete then ...
- else ...

This logic should live in one place only.

### Deliverable
For example:
- `TransferLifecycle::deriveState(...)`

---

## Phase 3 — centralize public status mapping
Move mapping to public protocol fields into one place.

### Goal
Both `/api/transfers/sent-status` and inline notify data should depend on the same mapper, with meaning derived from the explicit internal state model.

### Deliverable
For example:
- `TransferStatusMapper::toProtocolStatus(...)`

---

## Phase 4 — define invariants
Write down and enforce the key invariants.

### Goal
Prevent ambiguous or impossible state combinations from being silently accepted.

### Deliverable
For example:
- `TransferInvariants::assertValid(...)`

This can be used in:
- repository read normalization,
- service writes,
- status derivation,
- delivery transition handling.

---

## Phase 5 — make transitions explicit
Move the most important lifecycle changes behind explicit transition helpers.

### Goal
Instead of mutating field combinations ad hoc, key flows become named transitions.

The most important transitions to formalize first are:

- chunk stored,
- upload completed,
- recipient progress updated,
- delivery ACK received,
- transfer expired.

### Deliverable
Transition methods or helpers in the lifecycle model.

---

## Recommended commit order

### Commit 1
`refactor(server): introduce explicit internal TransferState definitions`

Contents:
- internal state definitions
- documentation comments for meaning

### Commit 2
`refactor(server): centralize transfer state derivation`

Contents:
- one place for deriving internal state from stored fields

### Commit 3
`refactor(server): centralize protocol status mapping`

Contents:
- public `status` and `delivery_state` mapping moved into one place

### Commit 4
`refactor(server): introduce transfer invariant checks`

Contents:
- core invariant assertions
- shared usage in sensitive paths

### Commit 5
`refactor(server): formalize lifecycle transitions for upload and delivery`

Contents:
- explicit transition helpers for key lifecycle changes

### Commit 6
`refactor(server): remove duplicated transfer state logic`

Contents:
- cleanup pass
- eliminate scattered field-combination logic where practical

---

## What should not be addressed here

This refactor **should not** address:

- protocol redesign,
- new public states,
- endpoint redesign,
- new storage backend,
- event sourcing,
- workflow framework adoption,
- adding a persisted state enum column unless strictly needed,
- client behavior changes.

This is about clarifying and centralizing the lifecycle model, not expanding the system.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. Internal transfer states are explicit
There is a clear, central definition of the internal lifecycle states.

### 2. State derivation exists in one place
The system no longer infers lifecycle state independently in multiple code paths.

### 3. Public status mapping exists in one place
`status` and `delivery_state` are mapped from the internal model centrally.

### 4. Key invariants are explicit
The important transfer invariants are written down in code and checked in sensitive paths.

### 5. Illegal transitions are no longer merely implicit
The code defines what transitions are valid rather than assuming invalid ones cannot happen.

### 6. Clients do not notice any change
Protocol behavior remains unchanged.

---

## Test checklist

After each major phase, verify:

### State derivation
- a newly initialized transfer derives to `initialized`
- a partial upload derives to `uploading`
- a complete upload with no recipient progress derives to `uploaded`
- a transfer with recipient progress and no ACK derives to `delivering`
- an ACKed transfer derives to `delivered`

### Protocol mapping
- `initialized` and `uploading` map to public upload-related semantics consistently
- `uploaded` maps to:
  - `status = pending`
  - `delivery_state = not_started`
- `delivering` maps to:
  - `status = pending`
  - `delivery_state = in_progress`
- `delivered` maps to:
  - `status = delivered`
  - `delivery_state = delivered`

### Invariants
- invalid `chunks_received > chunk_count` is detected
- invalid `downloaded == 1` with `chunks_downloaded < chunk_count` is detected
- invalid `complete == 1` with `chunks_received < chunk_count` is detected
- invalid final-delivery equivalence without ACK is not allowed

### Transition behavior
- chunk upload transitions correctly from `initialized -> uploading -> uploaded`
- recipient progress transitions correctly from `uploaded -> delivering`
- ACK transitions correctly from `uploaded/delivering -> delivered`
- cleanup transitions correctly to conceptual expiry

### Endpoint behavior
- `/api/transfers/sent-status` remains unchanged externally
- `/api/transfers/notify` inline `sent_status` remains unchanged externally
- upload/download/ACK behavior remains unchanged externally

---

## Risks

### 1. Accidental protocol drift
If public status mapping is changed while formalizing internal state, the refactor will accidentally become a protocol change.

That must be avoided.  
Internal clarity is the goal, not external redesign.

---

### 2. Overcomplicating the model
A state machine can easily become too abstract or too formal relative to the actual project.

The model should stay concrete and tied to real lifecycle behavior.

---

### 3. Duplicating the old implicit logic in a new wrapper
If the new lifecycle object merely rewraps the same scattered logic without centralizing meaning, the refactor will not deliver real value.

---

### 4. Premature schema redesign
It may be tempting to add a persisted enum state column immediately.

That is not necessary unless the current fields prove insufficient.  
The first step is to make the domain model explicit, not to redesign storage.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have a sophisticated abstract workflow engine."

The correct result is:

- the transfer lifecycle is explicit,
- invariants are centralized,
- public status mapping is centralized,
- transitions are named,
- future delivery-related changes become safer.

---

## Practical definition of done

Done looks like this:

- one central internal transfer-state model exists,
- one central state derivation exists,
- one central protocol-status mapping exists,
- invariants are defined and checked,
- key lifecycle transitions are explicit,
- protocol behavior remains unchanged.

That is the purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the transfer domain will be:

- easier to reason about,
- safer to extend,
- easier to test directly,
- less dependent on field-combination folklore,
- better prepared for future message-model unification in refactor 7,
- less likely to accumulate silent lifecycle inconsistencies.

That is why this refactor is fourth.

---

## Note about the next step

After this refactor, the next one should be:

**thin the desktop bootstrap (`main.py`) down to a clean entrypoint**

That is the right next move because by then the server side will have a much clearer internal structure, and attention can shift to the next major maintainability hotspot: the desktop application's startup and orchestration boundary.
