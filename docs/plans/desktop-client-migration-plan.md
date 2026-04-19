# desktop-client-migration-plan.md

## Purpose

This document provides a concrete migration plan for making the Desktop Connector Linux desktop client more professional and more maintainable.

It focuses on the desktop client only.

The main question it answers is:

**How should the Linux desktop client evolve from the current Python + pystray + GTK subprocess architecture into a stronger long-term desktop architecture?**

This document proposes two realistic paths:

1. **Recommended pragmatic path:** Python -> PySide6 (Qt for Python)
2. **Recommended long-term architecture path:** Rust core + Qt desktop shell

It also explains:

- why the current architecture is structurally limited,
- what should be preserved,
- what should be replaced,
- how to migrate incrementally,
- and what decision points matter most.

---

## Current problem

The current desktop client is not weak because it is written in Python.  
It is weak because its desktop architecture is shaped by a workaround-heavy UI/runtime composition.

The current desktop design includes:

- Python runtime
- `pystray` tray behavior
- GTK4/libadwaita windows launched as subprocesses
- explicit separation caused by GTK3/GTK4 conflict concerns
- desktop-specific integrations spread across multiple mechanisms

This works, but it creates several long-term problems:

- tray/runtime behavior is not unified under one desktop toolkit
- windowing behavior is more fragile than it should be
- the UI stack is harder to reason about
- packaging is harder to make feel polished
- future platform expansion becomes more expensive
- maintainability suffers as the app grows

The issue is therefore not “Python bad”.  
The issue is:

**the desktop toolkit/runtime architecture is not an ideal long-term foundation for a tray-first desktop application.**

---

## What a stronger desktop client should achieve

A more professional desktop client should provide these qualities:

### 1. One coherent desktop toolkit
Tray, windows, dialogs, clipboard-adjacent UX, and desktop actions should ideally live in one coherent desktop model.

### 2. Cleaner runtime architecture
The app should not depend on process splits and toolkit workarounds as a core architectural pattern.

### 3. Better packaging story
The application should be easier to package and present as a serious desktop product.

### 4. Better long-term maintainability
The next wave of features should become cheaper to add, not more expensive.

### 5. Better future platform readiness
Even if Linux stays the primary desktop target, the architecture should not make a future Windows client unreasonably painful.

---

## Recommendation summary

## Recommended practical path
**Move the desktop client to Qt using PySide6 first.**

This gives the best ratio of:

- improved professionalism,
- lower migration risk,
- faster delivery,
- lower rewrite cost,
- and better packaging/UI consistency.

This path keeps Python for now, while replacing the most fragile part of the current architecture: the desktop UI/toolkit composition.

---

## Recommended long-term architecture path
**Move toward a Rust core + Qt desktop shell.**

This gives the strongest long-term technical architecture if the project grows into:

- a more ambitious desktop application,
- a stronger background/runtime service,
- a more complex cross-platform desktop client,
- and eventually a Windows desktop target.

This path is stronger long-term, but more expensive and slower.

---

## Why Qt is the best fit

Qt is the best fit for this project because the app is not just a simple windowed GUI.

It is a desktop utility that needs:

- tray integration
- menu interaction
- dialogs
- external file/URL opening
- embedded web content support
- native desktop behavior
- reliable cross-platform desktop primitives
- mature Linux support
- future Windows path

That profile strongly matches Qt.

This is especially true for a tray-first utility application.

---

## Why not keep the current Python stack as-is

Keeping the current stack would only make sense if:

- the app remained very small,
- UI growth stayed minimal,
- the project remained Linux-only in a narrow sense,
- and the current workaround architecture did not continue to spread.

That is not the direction the project appears to be taking.

The project already behaves more like a product-grade desktop utility than a small script-wrapper app.  
Its desktop architecture should start matching that reality.

---

## Why PySide6 first is the best pragmatic move

Moving to PySide6 first gives the project these advantages:

- much better desktop coherence
- no need to immediately rewrite protocol/business logic
- one desktop toolkit for tray + windows + dialogs
- lower migration risk than a full language rewrite
- easier route to a more polished Linux application
- future Qt experience that still helps if Rust + Qt comes later

It is the best “improve architecture without setting the whole project on fire” option.

---

## Why Rust + Qt may still be the best final destination

Rust + Qt becomes attractive if the project eventually wants:

- a stronger systems-language core
- tighter control over runtime behavior
- more predictable concurrency and ownership boundaries
- cleaner long-term background/service logic
- stronger cross-platform desktop ambitions
- less dependence on Python packaging/runtime concerns

But that should be treated as a **long-term destination**, not necessarily the first migration step.

---

## Migration paths

# Path A — Python -> PySide6 (recommended first)

## Goal
Keep Python for now, but rebuild the desktop shell on a better toolkit.

