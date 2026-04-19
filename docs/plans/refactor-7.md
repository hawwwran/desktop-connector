# refactor-7.md

## Refactor 7 / 10
# Introduce a unified command/message model for `.fn.*` and fasttrack

## Why this is the seventh refactor

After the first six refactors, the next highest-value step is to unify the conceptual model behind special transfers and lightweight commands.

Right now, the system effectively has **two parallel ways to represent non-regular file behavior**:

1. special `.fn.*` transfer files, such as:
   - `.fn.clipboard.text`
   - `.fn.clipboard.image`
   - `.fn.unpair`

2. fasttrack encrypted messages, currently used for lightweight command-style behavior such as:
   - find-my-phone commands
   - lightweight wake-driven control messages
   - future small command payloads

Both mechanisms are valid and useful.  
The problem is that they currently evolve as **two separate conceptual systems** even though they are both forms of **cross-device command/message semantics**.

The purpose of this refactor is to introduce a **single conceptual command/message model** that can represent both:

- file-backed command delivery (`.fn.*` transfers),
- lightweight fasttrack command delivery,

while keeping external behavior unchanged.

---

## Relation to the previous refactors

This refactor comes after:

- server transfer-domain cleanup,
- request-boundary cleanup,
- repository separation,
- lifecycle formalization,
- desktop bootstrap cleanup,
- desktop platform-boundary cleanup.

That order matters because a unified message model depends on:

- clearer transfer lifecycle semantics,
- clearer server structure,
- cleaner desktop boundaries,
- and less entangled startup/platform logic.

Without those earlier refactors, message unification would become much messier.

---

## Position within the full sequence of 10 refactors

The full sequence remains:

1. Split the transfer domain into smaller server services without changing the protocol
2. Introduce an explicit server layer for auth, request context, and input validation
3. Introduce a repository layer above SQLite access
4. Formalize internal transfer states and state transitions
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. Separate desktop core from Linux-specific backends
7. **Introduce a unified command/message model for `.fn.*` and fasttrack**
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 7**.

---

## Main goal

Move the system from the current shape:

- `.fn.*` transfers interpreted by filename convention,
- fasttrack payloads interpreted as a separate conceptual system,
- command semantics split by transport type,
- feature behavior sometimes tied to delivery mechanism rather than command meaning,

to this shape:

- commands/messages have one unified semantic model,
- transport is a separate concern from message meaning,
- the system can express "what this message means" independently of whether it travels:
  - as a transfer,
  - or as a fasttrack payload,
- future command-style features can be added without inventing yet another semantic path.

---

## What must not change

This refactor **must not change externally observable behavior** unless a protocol change is intentionally introduced later.

That means no change to:

- current `.fn.*` behavior,
- current fasttrack behavior,
- current command meanings,
- current transfer vs fasttrack transport behavior,
- current pairing behavior,
- current clipboard behavior,
- current find-my-phone behavior.

This refactor is about **unifying semantics internally**, not redesigning delivery mechanisms.

---

## What the current conceptual problem is

Today the system already contains the seed of one unified idea:

- some messages are really files,
- some messages are really commands,
- some commands are encoded as special filenames,
- some commands are encoded as encrypted fasttrack payloads.

The issue is not that the mechanisms are wrong.  
The issue is that the conceptual model is fragmented.

Examples:

### `.fn.*`
The system uses filename conventions to signal:
- clipboard text,
- clipboard image,
- unpair behavior.

This works, but it couples command meaning to file naming.

### Fasttrack
The system uses encrypted message payloads for:
- find-my-phone control,
- location updates,
- future lightweight command-style semantics.

This works, but it is treated as a separate message world.

Conceptually, however, both are really:

- a sender expressing an intent,
- a receiver interpreting that intent,
- transport chosen according to delivery constraints.

That shared model should become explicit.

---

## Core architectural idea

A command/message should be modeled first by its **meaning**, then by its **transport**.

That means the system should distinguish clearly between:

### 1. Semantic layer
What the message means.

For example:
- clipboard text
- clipboard image
- unpair
- find-phone-start
- find-phone-stop
- find-phone-location-update
- future notification mirror message
- future SMS-related message
- future control command

### 2. Transport layer
How the message is delivered.

