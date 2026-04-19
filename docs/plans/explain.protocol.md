# explain.protocol.md

## Purpose of this document

This document explains **why `protocol.md` exists**, what it is for, how it should be used, and what role it should play in the future development of the project.

`protocol.md` is not marketing copy and it is not a general technical overview of the project.  
It is a **formal technical specification of the communication layer** between clients and server.

This companion document exists to make the following explicit:

- why the protocol is documented separately from the implementation,
- what its practical value is,
- how it should be extended over time,
- and how it should be used when designing changes.

---

## What `protocol.md` is

`protocol.md` is a document that describes the **shared contract between system components**.

In Desktop Connector, that contract exists mainly between:

- the Android client,
- the desktop client,
- the relay server.

That contract includes things such as:

- authentication,
- device identity,
- pairing,
- request and response formats,
- encrypted envelope formats,
- transfer lifecycle,
- fasttrack messages,
- long polling,
- FCM wake behavior,
- ping/pong behavior,
- compatibility rules.

In other words:

**`protocol.md` defines what the different parts of the system may send each other, in what shape, in what order, and with what meaning.**

---

## Why the code alone is not enough

Without a separate protocol document, the meaning of the system gradually gets split across multiple places:

- part of it lives in server controllers,
- part of it lives in the Android client,
- part of it lives in the desktop client,
- part of it lives in the README,
- part of it lives only in the author's head.

That works while the project is small and maintained by one person.  
Over time, however, it becomes expensive:

- changes are slower to design,
- compatibility is harder to reason about,
- different parts of the system are more likely to interpret the same thing differently,
- new features get added in an ad hoc way,
- and old decisions become hard to recover.

`protocol.md` exists to prevent that.

---

## The main purpose of `protocol.md`

### 1. A single source of truth for communication
When the question is how communication between components works exactly, the answer should not be:

- "look at the Android code",
- "I think the server expects it like this",
- "the desktop client probably does it a bit differently".

The correct answer should be:

**"it is defined in `protocol.md`. The implementation should conform to it."**

---

### 2. Protection against silent divergence
As soon as the system spans three runtime worlds:
- PHP,
- Python,
- Kotlin,

it becomes very easy for the meaning of one field or one state to drift silently over time.

For example:
- one client may start interpreting `pending` differently from the other,
- the server may return a new field but only one client uses it correctly,
- a fasttrack payload may gain a new meaning while the documentation stays silent.

`protocol.md` is meant to ensure that meaning is defined centrally, not merely inferred from whatever the current implementation does.

---

### 3. Safer design of changes
When adding a new feature, development should not begin directly in code.

The correct sequence is:

1. think through the change at protocol level,
2. record it in `protocol.md`,
3. only then change clients and server.

This separates:
- **system behavior design**
from
- **technical implementation in a specific language**.

That significantly reduces chaos.

---

### 4. Better reviews and future refactors
If you later:
- rewrite the server,
- change the desktop stack,
- add a Windows client,
- build a CLI,
- or split the monorepo,

`protocol.md` lets you preserve the same communication contract even if the implementation changes underneath it.

That is critical.

Code can change.  
The protocol should change only when system behavior intentionally changes.

---

## What `protocol.md` is and what it is not

### `protocol.md` is:
- a communication-layer specification,
- a description of the meaning of requests, responses, and states,
- a contract between components,
- a foundation for compatibility,
- a foundation for future extension.

### `protocol.md` is not:
- a README,
- an onboarding document,
- an implementation tutorial,
- a TODO list,
- a roadmap,
- a description of internal code structure,
- a replacement for architecture documentation.

---

## The relationship between `protocol.md` and the implementation

The correct relationship is this:

- the implementation **fulfills** the protocol,
- the implementation may be refactored,
- but the meaning of the protocol should remain stable until it is intentionally changed.

That means:

- a controller refactor should not change the protocol,
- rewriting a client in another language should not change the protocol,
- a UI change should not change the protocol,
- changing internal persistence should not change the protocol.

