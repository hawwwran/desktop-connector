# refactor.md

## Overview

This document summarizes the full sequence of 10 planned refactors for the Desktop Connector project.

The purpose of this document is to provide:

- a single high-level view of the full refactor plan,
- a short explanation of what each refactor is about,
- and a clear explanation of why the refactors are ordered the way they are.

The sequence is deliberately structured so that each step reduces the cost and risk of the next one.

The general progression is:

1. clean up server structure,
2. formalize server behavior,
3. clean up desktop structure,
4. unify message semantics,
5. make protocol behavior safer,
6. improve diagnostics,
7. prepare for future platform expansion.

---

## Why this sequence exists

Large refactor plans often fail because they contain good ideas in the wrong order.

For example:

- introducing abstractions before boundaries are clear,
- adding repositories before service structure is stable,
- trying to prepare for Windows before Linux-specific assumptions are isolated,
- adding compatibility checks before the protocol is documented,
- or improving diagnostics before the system has a stable set of concepts worth diagnosing.

This sequence avoids that.

Each refactor is placed where it has the highest leverage and the lowest chance of becoming wasted work.

---

## The 10 refactors

## 1. Split the transfer domain into smaller server services without changing the protocol

### What it is about
This refactor breaks the overloaded transfer domain on the server into smaller focused services.

The current transfer area contains too many responsibilities in one place, such as:

- transfer initialization,
- chunk upload/download,
- delivery status,
- long polling,
- cleanup,
- wake behavior.

This refactor separates those concerns into clearer server-side service boundaries.

### Why it comes first
The transfer flow is one of the most complex and risk-prone parts of the system.  
As long as that area remains structurally overloaded, other refactors on top of it are less useful.

This refactor lowers the cost of almost every later server-side change.

### How it leads to the next refactor
Once the main domain logic is no longer overloaded inside controllers, it becomes worthwhile to clean up the HTTP/request boundary around it.

---

## 2. Introduce an explicit server layer for auth, request context, and input validation

### What it is about
This refactor cleans up the server's request pipeline.

It separates and standardizes:

- authentication,
- request context,
- JSON/raw body parsing,
- input validation,
- error shaping.

The goal is to stop mixing HTTP concerns directly into controller logic.

### Why it comes after #1
There is little value in cleaning up request handling if the domain behind it is still structurally overloaded.

Refactor 1 makes domain boundaries clearer.  
Refactor 2 then makes the request boundary cleaner and more explicit.

### How it leads to the next refactor
Once domain logic and request handling are both cleaner, persistence becomes the next obvious concern to isolate.

---

## 3. Introduce a repository layer above SQLite access

### What it is about
This refactor introduces an explicit persistence boundary.

Instead of letting services and controllers reach into raw SQL directly, repositories become responsible for:

- SQL access,
- row mapping,
- storage lookups,
- and persistence-specific query meaning.

### Why it comes after #2
Repositories add real value only when they sit under reasonably clear service and request boundaries.

If introduced too early, they tend to become shallow wrappers around SQL instead of a meaningful persistence layer.

### How it leads to the next refactor
Once persistence is explicit, the project can reason more clearly about the lifecycle and invariants of transfer state itself.

---

## 4. Formalize internal transfer states and state transitions

### What it is about
This refactor turns the transfer lifecycle from an implicit combination of flags and counters into an explicit internal state model.

It defines:

- internal transfer states,
- allowed transitions,
- invariants,
- and mapping to public protocol-visible status values.

### Why it comes after #3
You should not formalize lifecycle semantics while state behavior is still entangled with raw SQL access and overloaded service code.

Refactors 1–3 create the conditions for explicit lifecycle modeling.

### How it leads to the next refactor
Once the server side is structurally healthier and semantically clearer, attention can shift to the next major maintainability hotspot: the desktop startup boundary.

---

## 5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint

### What it is about
This refactor reduces the desktop bootstrap file to a real entrypoint.

Instead of mixing:

- dependency checks,
- logging setup,
- registration,
- pairing,
- send mode,
- headless mode,
- tray startup,
- signal handling,

inside one startup file, the flow is split into clearer runners and setup modules.

### Why it comes after #4
The first four refactors stabilize the server and protocol-facing side of the system.  
Once that is in better shape, the next best structural target is the desktop startup/orchestration boundary.

### How it leads to the next refactor
Once startup is cleaner, the next desktop architectural weakness becomes easier to isolate: Linux-specific runtime coupling.

---

## 6. Separate desktop core from Linux-specific backends

### What it is about
This refactor introduces a clearer boundary between desktop core behavior and Linux-specific integrations such as:

- clipboard tools,
- notifications,
- dialogs,
- shell/open actions,
- tray/platform glue,
- file-manager integration.

The goal is to make core desktop logic depend on capabilities rather than Linux implementation details.

### Why it comes after #5
Before platform boundaries can be cleaned up properly, startup and orchestration need to stop being one large mixed entrypoint.