For example:
- file transfer transport
- fasttrack transport

### 3. Execution/handling layer
What the receiver does when such a command is received.

For example:
- write to clipboard
- remove pairing
- start alarm
- stop alarm
- update map position
- show notification
- etc.

This separation is the main point of the refactor.

---

## Recommended model

The system should adopt one unified internal message concept such as:

- `DeviceMessage`
- or `DeviceCommand`
- or `RemoteActionMessage`

The name is flexible.  
The important thing is the structure.

A practical internal model might contain:

- `kind` or `type`
- `transport`
- `payload`
- `sender_id`
- `recipient_id`
- optional metadata such as:
  - correlation ID
  - created time
  - delivery expectations
  - content subtype
  - attachment reference where relevant

This does **not** mean the wire format must immediately change.  
It means the internal application model becomes unified.

---

## Recommended semantic categories

A practical first semantic categorization could be:

### 1. Clipboard commands
Examples:
- `clipboard.text`
- `clipboard.image`

These are currently represented through `.fn.clipboard.*`.

### 2. Pairing/control commands
Examples:
- `pairing.unpair`

This is currently represented as `.fn.unpair`.

### 3. Find-phone commands
Examples:
- `find_phone.start`
- `find_phone.stop`
- `find_phone.location_update`
- maybe later `find_phone.status`

These are currently represented through fasttrack payload semantics.

### 4. Future lightweight app commands
Examples:
- notification mirror commands
- future control actions
- future remote UI actions

### 5. Future transfer-adjacent control messages
Anything that is not really "a user file" but still needs delivery and interpretation between paired devices.

---

## Recommended transport categories

The transport model should be explicit.

For example:

- `transfer_file`
- `fasttrack`

Later, if needed, the system could also support other transports conceptually, but for now these two are enough.

The key point is:

**message meaning must not be defined by transport choice.**

Instead:

- transport is chosen because of size / timing / delivery constraints,
- meaning is chosen because of feature semantics.

---

## What should be introduced

### 1. Unified internal message model
Introduce one internal representation for command-style cross-device messages.

For example:

```text
DeviceMessage
  type
  transport
  payload
  sender_id
  recipient_id
  ...
```

This may be:
- a dataclass,
- a lightweight class,
- a typed dict,
- or equivalent structured model in each relevant implementation language.

The exact representation is less important than the conceptual unification.

---

### 2. Message parser/adapter for `.fn.*` transfers
Introduce an adapter that converts existing `.fn.*` transfers into the unified message model.

That means:

- `.fn.clipboard.text` becomes a semantic message
- `.fn.clipboard.image` becomes a semantic message
- `.fn.unpair` becomes a semantic message

The filename convention may remain externally, but internally it should become an input adapter into the unified command model.

---

### 3. Message parser/adapter for fasttrack payloads
Introduce an adapter that converts current fasttrack payloads into the same unified message model.

That means fasttrack stops being "the other command system" and becomes just another transport for unified messages.

---

### 4. Unified message dispatcher/handler
Introduce a receiver-side dispatcher that handles messages based on semantic meaning, not based on raw transport details.

For example:
- clipboard message handler
- pairing-control handler
- find-phone handler
- future notification mirror handler

The dispatcher should decide:
- what handler is responsible,
- whether the message is valid,
- whether payload is sufficient,
- what action is executed.

It should not care whether the message arrived via `.fn.*` transfer or fasttrack, except where transport imposes delivery-specific behavior.

---

### 5. Transport selection policy
Introduce an explicit policy for choosing transport.

For example, conceptually:

- use `transfer_file` when payload is file-backed or naturally transfer-oriented,
- use `fasttrack` when payload is lightweight, low-latency, and not worth the transfer pipeline.

This policy may remain simple, but it should be explicit.

That helps future features decide:
- "this is a command",
- "which transport should it use?"

instead of inventing a mechanism ad hoc.

---

## Suggested target structure

A practical target shape could be:

```text
desktop/src/
  messaging/
    message_model.py
    message_types.py
    fn_transfer_adapter.py
    fasttrack_adapter.py
    dispatcher.py
    handlers/
      clipboard_handler.py
      pairing_handler.py
      find_phone_handler.py

android/app/src/main/kotlin/.../messaging/
  DeviceMessage.kt
  MessageAdapters.kt
  MessageDispatcher.kt
  handlers/
    ClipboardHandler.kt
    PairingHandler.kt
    FindPhoneHandler.kt
```

