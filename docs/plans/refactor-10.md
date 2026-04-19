# refactor-10.md

> **Status: Done** — landed on `main` in commit `46e423e` (PR #16). New `desktop/src/platform/contract/` (`DesktopPlatform` + `PlatformCapabilities`) as the first-class boundary; `desktop/src/platform/compose.py` with `compose_desktop_platform()` that raises `NotImplementedError` on non-Linux (no silent fallback); `desktop/src/platform/linux/compose.py` returns a `DesktopPlatform(name="linux", …)` directly — no subclass layer. `StartupContext.platform`, `Poller`, `TrayApp`, `run_receiver`, and `dependency_check` all consume the contract instead of backend bundles. `PlatformCapabilities` is load-bearing: `Poller` gates auto-open-URL on `capabilities.auto_open_urls`; tray menu gates "Send Clipboard" on `capabilities.clipboard_text` and "Open Save Folder" on `capabilities.open_folder`. `platform/__init__.py` re-exports only the contract so importing `DesktopPlatform` does not transitively load Linux backends (pinned by `test_contract_import_does_not_load_linux_impl`). `docs/ROADMAP-windows-client.md` carries the refactor-10 gap map as the leading status section with the concrete phase 4–8 Windows implementation plan preserved underneath. 17/17 protocol tests pass; smoke-tested `clipboard.text` with a URL from android → desktop auto-opens in the browser.

## Refactor 10 / 10
# Prepare for a Windows desktop client through a platform abstraction boundary

## Why this is the tenth refactor

This is intentionally the last refactor in the sequence.

A Windows desktop client is not the kind of goal that should be approached by simply "starting to code Windows support" on top of an architecture that is still tightly coupled to:

- Linux startup assumptions,
- Linux platform tools,
- Linux tray/runtime behavior,
- Linux file-manager integration,
- implicit desktop-specific flow boundaries.

By the time this refactor is reached, the project should already have:

- cleaner server boundaries,
- cleaner request handling,
- cleaner persistence boundaries,
- a clearer internal transfer lifecycle,
- a thinner desktop bootstrap,
- clearer platform backend separation,
- a more unified command/message model,
- a protocol compatibility layer,
- and better diagnostics.

That is what makes it reasonable to prepare for a second desktop platform.

The purpose of this refactor is **not** to implement the Windows client itself.  
Its purpose is to make the architecture genuinely ready for it.

---

## Relation to the previous refactors

This refactor depends on the previous nine steps.

It only makes sense after:

- bootstrap is no longer the main desktop bottleneck,
- Linux-specific behavior is already isolated from desktop core,
- protocol behavior is clearer,
- command semantics are more unified,
- diagnostics are more consistent.

Without that groundwork, "preparing for Windows" would mostly mean creating more duplication and more architectural debt.

This refactor is therefore the final structural preparation step before any serious Windows implementation effort.

---

## Position within the full sequence of 10 refactors

The full sequence is now complete:

1. Split the transfer domain into smaller server services without changing the protocol
2. Introduce an explicit server layer for auth, request context, and input validation
3. Introduce a repository layer above SQLite access
4. Formalize internal transfer states and state transitions
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. Separate desktop core from Linux-specific backends
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. **Prepare for a Windows desktop client through a platform abstraction boundary**

This document covers **only item 10**.

---

## Main goal

Move the desktop application from the current shape:

- Linux is still the only real desktop runtime model,
- platform boundaries exist but are still Linux-shaped in places,
- desktop core is cleaner but not yet fully platform-neutral,
- some assumptions still reflect Linux-first execution and packaging,

to this shape:

- desktop core has an explicit platform-neutral boundary,
- platform capabilities are modeled intentionally,
- Linux becomes one implementation of a desktop platform contract,
- Windows can later become another implementation,
- future Windows work can proceed by filling defined gaps rather than untangling the architecture again.

---

## What must not change

This refactor **must not change protocol behavior**.

It should also avoid unnecessary user-visible changes on Linux.

That means no change to:

- transfer behavior,
- pairing behavior,
- clipboard semantics,
- message semantics,
- tray behavior as seen by Linux users,
- protocol-visible behavior,
- server behavior,
- Android behavior.

This is a preparation refactor, not a Windows feature release.

---

## Why this matters

### 1. "Platform-neutral later" is expensive if not designed explicitly
Projects often say:
- "we can always add Windows later"

but in practice that becomes expensive if the architecture still assumes:

- Linux launch paths,
- Linux toolchain behavior,
- Linux desktop conventions,
- Linux filesystem conventions,
- Linux notification and clipboard expectations,
- Linux packaging/install flows.

This refactor exists so that "later" remains realistic.

---

### 2. A second desktop platform changes architecture pressure
Supporting a second desktop platform is not just about replacing:
- clipboard API,
- notification API,
- tray API.

It also affects:
- startup composition,
- environment detection,
- capability availability,
- config defaults,
- packaging assumptions,
- update/install assumptions,
- external file opening behavior,
- command execution behavior.

The architecture needs to make room for those differences deliberately.

---

### 3. Cross-platform readiness is not the same thing as cross-platform implementation
It is possible to prepare properly for Windows without writing the Windows client immediately.

That preparation is valuable because it turns future work from:
- "reopen architectural questions while implementing features"

into:
- "implement a defined platform contract"

That is a much healthier starting point.

---

## Core architectural idea

The desktop application should treat "platform" as an explicit runtime dimension.

That means there should be a deliberate boundary between:

### 1. Desktop core
What the app does as a desktop client:
- pairing
- transfer orchestration
- delivery tracking
- config/state decisions
- command/message handling
- protocol interaction
- history management
- app-level behavior

### 2. Platform contract
What the platform must provide:
- clipboard access
- notification transport
- dialog/file picker support
- shell/open behavior
- tray integration
- startup environment support
- optional file-manager integration
- platform metadata/capabilities
- installer/update hooks if needed later

### 3. Platform implementation
How that contract is implemented on:
- Linux
- later Windows

This refactor is about making that middle layer explicit and complete enough.

---

## What should be introduced

### 1. A first-class desktop platform contract
The project should define an explicit contract for what a desktop platform implementation must provide.

This may be represented as:

- a `DesktopPlatform` object,
- a group of explicit backend interfaces,
- or a platform service bundle.

The important point is that the contract should be complete enough to support a second desktop runtime.

It should cover at least:

- clipboard backend
- notification backend
- dialog backend
- shell/open backend
- tray backend
- file-manager integration backend
- platform capability reporting
- startup/runtime environment helpers where needed

---

### 2. Platform capability model
Different desktop platforms may not support all capabilities in the same way.

The architecture should explicitly model things such as:

- clipboard text support
- clipboard image support
- tray availability
- notification availability
- file-manager integration support
- background-service assumptions
- auto-open behavior support
- installer terminal equivalent support

This allows the app to reason in terms of:
- "capability available"
instead of:
- "Linux probably has this".

---

### 3. Explicit Linux platform implementation package
Linux should be repositioned architecturally as:

- one platform implementation,
- not the assumed desktop runtime.

That means Linux-specific code should live inside a clearly identified platform implementation area and satisfy the desktop platform contract explicitly.

This is important psychologically as well as structurally.

---

### 4. A Windows-target gap map
The project should produce an explicit list of what is still missing for Windows support.

This should not be vague.  
It should identify:

- what contracts already exist,
- what Linux assumptions still leak,
- what capabilities lack neutral modeling,
- what app flows depend on Linux behavior,
- what runtime or packaging problems remain unresolved.

This gap map turns future Windows implementation into a concrete plan rather than a speculative idea.

---

### 5. Platform composition by target, not by ad hoc imports
Startup/bootstrap should select platform implementations in one place based on target environment.

For now that will still select Linux.

The important point is that selection should become explicit enough that future Windows composition has a place to plug in without revisiting bootstrap design from scratch.

---

## Recommended platform contract areas

A practical first-class desktop platform contract should cover the following areas.

### 1. Clipboard
Must define support for:
- read text
- write text
- write image
- capability detection where needed

This area likely already exists conceptually after refactor 6, but now it should be framed as part of a complete desktop platform contract rather than as an isolated Linux-boundary cleanup.

---

### 2. Notifications
Must define:
- send notification
- optional notification support/capability detection
- failure behavior

The contract should not encode Linux assumptions such as `notify-send` style behavior.

---

### 3. Dialogs
Must define:
- file selection
- confirmation dialogs
- optional message dialogs if needed
- cancellation semantics

The contract should describe the app-level need, not the Linux tool used.

---

### 4. Shell/open behavior
Must define:
- open URL
- open folder
- launch external installer flow or equivalent
- possibly reveal file behavior if needed later

This is often a platform-sensitive area and should be explicitly modeled.

---

### 5. Tray runtime
Must define:
- tray start
- tray stop
- action wiring expectations
- capability assumptions
- fallback expectations if tray is unavailable later on some platform

Tray behavior is often one of the largest sources of platform pain.  
It should be deliberately modeled, not treated as a Linux-specific implementation detail only.

---

### 6. File-manager integration
Must define whether platform-level integration such as right-click "Send to Phone" is:

- supported,
- unsupported,
- or optional.

This should become a capability-driven area rather than an implicit Linux feature.

Windows may need a different approach here, and the architecture should make room for that.

---

### 7. Platform identity and capabilities
The app should be able to ask the platform object things such as:

- platform name
- supported capabilities
- optional feature support
- startup/runtime constraints if relevant

This makes future platform-specific branching cleaner and more explicit.

---

## Suggested target structure

A practical target shape could be:

```text
desktop/src/
  platform/
    contract/
      desktop_platform.py
      capabilities.py
      clipboard.py
      notifications.py
      dialogs.py
      shell.py
      tray.py
      file_manager.py

    linux/
      platform.py
      clipboard_backend.py
      notification_backend.py
      dialog_backend.py
      shell_backend.py
      tray_backend.py
      file_manager_backend.py

    compose.py
```

Alternative structure is also fine, but the important parts are:

- explicit contract
- explicit Linux implementation
- explicit composition point
- clear place for future Windows implementation

For example, later it should be obvious where this would go:

```text
desktop/src/platform/windows/
  platform.py
  clipboard_backend.py
  ...
```

even if those files do not exist yet.

---

## What the first iteration should include

To keep this refactor practical, the first iteration should focus on architectural readiness, not platform completeness.

### Required in the first iteration
At minimum, introduce:

- a first-class `DesktopPlatform` contract or equivalent bundle,
- explicit platform capability reporting,
- explicit Linux platform composition as one implementation of the contract,
- a Windows gap map document,
- elimination of remaining Linux-first assumptions from desktop core where practical.

### Strongly recommended
Also introduce:

- a small startup-time platform selection mechanism,
- a list of platform-specific UX assumptions that still need future review,
- clear markers for what remains Linux-only by design.

### Not required yet
The first iteration does **not** need:

- an actual Windows backend implementation,
- Windows tray code,
- Windows clipboard code,
- Windows packaging,
- Windows installer/update story,
- Windows-specific file-manager integration,
- cross-platform CI.

This refactor is about readiness, not delivery.

---

## Concrete execution plan

## Phase 1 — define `DesktopPlatform` contract
Create one explicit platform contract that bundles the required desktop capabilities.

### Goal
Stop thinking in terms of "a collection of Linux backends" and start thinking in terms of "a desktop platform implementation".

### Deliverable
For example:
- `DesktopPlatform`
- with fields/services for clipboard, notifications, dialogs, shell/open, tray, file-manager integration, and capability reporting

---

## Phase 2 — define platform capabilities model
Introduce a clear capabilities representation.

### Goal
Allow core logic to reason about what the platform supports without assuming Linux semantics.

### Deliverable
For example:
- `PlatformCapabilities`
- capability flags or descriptors for:
  - tray support
  - clipboard image support
  - file-manager integration support
  - notification support
  - installer-launch support
  - etc.

---

## Phase 3 — reframe Linux as one platform implementation
Create a Linux platform implementation object that satisfies the contract explicitly.

### Goal
Make Linux "one implementation" rather than "the environment the app secretly assumes".

### Deliverable
For example:
- `LinuxDesktopPlatform`

This can compose the Linux backends already extracted earlier.

---

## Phase 4 — add platform composition module
Add one explicit place where the app selects the platform implementation.

### Goal
Prepare for future target selection without changing bootstrap structure again.

### Deliverable
For example:
- `platform/compose.py`
- currently selecting Linux only

Later this is where Windows can be added.

---

## Phase 5 — remove remaining Linux-first assumptions from core
Audit desktop core and reduce remaining assumptions such as:

- Linux-only shell behavior assumptions
- Linux-only installer assumptions
- Linux-only file-manager assumptions
- Linux-only tray expectations
- Linux-only capability expectations

### Goal
Core should ask the platform, not assume the answer.

---

## Phase 6 — produce Windows gap map
Create an explicit document identifying what remains between the current architecture and a real Windows client.

### Goal
Turn "Windows later" into a concrete engineering plan.

### Deliverable
For example:
- `docs/ROADMAP-windows-client.md` update
- or `docs/windows-gap-map.md`

This should include:
- already-ready areas
- partially-ready areas
- unresolved areas
- likely difficult areas
- non-goals for first Windows version

---

## Recommended commit order

### Commit 1
`refactor(platform): introduce DesktopPlatform contract`

Contents:
- first-class platform contract
- capability bundle definition

### Commit 2
`refactor(platform): introduce explicit PlatformCapabilities model`

Contents:
- capability representation
- platform-neutral capability checks

### Commit 3
`refactor(platform): implement LinuxDesktopPlatform`

Contents:
- Linux repositioned as one platform implementation
- existing Linux backends composed under platform object

### Commit 4
`refactor(platform): add startup-time platform composition`

Contents:
- explicit platform selection/composition module
- Linux selected for now

### Commit 5
`refactor(core): remove remaining Linux-first assumptions from desktop core`

Contents:
- audit and cleanup pass
- core uses platform contract instead of assumptions where practical

### Commit 6
`docs(platform): add Windows gap map and platform readiness notes`

Contents:
- explicit document for what remains before Windows implementation

---

## What should not be addressed here

This refactor **should not** address:

- implementing the Windows desktop client,
- Windows UI toolkit selection,
- Windows tray implementation,
- Windows clipboard implementation,
- Windows packaging,
- Windows installer/update strategy in full,
- platform-specific UX redesign,
- protocol changes.

Those are downstream tasks.

This refactor is about making those future tasks possible without reopening core architecture.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. A first-class desktop platform contract exists
The app has a clear platform-level abstraction, not just a collection of isolated backends.

### 2. Linux is explicitly one implementation of that contract
Linux is no longer treated architecturally as the implicit default shape of desktop core.

### 3. Platform capabilities are explicit
Core can reason about platform support through a capability model rather than assumptions.

### 4. Platform composition is centralized
There is one place where desktop platform implementation is selected and composed.

### 5. Remaining Linux-first assumptions are reduced
Desktop core is more platform-neutral than before.

### 6. A Windows gap map exists
The project has an explicit document showing what remains before actual Windows support.

### 7. Linux behavior remains unchanged
Linux users do not experience feature regressions from this preparation refactor.

---

## Test checklist

After each major phase, verify:

### Platform contract behavior
- clipboard capability still behaves the same on Linux
- notifications still behave the same on Linux
- dialogs still behave the same on Linux
- shell/open behavior still behaves the same on Linux
- tray behavior still behaves the same on Linux
- file-manager integration still behaves the same on Linux

### Platform capability behavior
- core can query capability support without breaking current flows
- unsupported/optional capability handling remains sane where relevant

### Startup composition
- Linux platform composition still starts correctly
- pairing, send, headless receive, and tray receive still work
- no bootstrap regressions are introduced

### Core/platform boundary
- core modules rely on platform contract more explicitly than before
- remaining Linux assumptions are visible and easier to identify

### Windows gap map usefulness
- the document clearly distinguishes:
  - architecture-ready areas
  - missing implementation areas
  - unresolved design choices
  - likely difficult items

---

## Risks

### 1. Prematurely designing for an imaginary platform
It is possible to over-abstract around Windows before real implementation constraints are known.

That should be avoided.

The platform contract should be driven by real needs already visible in the current Linux app.

---

### 2. Treating all differences as backend-only
Some Windows differences may not be purely backend details.  
They may affect UX assumptions, tray lifecycle assumptions, packaging, or startup behavior.

The refactor should prepare for that by modeling capabilities and constraints explicitly, not by pretending all platforms are the same.

---

### 3. Creating a platform contract that is still Linux-shaped
If the contract bakes in Linux-specific behavior, the refactor will not achieve its purpose.

The contract should describe desktop capabilities, not Linux mechanisms.

---

### 4. Doing too much before actual Windows work starts
The goal is readiness, not speculative over-design.

It is better to create a clean contract and a good gap map than to build a giant abstraction for problems that may never materialize.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have full cross-platform desktop architecture already solved."

The correct result is:

- desktop platform is a first-class concept,
- Linux is one implementation of it,
- core is less platform-assumptive,
- Windows work has a defined place to attach later,
- there is a concrete map of what remains.

---

## Practical definition of done

Done looks like this:

- a platform contract exists,
- capabilities are explicit,
- Linux is composed as one platform implementation,
- platform selection is centralized,
- desktop core is less Linux-first,
- a Windows gap map exists,
- Linux behavior remains unchanged.

That is the purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the project will be:

- architecturally ready for a second desktop platform,
- less likely to reintroduce Linux coupling into core logic,
- better prepared for realistic Windows planning,
- clearer about what is still missing,
- easier to evolve without reopening foundational design questions.

That is why this refactor is tenth and final.

---

## Closing note

At this point, the 10-refactor sequence is complete.

If followed in order, the project should end up with:

- cleaner server architecture,
- clearer protocol boundaries,
- more explicit lifecycle semantics,
- cleaner desktop structure,
- cleaner platform boundaries,
- more unified command semantics,
- better protocol safety,
- better diagnostics,
- and a realistic architectural foundation for a future Windows desktop client.