Refactor 5 makes that possible.  
Refactor 6 then separates what is core desktop logic and what is Linux-specific behavior.

### How it leads to the next refactor
Once both server semantics and desktop platform boundaries are clearer, the next high-value unification point is the meaning of cross-device messages themselves.

---

## 7. Introduce a unified command/message model for `.fn.*` and fasttrack

### What it is about
This refactor unifies command-style semantics across two existing mechanisms:

- `.fn.*` special transfers,
- fasttrack messages.

Instead of treating them as two separate conceptual systems, the project introduces one internal command/message model with:

- shared semantic types,
- transport adapters,
- unified dispatch,
- and explicit handlers.

### Why it comes after #6
Unifying command semantics is easier once:

- transfer lifecycle semantics are cleaner,
- desktop platform coupling is reduced,
- and both server and desktop structure are less entangled.

Otherwise the message model tends to inherit old architectural messiness.

### How it leads to the next refactor
Once command semantics are unified, it becomes much easier to explicitly verify implementation behavior against the documented protocol.

---

## 8. Introduce a compatibility layer between `protocol.md` and the implementation

### What it is about
This refactor makes protocol conformance explicit.

Instead of relying on manual confidence that code still matches `protocol.md`, the project gains:

- protocol contract tests,
- canonical examples,
- compatibility rules,
- and clearer distinction between protocol-preserving, extending, and breaking changes.

### Why it comes after #7
Compatibility checks are more valuable once the system's command semantics and behavior model are already cleaner and more unified.

A messy implementation can be tested, but the tests are less meaningful and more fragile.

### How it leads to the next refactor
Once protocol compatibility is easier to check, the next best system-level improvement is making diagnostics equally easier to understand across runtimes.

---

## 9. Consolidate logging and diagnostic events across platforms

### What it is about
This refactor aligns logging and diagnostic event vocabulary across:

- server,
- desktop,
- Android.

The goal is to make cross-platform flows easier to reconstruct by using:

- shared event categories,
- clearer event names,
- more deliberate correlation IDs,
- and better severity discipline.

### Why it comes after #8
There is much more value in shared diagnostics once:

- protocol behavior is clearer,
- message semantics are more unified,
- and important flows are structured enough to describe consistently.

Otherwise logging standardization mostly makes confusing behavior easier to observe, not easier to reason about.

### How it leads to the next refactor
Once structure, semantics, compatibility, and diagnostics are all healthier, the project is finally in a realistic position to prepare for a second desktop platform.

---

## 10. Prepare for a Windows desktop client through a platform abstraction boundary

### What it is about
This refactor makes the desktop application genuinely ready for a future Windows client without implementing Windows support yet.

It introduces or completes:

- a first-class desktop platform contract,
- explicit platform capabilities,
- Linux as one platform implementation,
- centralized platform composition,
- and a Windows gap map documenting what is still missing.

### Why it comes last
Preparing for Windows too early would mostly create speculation and duplication.

Only after the previous nine refactors does the architecture become stable enough that "Windows readiness" means something real rather than aspirational.

### What it completes
This final step turns the whole sequence into a coherent architectural preparation plan:

- server structure is cleaner,
- state semantics are clearer,
- desktop core is cleaner,
- platform-specific code is better isolated,
- command semantics are unified,
- protocol compatibility is safer,
- diagnostics are stronger,
- and future desktop expansion has a defined place to attach.

---

## Why the sequence is ordered this way

The sequence is intentionally cumulative.

### Phase A — stabilize the server core
Refactors 1–4 focus on the server because that is where:

- protocol behavior is enforced,
- delivery semantics are defined,
- and a lot of system risk lives.

These steps clean up:
- domain structure,
- request structure,
- persistence structure,
- and lifecycle semantics.

### Phase B — stabilize the desktop structure
Refactors 5–6 focus on the desktop application's maintainability:

- first the startup/orchestration boundary,
- then the platform-specific boundary.

This prevents desktop architecture from becoming the next long-term bottleneck.

### Phase C — unify semantics and protect the protocol
Refactors 7–8 focus on higher-level correctness:

- command/message semantics become unified,
- protocol compatibility becomes explicit and testable.

This is the point where the project stops being just "structured code" and starts becoming a more disciplined system with a protected contract.

### Phase D — improve system-level operability
Refactor 9 improves visibility across the whole stack by making diagnostics more coherent and useful.

### Phase E — prepare for future platform expansion
Refactor 10 uses all the earlier work to make a future Windows client realistic without forcing that implementation prematurely.

---

## Summary

The purpose of this 10-refactor sequence is not to make the project "more abstract."

The purpose is to make it:

- easier to reason about,
- easier to change safely,
- easier to diagnose,
- safer against protocol drift,
- more maintainable across runtimes,
- and realistically expandable in the future.

In short:

- the early refactors reduce structural debt,
- the middle refactors reduce semantic and platform coupling,
- the later refactors improve safety and operability,
- and the final refactor prepares the project for future growth without forcing that growth too early.

That is why these 10 refactors are ordered exactly this way.