On the server side, if useful:

```text
server/src/
  Messaging/
    MessageTransportPolicy.php
```

The exact structure can vary, but the separation should be:
- model,
- adapter,
- dispatcher,
- handler.

---

## What the first iteration should include

To keep this refactor practical, the first iteration should focus on semantic unification, not on protocol redesign.

### Required in the first iteration
At minimum, introduce:

- one unified internal command/message model,
- one adapter for `.fn.*`,
- one adapter for fasttrack payloads,
- one unified receiver-side dispatcher,
- explicit semantic handler boundaries for:
  - clipboard
  - unpair
  - find-phone

### Strongly recommended
Also introduce:

- explicit transport selection rules in code or docs,
- shared naming conventions for message types,
- basic validation of command payload shape per message type.

### Not required yet
The first iteration does **not** need:

- a wire-format change,
- removal of `.fn.*`,
- removal of fasttrack,
- one universal transport for everything,
- a protocol migration,
- fully shared code between desktop and Android,
- message versioning yet.

This is an internal unification refactor first.

---

## Concrete execution plan

## Phase 1 — define unified message types
Introduce a central set of semantic message types.

### Goal
Stop representing command meaning only through:
- filenames,
- or transport-specific payload conventions.

### Deliverable
For example:
- `MessageType.CLIPBOARD_TEXT`
- `MessageType.CLIPBOARD_IMAGE`
- `MessageType.UNPAIR`
- `MessageType.FIND_PHONE_START`
- `MessageType.FIND_PHONE_STOP`
- `MessageType.FIND_PHONE_LOCATION_UPDATE`

---

## Phase 2 — define unified message model
Introduce the structured internal message representation.

### Goal
Make command handling depend on one model rather than two unrelated representations.

### Deliverable
For example:
- `DeviceMessage`

with fields like:
- `type`
- `transport`
- `payload`
- `sender_id`
- `recipient_id`

---

## Phase 3 — add `.fn.*` adapter
Create an adapter that converts incoming `.fn.*` transfer artifacts into unified messages.

### Goal
Keep filename conventions as an external compatibility mechanism, but stop making them the primary semantic model.

### Deliverable
For example:
- `fn_transfer_adapter.py`
- or equivalent Kotlin/Python implementation as needed

---

## Phase 4 — add fasttrack adapter
Create an adapter that converts incoming fasttrack payloads into unified messages.

### Goal
Fasttrack becomes a transport path into the same semantic model.

### Deliverable
For example:
- `fasttrack_adapter.py`

---

## Phase 5 — add unified message dispatcher
Introduce a dispatcher that routes unified messages to semantic handlers.

### Goal
Receiver logic no longer branches first on transport-specific representation.

### Deliverable
For example:
- `dispatcher.py`
- handler registry or equivalent dispatch mapping

---

## Phase 6 — extract semantic handlers
Move command execution behavior into dedicated handlers.

### Start with:
- clipboard handler
- pairing/unpair handler
- find-phone handler

### Goal
Command execution becomes explicit and modular.

---

## Phase 7 — define transport selection rules
Document and encode how the sender chooses between:
- transfer transport
- fasttrack transport

### Goal
Future features should not choose delivery mechanism ad hoc.

This may initially be a simple policy helper rather than a large abstraction.

---

## Recommended commit order

### Commit 1
`refactor(messaging): introduce unified message type definitions`

Contents:
- central semantic type definitions

### Commit 2
`refactor(messaging): introduce unified DeviceMessage model`

Contents:
- common internal command/message representation

### Commit 3
`refactor(messaging): add .fn transfer adapter`

Contents:
- `.fn.*` parsed into unified message model

### Commit 4
`refactor(messaging): add fasttrack adapter`

Contents:
- fasttrack payloads parsed into unified message model

### Commit 5
`refactor(messaging): introduce unified message dispatcher`

Contents:
- one dispatch point for semantic handling

### Commit 6
`refactor(messaging): extract clipboard, unpair, and find-phone handlers`