## What changes
Replace:
- `pystray`
- GTK4 subprocess windows
- ad hoc desktop glue

With:
- Qt/PySide6 tray
- Qt windows
- Qt dialogs where appropriate
- Qt desktop-services integrations
- optional Qt web view where needed

## What stays
Keep as much as possible of:
- protocol logic
- transfer logic
- API client logic
- connection logic
- history logic
- pairing logic
- business behavior
- current feature semantics

## Why this path is strong
This path attacks the biggest architectural weakness first without forcing a full rewrite of the desktop runtime logic.

---

# Path B — Rust core + Qt desktop shell (recommended long-term)

## Goal
Split the desktop client into:
- a Rust runtime/core layer
- a Qt desktop shell

## What changes
Move into Rust over time:
- transfer orchestration
- poller/runtime logic
- message handling
- background logic
- stateful desktop client core
- possibly config/history core logic

Use Qt for:
- tray
- windows
- dialogs
- shell/open behavior
- embedded map/history web content
- native desktop UX

## What stays conceptually
The protocol and product behavior stay the same.

## Why this path is strong
This gives the cleanest long-term desktop architecture, especially if the project later wants a Windows client.

---

## Detailed recommendation

## My recommendation
Use **Path A first**, and leave the project explicitly open to **Path B later**.

That means:

1. Rebuild the desktop UI/toolkit shell in PySide6
2. Keep Python business logic during the first major migration
3. Introduce stronger internal boundaries while doing that
4. Reassess later whether the runtime/core should move to Rust

This avoids doing two difficult migrations at once:
- language/runtime rewrite
- desktop toolkit rewrite

Doing both at the same time would be the highest-risk route.

---

## Why not jump directly to Rust

A full Rust rewrite is tempting because it sounds “more serious,” but that instinct is often wrong.

The biggest current weakness is not Python compute performance.  
It is architectural coherence at the desktop-shell level.

You get more immediate professionalization by fixing the desktop toolkit and runtime composition first.

---

## Recommended target architecture

## For the next serious iteration
A strong near-term architecture would look like:

- **Python core/app logic**
- **PySide6 desktop shell**
- explicit platform backends
- explicit startup runners
- explicit command/message layer
- Qt tray + windows + dialogs + shell integration
- web view for map/history-style content if desired

This is already a major step up in professionalism.

---

## Long-term target architecture
A strong longer-term architecture would look like:

- **Rust desktop core**
- **Qt desktop shell**
- platform abstraction boundary
- Linux implementation now
- Windows implementation later
- compatibility layer and diagnostics already in place

That is likely the best final architecture if the project keeps growing.

---

## Concrete migration plan for Path A (Python -> PySide6)

## Phase 1 — define the Qt shell boundary
Before replacing UI code, define what the desktop shell is responsible for.

It should own:
- tray
- windows
- dialogs
- app menu interactions
- platform actions like open URL/open folder
- possibly clipboard-adjacent interactive UX
- optional embedded web content

It should not own:
- protocol semantics
- transfer semantics
- connection semantics
- business rules

### Deliverable
A clean internal boundary between:
- desktop shell
- app/core logic

---

## Phase 2 — replace tray runtime with Qt tray
Move tray behavior from `pystray` to Qt tray.

This is one of the highest-value changes because the app is tray-first.

### Deliverable
A Qt-based tray implementation with:
- menu
- icon state updates
- action dispatch
- notification integration where appropriate

---

## Phase 3 — move pairing/settings/history/send windows to Qt
Replace GTK4 subprocess windows with Qt windows.

Do this one surface at a time:

1. pairing window
2. send-files window
3. settings window
4. history window
5. find-phone window

### Deliverable
All desktop windows live in one coherent toolkit and no longer require the current workaround-based split.

---

## Phase 4 — replace dialog layer
Replace `zenity`-style or ad hoc dialog launching with Qt-native dialogs where appropriate.

### Deliverable
A unified dialog layer:
- file pickers
- confirmation dialogs
- message dialogs where needed

---

## Phase 5 — replace shell/open integration with Qt-native desktop services
Move open URL / open folder behavior into the Qt shell boundary.

### Deliverable
Qt-based external open handling with less toolkit fragmentation.

---

## Phase 6 — evaluate embedded map/web content
If embedded map/history or similar functionality remains useful, implement it inside the Qt shell using a web-view strategy.

### Deliverable
A consistent UI story for map/history-related content.

---

## Phase 7 — clean packaging and Linux polish
After the shell migration stabilizes:

- improve desktop packaging
- improve app identity/assets
- improve launcher behavior
- improve desktop-file/autostart consistency
- review release/build process for a more polished Linux story

### Deliverable
A much more professional Linux desktop client experience.

---