By contrast:

- a new API field,
- a new transfer state,
- a changed meaning of `delivery_state`,
- a new fasttrack `fn`,
- a new compatibility rule,

**is a protocol change** and should be recorded in `protocol.md` first.

---

## How to use `protocol.md` during development

### When adding a new feature
Ask:

- Is a new endpoint needed?
- Is a new field needed?
- Does the meaning of an existing field change?
- Is a new state being introduced?
- Does the order of operations change?
- Does compatibility between clients and server change?

If the answer is yes, the change belongs in `protocol.md` first.

---

### When modifying existing behavior
Ask:

- Is this purely an internal refactor?
- Or is externally observable behavior changing?

If the change is visible through wire format, response shape, or state semantics, it belongs in `protocol.md`.

---

### When debugging a bug
Ask:

- Is the bug in the implementation?
- Or is the protocol itself insufficiently defined?

If two parts of the system behave differently and both behaviors could be defended, the problem is often not just in the code, but in the fact that the protocol was not defined precisely enough.

In that case, `protocol.md` should be corrected as well, not just the code.

---

## What the standard workflow should be in the future

This is the recommended workflow for future development:

### 1. Design the change
Describe the change at the level of system behavior.

### 2. Record it in `protocol.md`
If the change affects communication or state meaning, record it in the protocol specification.

### 3. Add companion explanation if needed
If the change is more complex, add a companion document like this one so the rationale and broader meaning are clear.

### 4. Implement it
Only then modify server, Android, and desktop.

### 5. Verify it
Finally, verify that the implementation conforms to the protocol, not the other way around.

---

## What style `protocol.md` should have

For long-term usefulness, the document should be:

- precise,
- not verbose,
- unambiguous,
- technical,
- stable,
- focused on observable behavior.

It should avoid:
- vague wording,
- marketing language,
- implementation details that are not part of the contract,
- speculation,
- future wishes without a clear proposal.

---

## What should live in other documents

To keep `protocol.md` from filling up with things that do not belong there, different kinds of knowledge should remain separate:

### `README.md`
What the project does, who it is for, how to install it.

### `protocol.md`
How the parts of the system communicate.

### `explain.protocol.md`
Why the protocol exists, what it means, and how to use it.

### `architecture.md`
How the project is internally structured and how responsibilities are split.

### `roadmap.md`
What is planned for the future.

### `decision/*.md` or `adr/*.md`
Why specific technical decisions were made.

---

## Why companion documents like this are useful

A formal specification is good for precision.  
But it often does not explain:
- why it was introduced,
- what problem it solves,
- how it should be used during development,
- where its boundaries are,
- how it relates to other documents.

That is why `explain.*.md` documents are useful.

These documents have a different role from the specification itself:

- they are not the source of truth for wire format,
- but they explain purpose, meaning, and usage rules.

This becomes especially useful when you come back to the document after a long time and do not want to reconstruct from scratch why it exists.

---

## Recommendation for the future

For future documents of this kind, it is reasonable to keep the following pair:

- **formal document** — what exactly is true,
- **explain document** — why it exists and how to work with it.

Examples:

- `protocol.md` + `explain.protocol.md`
- `architecture.md` + `explain.architecture.md`
- `storage.md` + `explain.storage.md`
- `sync.md` + `explain.sync.md`

This creates a system where:
- one layer states exactly **what**,
- the second layer explains **why** and **how to read it**.

That is far more maintainable than mixing everything into one file.

---

## Summary

`protocol.md` should remain, over the long term:

- a formal contract between components,
- the basis of compatibility,
- the place where communication changes are designed first,
- a reference point during refactors,
- protection against ambiguity and divergence between clients and server.

`explain.protocol.md` exists so that it is clear:

- why this matters,
- how to work with it,
- what belongs in the protocol,
- and how to keep using this style of documentation in the future.

Practically:

**first define the meaning, then write the implementation.**