Contents:
- semantic handlers separated from raw transport logic

### Commit 7
`refactor(messaging): codify transport selection policy`

Contents:
- explicit rule for choosing transfer vs fasttrack

---

## What should not be addressed here

This refactor **should not** address:

- protocol redesign,
- deletion of `.fn.*`,
- deletion of fasttrack,
- conversion of all messages to a single transport,
- feature redesign of clipboard or find-phone,
- server transport redesign,
- message versioning framework,
- guaranteed bidirectional symmetry in one pass across all runtimes.

Those are separate concerns.

This refactor is about **semantic unification**, not transport consolidation.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. Command semantics are modeled centrally
There is one internal message/command model for command-style cross-device behavior.

### 2. `.fn.*` is no longer the primary semantic model
`.fn.*` remains a compatibility mechanism, but internally it is treated as an adapter into the unified model.

### 3. Fasttrack is no longer a separate conceptual command system
Fasttrack payloads are adapted into the same semantic model.

### 4. Receiver-side handling is based on semantic type
Command execution is dispatched by unified message type, not raw transport representation.

### 5. Clipboard, unpair, and find-phone behavior are represented within the same message model
These three become the proof that the unified model works across both existing mechanisms.

### 6. User-visible behavior remains unchanged
Clipboard, unpair, and find-phone features behave the same as before.

---

## Test checklist

After each major phase, verify:

### `.fn.*` command behavior
- incoming `.fn.clipboard.text` still writes text to clipboard,
- incoming `.fn.clipboard.image` still writes image to clipboard,
- incoming `.fn.unpair` still removes the pairing as before.

### Fasttrack command behavior
- find-phone start command still works,
- find-phone stop command still works,
- location update behavior still works,
- fasttrack wake behavior remains unchanged.

### Unified dispatch behavior
- dispatch is based on semantic message type,
- handlers do not depend on raw transport representation,
- malformed payloads fail in a controlled way.

### Sender-side transport choice
- existing commands still choose the same transport as before unless intentionally changed,
- no file-backed command is accidentally moved to fasttrack,
- no fasttrack command is accidentally forced through file transfer.

### Behavior compatibility
- protocol behavior remains unchanged,
- Android and desktop continue interoperating exactly as before.

---

## Risks

### 1. Accidental protocol redesign
If semantic unification starts changing wire format too early, this refactor will become much larger and riskier than intended.

That must be avoided in the first iteration.

---

### 2. Building an overly abstract message framework
The model should stay concrete and feature-driven.

The goal is not to build a grand generalized messaging platform.  
The goal is to stop maintaining two separate semantic systems for command-style behavior.

---

### 3. Mixing transport policy and semantic meaning
Transport choice and semantic meaning are related, but they are not the same thing.

If those are not kept separate, the new model will remain conceptually muddy.

---

### 4. Partial migration creating two semantic layers
During migration, it is easy to accidentally create:
- old `.fn.*` semantics,
- old fasttrack semantics,
- and the new unified model,

all at once.

The transition should deliberately reduce the old semantic branching, not add a third permanent layer.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have a universal distributed command bus."

The correct result is:

- one internal semantic model for command-style behavior,
- `.fn.*` and fasttrack treated as transports/adapters,
- one dispatcher,
- explicit handlers,
- future command features become cheaper to add.

---

## Practical definition of done

Done looks like this:

- semantic message types are explicit,
- one internal message model exists,
- `.fn.*` adapts into it,
- fasttrack adapts into it,
- receiver dispatch is unified,
- clipboard, unpair, and find-phone are handled through the same semantic architecture,
- behavior remains unchanged.

That is the purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the system will be:

- easier to reason about semantically,
- easier to extend with future command-style features,
- less likely to split into multiple ad hoc command systems,
- better prepared for protocol compatibility work in refactor 8,
- less likely to couple feature meaning to transport mechanism,
- structurally cleaner across desktop, Android, and server.

That is why this refactor is seventh.

---

## Note about the next step

After this refactor, the next one should be:

**introduce a compatibility layer between `protocol.md` and the implementation**

That becomes the natural next move because once command semantics are unified, the next high-value step is ensuring that the implementation can be checked against the protocol explicitly rather than relying on informal alignment.