## Concrete migration plan for Path B (Rust core + Qt shell)

## Phase 1 — do Path A first or at least define the shell boundary
Even if Rust is the long-term target, the shell boundary should be made explicit first.

The project needs a clean split between:
- desktop shell
- app core

before moving core logic out of Python.

---

## Phase 2 — identify which logic belongs in Rust
Good candidates for Rust first:

- runtime orchestration
- polling and background lifecycle
- message dispatch core
- command handling core
- transfer-state orchestration
- possibly config/state services if they are heavily used by the runtime

Not good first candidates:
- rapidly changing UI concerns
- platform-specific UX details
- shell/window code

---

## Phase 3 — define the Rust/Python or Rust/Qt boundary
Choose how the desktop shell will talk to the Rust core.

This needs to be explicit:
- what commands the shell can send,
- what events the core emits,
- what state the shell observes,
- how async/runtime behavior is surfaced.

Do not bury this inside random glue code.

---

## Phase 4 — migrate runtime core incrementally
Move core modules one area at a time rather than attempting a big-bang rewrite.

Suggested order:
1. message/command core
2. poller/runtime loop
3. transfer orchestration
4. delivery tracking
5. config/history access if beneficial

---

## Phase 5 — stabilize diagnostics and protocol compatibility
The Rust migration should preserve:
- diagnostic event model
- protocol compatibility checks
- command/message semantics

If these are already explicit, the migration becomes much safer.

---

## Phase 6 — retire Python runtime core if justified
Only once the Rust core clearly replaces the Python runtime layer should the old Python core be retired.

The Qt shell can remain as the UI layer.

---

## Decision matrix

## Choose Path A first if:
- you want a more professional Linux client soon
- you want lower migration risk
- you want to keep current app logic mostly intact
- you want to eliminate the current tray/toolkit workaround architecture first
- you are not ready to rewrite core logic in a systems language yet

## Choose Path B first only if:
- you are fully committed to a larger rewrite
- you want the strongest long-term architecture immediately
- you are comfortable paying much higher migration cost now
- you are explicitly planning a serious multi-platform desktop future
- you are willing to move more slowly in the short term

---

## What not to do

## 1. Do not keep the current architecture and only polish UI
That would improve the surface but preserve the core desktop-architecture weakness.

## 2. Do not rewrite everything at once
A full toolkit + language + packaging rewrite at the same time is the highest-risk path.

## 3. Do not start with Windows
Do not try to solve Windows first from the current Linux-shaped architecture.

Fix the desktop architecture first.  
Then Windows becomes a platform implementation problem instead of a structural crisis.

## 4. Do not over-abstract before replacing the shell
The architecture should become cleaner, but the real value comes from replacing the current desktop-shell composition, not from building abstract layers in a vacuum.

---

## Risks

## Path A risks
- some Python architectural weaknesses will remain
- migration may temporarily duplicate UI code
- Qt packaging/polish still needs deliberate work
- you may still want Rust later

## Path B risks
- much larger rewrite cost
- slower feature progress
- boundary design mistakes become more expensive
- easier to stall in migration
- much more integration work up front

---

## Recommended final decision

## Best near-term decision
**Rebuild the Linux desktop client in PySide6 while keeping most Python core logic initially.**

That is the best move if the goal is:

- more professional desktop feel
- stronger architecture
- lower migration risk
- faster delivery
- clearer route toward future growth

## Best long-term destination
**Aim toward Rust core + Qt shell only after the Qt shell architecture exists and is stable.**

That gives you:
- a serious desktop toolkit
- a coherent tray-first app model
- a manageable first migration
- and a realistic path toward a stronger long-term desktop runtime

---

## Practical milestone sequence

A realistic milestone sequence would be:

### Milestone 1
Architecture preparation
- define shell/core boundary
- define migration scope
- freeze current desktop feature inventory

### Milestone 2
Qt shell prototype
- tray
- menu
- one window
- basic action wiring

### Milestone 3
Qt shell production migration
- all main windows moved
- dialogs moved
- open/shell behavior moved
- current GTK subprocess pattern retired

### Milestone 4
Linux polish
- packaging
- launcher/autostart
- visual quality
- stability pass

### Milestone 5
Post-migration review
- decide whether Python core is now “good enough”
- or whether Rust core migration is justified

### Milestone 6 *(optional, later)*
Rust core migration
- only if the project’s scale and goals justify it

---

## Final conclusion

If the goal is to make the Linux desktop client **more professional**, the right first move is not “replace Python because Python is embarrassing.”

The right first move is:

**replace the current desktop-shell architecture with a coherent Qt-based desktop client.**

That means:

- **PySide6 first**
- **Rust later only if justified**

This path gives the best combination of:
- realism
- quality
- maintainability
- and future flexibility
