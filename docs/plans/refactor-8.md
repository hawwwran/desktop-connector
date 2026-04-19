# refactor-8.md

> **Status: Done** — landed on `main` in commit `06b3f0b` (PR #15). New `docs/protocol.compatibility.md` (preserving / extending / breaking classification + concrete edit-to-category table) and `docs/protocol.examples.md` (canonical request/response examples for auth headers, registration, pairing, transfers + chunks + ack, fasttrack, `.fn.*` mappings, ping/pong, error envelope). Executable contract tests at `tests/protocol/`: `test_desktop_message_contract.py` pins `FnTransferAdapter`/`FasttrackAdapter` in-process, `test_server_contract.py` spawns a hermetic PHP server (copies source without `data/`/`storage/`) and exercises the HTTP surface including 401/404/400/path-traversal error envelopes. All 11 tests pass under `python3 -m unittest discover tests/protocol`. No production code changes.

## Refactor 8 / 10
# Introduce a compatibility layer between `protocol.md` and the implementation

## Why this is the eighth refactor

After the first seven refactors, the next highest-value step is to make protocol conformance explicit.

At this point, the project has:

- a formal `protocol.md`,
- a companion `explain.protocol.md`,
- cleaner server boundaries,
- cleaner desktop boundaries,
- and a more unified internal message model.

That creates the right conditions for the next structural step:

the implementation should no longer rely on **informal alignment** with the protocol.

Instead, the system should gain an explicit compatibility layer that makes it possible to answer questions such as:

- does this endpoint still conform to the protocol?
- does this message shape still match the documented contract?
- does this status mapping still match the documented meaning?
- did a refactor accidentally change externally visible behavior?
- is a new feature protocol-compatible, protocol-extending, or protocol-breaking?

This is the purpose of refactor 8.

---

## Relation to the previous refactors

This refactor depends on the previous seven steps.

It only becomes practical after:

- server behavior is more modular,
- request handling is more explicit,
- persistence boundaries are cleaner,
- transfer lifecycle semantics are explicit,
- desktop bootstrap is cleaner,
- platform boundaries are cleaner,
- command/message semantics are unified.

Without those earlier refactors, protocol compatibility checking would be too entangled with implementation details to be truly useful.

Now it becomes realistic.

---

## Position within the full sequence of 10 refactors

The full sequence remains:

1. Split the transfer domain into smaller server services without changing the protocol
2. Introduce an explicit server layer for auth, request context, and input validation
3. Introduce a repository layer above SQLite access
4. Formalize internal transfer states and state transitions
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. Separate desktop core from Linux-specific backends
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. **Introduce a compatibility layer between `protocol.md` and the implementation**
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 8**.

---

## Main goal

Move the project from the current shape:

- protocol exists as a document,
- implementation exists as code,
- conformance is inferred manually,
- regressions are found indirectly,
- protocol drift is possible during refactors,

to this shape:

- protocol expectations are represented in machine-checkable or at least systematically testable form,
- implementation can be checked against those expectations,
- protocol changes become explicit,
- protocol-compatible vs protocol-breaking changes become easier to classify,
- the gap between documentation and behavior becomes much smaller.

---

## What must not change

This refactor **must not itself change the protocol**.

It is not a feature refactor and not a protocol redesign.

That means it should not change:

- endpoint shapes,
- public message shapes,
- status meanings,
- transport behavior,
- delivery semantics,
- pairing semantics,
- command semantics.

Its purpose is to make protocol compliance more explicit, not to redefine the protocol.

---

## Core architectural idea

`protocol.md` should stop being only a human-readable specification and start acting as a **verifiable contract boundary**.

That does not mean `protocol.md` itself must become code.  
It means the project should introduce an explicit layer that translates the protocol into things the implementation can be checked against.

This compatibility layer should answer two related but different needs:

### 1. Conformance checking
Does the current implementation still behave according to the protocol?

### 2. Change classification
If behavior is changing, is it:
- protocol-preserving,
- protocol-extending,
- or protocol-breaking?

That distinction is extremely valuable for future refactors and feature work.

---

## What "compatibility layer" means here

This refactor is **not** about inventing a full IDL system or replacing prose documentation with generated code.

It is about creating a practical layer that sits between:

- the protocol specification,
- and the concrete implementation,

and captures the protocol in forms such as:

- protocol contract tests,
- schema-like expectations,
- status mapping assertions,
- endpoint request/response expectations,
- message-shape validators,
- compatibility rules.

The exact mechanism can be lightweight.  
What matters is that compatibility stops being purely informal.

---

## Recommended compatibility model

A practical compatibility model should cover at least these areas:

### 1. Endpoint contracts
For each important public endpoint, define expectations such as:

- required headers,
- required request fields,
- response field presence,
- response field meaning,
- expected status codes for core scenarios.

Examples:
- `/api/devices/register`
- `/api/pairing/request`
- `/api/transfers/init`
- `/api/transfers/sent-status`
- `/api/transfers/notify`
- `/api/fasttrack/send`

The goal is not to exhaustively encode every edge case immediately, but to cover the most protocol-critical ones first.

---

### 2. Message contracts
For command-style behavior, define expectations for:

- `.fn.*` compatibility semantics,
- fasttrack message shapes,
- unified message types,
- required payload fields,
- transport-appropriate constraints.

The point is to ensure that command semantics do not drift away from the documented model.

---

### 3. State/status contracts
For transfer lifecycle behavior, define expectations for mappings such as:

- internal state -> public `status`
- internal state -> public `delivery_state`
- delivery completion invariants
- sent-status response consistency
- notify inline `sent_status` consistency

This is especially important because status drift is easy to introduce accidentally during server refactors.

---

### 4. Compatibility rules
Add explicit rules for classifying changes.

For example:

- adding an optional response field may be protocol-extending,
- changing the meaning of `delivery_state` is protocol-breaking,
- changing required request fields is protocol-breaking,
- adding a new message type may be protocol-extending,
- removing support for `.fn.unpair` is protocol-breaking.

This does not need to become legalistic, but it should be explicit enough to guide future work.

---

## What should be introduced

### 1. Protocol contract test suite
Introduce tests that represent protocol expectations directly.

These should not merely test implementation internals.  
They should test observable protocol behavior.

Examples:

- register contract test
- pairing contract test
- transfer init contract test
- sent-status contract test
- notify contract test
- fasttrack contract test
- command message compatibility tests

These tests form the first practical compatibility layer.

---

### 2. Protocol fixtures or examples
Introduce canonical example payloads and responses for important protocol flows.

These can be used by tests and by future documentation maintenance.

Examples:
- successful register request/response
- pairing request example
- sent-status example
- notify example with inline `sent_status`
- fasttrack message example
- `.fn.*` semantic examples

This turns the protocol from pure prose into prose backed by concrete examples.

---

### 3. Schema-like validators for key message shapes
For the most important protocol structures, introduce lightweight shape validation.

This does not need a full OpenAPI or Protobuf migration.

It can be simple:
- required fields,
- allowed enum values,
- optional fields,
- numeric constraints,
- shape validators for example responses.

Good candidates:
- register response
- sent-status entries
- notify response
- fasttrack pending entries
- unified message payloads

---

### 4. Compatibility classification guidelines
Introduce an explicit internal guide for how to classify changes relative to the protocol.

This may live in:
- `protocol-compatibility.md`
- or a section in a future contributing/dev document
- or comments adjacent to the compatibility test layer

The goal is that developers can ask:
- "am I changing the protocol?"
and answer that consistently.

---

### 5. Optional protocol version marker strategy
The project may benefit from planning how protocol versioning would work later, even if no explicit version field is introduced now.

This refactor does **not** need to add protocol version negotiation yet.  
But it should at least create a place where future protocol versioning would connect to compatibility checks.

That keeps later evolution cleaner.

---

## Suggested target structure

A practical target shape could be:

```text
docs/
  protocol.md
  explain.protocol.md
  protocol.examples.md
  protocol.compatibility.md

tests/
  protocol/
    test_register_contract.py
    test_pairing_contract.py
    test_transfer_contract.py
    test_notify_contract.py
    test_fasttrack_contract.py
    test_message_contract.py
```

Or, depending on your testing layout:

```text
desktop/tests/protocol/
server/tests/protocol/
android/... (where applicable)
```

The exact structure is flexible.  
The key point is that protocol expectations become an explicit testable artifact.

---

## What the first iteration should include

To keep this refactor practical, the first iteration should focus on high-value protocol surfaces.

### Required in the first iteration
At minimum, introduce compatibility checks for:

- registration
- pairing
- transfer init
- sent-status
- notify
- fasttrack send/pending/ack
- unified command/message semantics for the currently active command types

### Strongly recommended
Also introduce:

- canonical protocol examples,
- shape validators for key responses,
- explicit change-classification rules.

### Not required yet
The first iteration does **not** need:

- automatic documentation generation,
- full OpenAPI conversion,
- codegen,
- protocol negotiation,
- binary schema systems,
- cross-language shared model generation,
- perfect coverage of every endpoint edge case.

The goal is to establish a real compatibility boundary, not to fully mechanize the whole protocol in one step.

---

## Concrete execution plan

## Phase 1 — identify protocol-critical surfaces
Make a concrete list of the most protocol-sensitive behaviors.

At minimum, include:

- registration
- authentication headers
- pairing request/poll/confirm
- transfer init
- transfer pending/download/ack
- sent-status
- notify
- fasttrack send/pending/ack
- `.fn.*` command semantics
- fasttrack command semantics

### Goal
Start from what matters most for interoperability and correctness.

---

## Phase 2 — introduce canonical protocol examples
Create one place where representative request/response examples live.

### Goal
Make the protocol easier to review and easier to test against.

### Deliverable
For example:
- `docs/protocol.examples.md`

This should contain concise canonical examples, not a second prose specification.

---

## Phase 3 — add contract tests for endpoints
Introduce tests that assert observable behavior of protocol-critical endpoints.

### Goal
Catch accidental protocol drift during refactors.

These tests should validate:
- status code,
- key response fields,
- field meanings where practical,
- consistency across related endpoints.

---

## Phase 4 — add contract tests for status mapping
Introduce tests specifically focused on:
- sent-status
- notify inline `sent_status`
- lifecycle-to-status mapping

### Goal
Protect the most drift-prone part of the protocol.

---

## Phase 5 — add command/message compatibility tests
Introduce tests for:
- `.fn.*` semantics
- fasttrack message semantics
- unified internal message handling compatibility

### Goal
Ensure semantic unification does not create hidden drift relative to documented behavior.

---

## Phase 6 — define compatibility classifications
Write down how changes should be classified.

Suggested categories:

### Protocol-preserving
Implementation changes that do not affect externally visible protocol behavior.

### Protocol-extending
Changes that add optional behavior or optional fields without breaking compliant existing clients.

### Protocol-breaking
Changes that alter required fields, meanings, state semantics, or required behavior.

This classification should become part of future development discipline.

---

## Recommended commit order

### Commit 1
`test(protocol): add canonical protocol examples`

Contents:
- protocol examples document
- representative request/response samples

### Commit 2
`test(protocol): add register and pairing contract tests`

Contents:
- endpoint contract tests for registration and pairing

### Commit 3
`test(protocol): add transfer contract tests`

Contents:
- transfer init / pending / ack / sent-status contract tests

### Commit 4
`test(protocol): add notify compatibility tests`

Contents:
- long-poll response shape and inline `sent_status` compatibility tests

### Commit 5
`test(protocol): add fasttrack contract tests`

Contents:
- send / pending / ack protocol checks

### Commit 6
`test(protocol): add command/message compatibility tests`

Contents:
- `.fn.*` and fasttrack semantic compatibility coverage

### Commit 7
`docs(protocol): define compatibility classification rules`

Contents:
- protocol-preserving / extending / breaking rules

---

## What should not be addressed here

This refactor **should not** address:

- protocol redesign,
- transport redesign,
- wire-format migration,
- OpenAPI migration,
- code generation,
- endpoint restructuring,
- explicit protocol version negotiation,
- full client simulation framework,
- test coverage for every implementation detail.

Those may come later if useful, but they are not required for this refactor to deliver value.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. Protocol-critical behaviors are represented in explicit contract tests
The implementation is no longer trusted to match `protocol.md` purely by manual inspection.

### 2. Canonical examples exist
The protocol has concrete examples for key flows, not only prose descriptions.

### 3. Status mapping is protected by compatibility tests
Sent-status and notify compatibility are explicitly tested.

### 4. Command/message semantics are compatibility-tested
The system can detect drift in `.fn.*` or fasttrack behavior.

### 5. Compatibility classifications are defined
The project has an explicit way to describe whether a future change is:
- protocol-preserving,
- protocol-extending,
- or protocol-breaking.

### 6. No protocol behavior changes are introduced by this refactor
The compatibility layer is added without changing the protocol itself.

---

## Test checklist

After each major phase, verify:

### Registration compatibility
- public key registration still returns expected fields,
- existing-device re-registration behavior remains compatible,
- status codes remain correct.

### Pairing compatibility
- request/poll/confirm flows still match documented shapes,
- missing-field behavior remains consistent,
- pairing success remains compatible.

### Transfer compatibility
- transfer init shape remains correct,
- pending transfer entries remain correct,
- ACK behavior remains correct,
- sender status views remain correct.

### Notify compatibility
- `test=1` path remains compatible,
- pending/delivered/download_progress flags remain compatible,
- inline `sent_status` remains compatible.

### Fasttrack compatibility
- send response shape remains correct,
- pending message shape remains correct,
- ack behavior remains correct.

### Message compatibility
- `.fn.clipboard.text` semantics remain correct,
- `.fn.clipboard.image` semantics remain correct,
- `.fn.unpair` semantics remain correct,
- find-phone semantics remain correct.

---

## Risks

### 1. Mistaking implementation behavior for protocol intent
If the compatibility layer simply snapshots accidental implementation quirks, it may freeze the wrong behavior.

That is why compatibility work must stay anchored to `protocol.md`, not just to "whatever the code does today."

---

### 2. Building tests that are too shallow
If tests only check that a field exists, but not what it means, they may fail to catch real protocol drift.

The highest-value checks are meaning-sensitive ones.

---

### 3. Turning the protocol into a second undocumented implementation
The compatibility layer must remain clearly subordinate to `protocol.md`.

It should operationalize the protocol, not replace it as a secret second spec.

---

### 4. Over-mechanizing too early
There is a risk of turning this into a full schema/tooling project.

That is not necessary yet.

The practical goal is confidence and clarity, not protocol tooling maximalism.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have a complete formal machine-generated protocol system."

The correct result is:

- protocol-critical behavior is explicitly tested,
- key examples are documented,
- semantic drift becomes detectable,
- future changes can be classified against the protocol,
- implementation and protocol are tied together much more tightly than before.

---

## Practical definition of done

Done looks like this:

- key protocol behaviors have contract tests,
- canonical examples exist,
- status semantics are protected,
- command semantics are protected,
- compatibility classifications exist,
- `protocol.md` remains the source of truth,
- protocol drift becomes much harder to introduce silently.

That is the purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the project will be:

- safer to refactor,
- clearer to evolve,
- less likely to drift away from its own documented protocol,
- better prepared for future versioning if needed,
- better prepared for broader contributor involvement,
- more trustworthy as a system with a real documented contract.

That is why this refactor is eighth.

---

## Note about the next step

After this refactor, the next one should be:

**consolidate logging and diagnostic events across platforms**

That is the logical next move because once protocol behavior is easier to verify, the next high-value improvement is operational visibility: better diagnostic consistency across server, desktop, and Android.
