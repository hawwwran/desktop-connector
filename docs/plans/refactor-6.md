# refactor-6.md

> **Status: Done** — landed on `main` in commit `79e9c31` (PR #13). New `desktop/src/interfaces/` (capability `Protocol`s for clipboard/dialogs/notifications/shell + `DesktopBackends` composition shape), `desktop/src/backends/linux/` (Linux implementations wrapping existing helpers), and `desktop/src/platform/linux/compose.py` (single composition point). `Poller`, `TrayApp`, and `run_receiver` now take `backends: DesktopBackends` as a required positional and route all platform calls through `self.backends.*` — core modules no longer import anything from `backends/linux/`. `dependency_check.py` documents its intentional pre-composition Linux scope; `windows.py` (GTK4 subprocess) instantiates `LinuxDialogBackend` directly. Behavior preserved; verified desktop ↔ android roundtrips.

## Refactor 6 / 10
# Separate desktop core from Linux-specific backends

## Why this is the sixth refactor

After refactor 5, the next highest-value step is to separate the desktop application's core behavior from Linux-specific integrations.

At the moment, the desktop side still mixes two different categories of logic:

- **core application behavior**, such as pairing, transfer orchestration, delivery tracking, config-driven decisions, and protocol interaction,
- **Linux-specific runtime behavior**, such as clipboard tools, notifications, tray integration, dialogs, terminal launching, and file-manager integration assumptions.

This is currently acceptable because the desktop target is Linux.  
However, it creates a structural limitation:

the application logic is more tightly coupled to Linux-specific mechanisms than it should be.

The purpose of this refactor is to introduce a cleaner boundary so that:

- core behavior becomes easier to reason about,
- Linux-specific integrations become isolated,
- future platform work becomes cheaper,
- and desktop features stop hard-coding platform assumptions into general application logic.

---

## Relation to the previous refactors

This refactor intentionally comes after refactor 5.

Refactor 5 makes the desktop bootstrap smaller and clearer.  
Once startup is no longer the main structural hotspot, the next maintainability boundary is the platform boundary itself.

That means the sequence is deliberate:

- refactor 5 cleans up **how the app starts**,
- refactor 6 cleans up **what parts of the app are platform-specific**.

Doing these in reverse order would make the platform split harder to apply consistently.

---

## Position within the full sequence of 10 refactors

The full sequence remains:

1. Split the transfer domain into smaller server services without changing the protocol
2. Introduce an explicit server layer for auth, request context, and input validation
3. Introduce a repository layer above SQLite access
4. Formalize internal transfer states and state transitions
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. **Separate desktop core from Linux-specific backends**
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 6**.

---

## Main goal

Move the desktop application from the current shape:

- core logic directly calling Linux-specific helpers,
- business flow implicitly depending on Linux tools,
- runtime behavior and platform behavior mixed together,
- no clear platform-service boundary,

to this shape:

- core desktop logic depends only on explicit capability interfaces or backend contracts,
- Linux-specific implementations live behind those contracts,
- feature logic no longer directly imports platform details,
- platform assumptions become isolated and replaceable.

---

## What must not change

This refactor **must not change protocol behavior** and should not change user-visible behavior except where tiny implementation-level side effects are unavoidable.

That means no change to:

- pairing behavior,
- send/receive behavior,
- transfer history semantics,
- delivery tracking semantics,
- clipboard transfer semantics,
- notification semantics,
- tray behavior as observed by users,
- settings behavior,
- right-click send integration behavior,
- CLI behavior.

This is a structural boundary refactor, not a feature redesign.

---

## Why this refactor matters

### 1. Core logic should not know how Linux does things
Core application behavior should care about operations such as:

- read clipboard text,
- write clipboard image,
- show notification,
- ask user to choose files,
- run tray app,
- open a URL,
- open a folder,
- launch dependency installer terminal.

It should not care whether that happens via:

- `wl-copy`,
- `xclip`,
- `notify-send`,
- `zenity`,
- `gnome-terminal`,
- `x-terminal-emulator`,
- or some future Windows/macOS mechanism.

Without this boundary, platform-specific behavior leaks into general logic.

---

### 2. Linux assumptions spread too easily
Once a project starts with Linux-only support, it is natural for Linux decisions to spread into:

- poller logic,
- runner logic,
- tray flow,
- dialog flow,
- clipboard flow,
- notification flow,
- file-received actions,
- startup failure handling.

That is manageable for a while, but expensive later.

This refactor creates a point where Linux assumptions are allowed to exist — and just as importantly, where they are not.

---

### 3. Future platform support becomes less painful
Even if Windows support is only a later goal, it is far cheaper to introduce platform boundaries before desktop logic is completely saturated with Linux-only calls.

This refactor is not about adding Windows now.  
It is about avoiding the need to untangle everything later under pressure.

---

### 4. Testing becomes easier
Core logic is easier to test when it depends on explicit backend contracts rather than concrete shell tools and UI binaries.

You do not need a full fake OS layer.  
You only need to stop tying core logic directly to Linux implementation details everywhere.

---

## Core architectural idea

Desktop core should speak in terms of **capabilities**, not platform mechanisms.

Examples of core-level capability language:

- clipboard backend,
- notification backend,
- dialog backend,
- shell/open backend,
- tray backend,
- file-manager integration backend,
- platform environment capability probe.

Linux implementations then satisfy those capabilities.

The core should not directly encode:
- "call `notify-send`",
- "call `xdg-open`",
- "use `zenity`",
- "use `wl-copy` or `xclip`".

Those belong to Linux-specific backend modules.

---

## What should be considered "desktop core"

The following areas should be treated as core or core-adjacent:

- transfer orchestration,
- delivery tracking,
- pairing orchestration,
- config handling,
- app state decisions,
- startup mode decisions,
- history updates,
- interpretation of `.fn.*` payload meaning,
- polling strategy,
- connection-state behavior,
- decision of *when* to notify or *when* to read/write clipboard.

The following areas should be treated as platform-specific:

- clipboard implementation,
- notification transport,
- tray/toolkit platform glue,
- file pickers and confirmation dialogs,
- folder opening / URL opening,
- launching terminal windows,
- file-manager integration installation details.

The core decides **that** something should happen.  
The platform backend decides **how** it happens on Linux.

---

## Recommended capability boundaries

At minimum, the desktop app should gain explicit backend boundaries for:

### 1. Clipboard backend
Operations such as:

- read text clipboard,
- write text clipboard,
- write image clipboard,
- detect availability / capability if useful.

This should isolate `wl-copy`, `xclip`, and any similar Linux mechanism details.

---

### 2. Notification backend
Operations such as:

- show generic notification,
- show file-received notification,
- show connection-lost notification,
- show connection-restored notification.

The core should not care that Linux currently uses `notify-send`.

---

### 3. Dialog backend
Operations such as:

- choose file(s),
- confirm action,
- maybe show simple message dialogs where needed.

This should isolate `zenity` or any GTK-specific Linux mechanism used for dialogs.

---

### 4. Open/shell backend
Operations such as:

- open URL,
- open folder,
- launch installer terminal,
- maybe invoke external handlers.

This should isolate `xdg-open`, terminal command choices, and similar system calls.

---

### 5. Tray backend
Operations related to:

- tray icon runtime,
- tray menu platform glue,
- window-launch integration from tray actions.

The business logic for tray actions should stay as high-level as possible, while toolkit/platform specifics remain inside the backend layer.

---

### 6. File-manager integration backend
Operations related to:

- installation of "Send to Phone" scripts,
- path conventions for Nautilus / Nemo / Dolphin,
- future uninstall/update of those integrations.

This is very clearly platform-specific and should not bleed into general desktop logic.

---

## Suggested target structure

A practical target shape could be:

```text
desktop/src/
  core/
    ...
  platform/
    linux/
      clipboard_backend.py
      notification_backend.py
      dialog_backend.py
      shell_backend.py
      tray_backend.py
      file_manager_backend.py
  interfaces/
    clipboard.py
    notifications.py
    dialogs.py
    shell.py
    tray.py
```

Or, if you prefer fewer layers:

```text
desktop/src/
  backends/
    linux/
      clipboard.py
      notifications.py
      dialogs.py
      shell.py
      tray.py
      file_manager.py
  core/
    ...
```

The exact naming can vary.  
The important thing is the boundary, not the package names.

---

## What should be introduced

### 1. Explicit backend contracts or protocols
These do not need to be heavy formal interface hierarchies, but the capability boundary should be explicit.

This can be done using:

- lightweight abstract base classes,
- `Protocol` types,
- or simple injected objects with documented method contracts.

The critical point is that core logic should depend on the contract, not on Linux implementation modules directly.

---

### 2. Linux backend implementations
Concrete Linux implementations should be moved behind the contracts.

These implementations can keep using the same existing tools and mechanisms.

This refactor is not about replacing Linux integrations.  
It is about containing them.

---

### 3. Platform composition point
There should be one clear place where the app decides:

- use Linux clipboard backend,
- use Linux notification backend,
- use Linux dialog backend,
- etc.

This composition point should happen near startup/bootstrap, not deep inside feature logic.

---

### 4. Capability-aware core usage
Core logic should depend on capabilities like:

- `clipboard.write_text(...)`
- `notifier.notify(...)`
- `dialogs.pick_files(...)`
- `shell.open_url(...)`

instead of directly importing Linux implementation helpers.

---

## What the first iteration should include

To keep this refactor practical, the first iteration should focus on the highest-value Linux boundaries.

### Required in the first iteration
At minimum, isolate these backends:

- clipboard
- notifications
- dialogs
- shell/open behavior

These four provide the best immediate leverage and are used widely enough to justify the boundary.

### Strongly recommended
Also begin isolating:

- tray backend
- file-manager integration backend

Even if tray is not fully abstracted in one pass, the separation should at least start.

### Not required yet
The first iteration does **not** need:

- a full cross-platform implementation,
- a Windows backend,
- a macOS backend,
- a plugin system,
- a dynamic backend registry,
- runtime backend hot-swapping.

This refactor is about explicit boundaries, not platform expansion.

---

## Concrete execution plan

## Phase 1 — isolate clipboard backend
Move Linux clipboard implementation details behind a dedicated backend contract.

### Goal
Core logic should stop knowing whether clipboard operations use:
- Wayland tools,
- X11 tools,
- or anything else.

### Deliverable
For example:
- `ClipboardBackend`
- `LinuxClipboardBackend`

The poller and transfer logic then call the backend, not Linux helper functions directly.

---

## Phase 2 — isolate notification backend
Move Linux notification implementation details behind a dedicated backend contract.

### Goal
Core logic should decide that a notification should happen, not how Linux emits it.

### Deliverable
For example:
- `NotificationBackend`
- `LinuxNotificationBackend`

Notification helpers may remain, but they should no longer be the direct platform mechanism.

---

## Phase 3 — isolate dialogs backend
Move file-picking and confirmation UX behind a dedicated backend contract.

### Goal
Core logic should not directly know about `zenity` or equivalent Linux tool choices.

### Deliverable
For example:
- `DialogBackend`
- `LinuxDialogBackend`

---

## Phase 4 — isolate shell/open backend
Move URL opening, folder opening, and terminal-launch behavior behind a dedicated backend contract.

### Goal
Core logic should stop calling `xdg-open` and terminal executables directly.

### Deliverable
For example:
- `ShellBackend`
- `LinuxShellBackend`

This backend may handle:
- open URL
- open folder
- launch installer terminal

---

## Phase 5 — begin tray boundary extraction
Move tray runtime/toolkit glue toward a dedicated backend boundary.

### Goal
Separate tray-specific Linux runtime details from higher-level tray action behavior.

This may be partial in the first iteration if full tray separation would be too large.

### Deliverable
For example:
- `TrayBackend`
- `LinuxTrayBackend`

Or a first intermediate step where tray-specific process/toolkit code is isolated even if action wiring still stays nearby.

---

## Phase 6 — isolate file-manager integration backend
Move install/update/uninstall knowledge for Nautilus/Nemo/Dolphin integration into a dedicated Linux backend area.

### Goal
Keep these environment-specific filesystem conventions out of more general install logic.

### Deliverable
For example:
- `FileManagerIntegrationBackend`
- `LinuxFileManagerIntegrationBackend`

---

## Phase 7 — create a desktop platform composition module
Introduce one startup-time composition point that wires the app to Linux backend implementations.

### Goal
Make Linux selection explicit and centralized.

### Deliverable
For example:
- `platform/linux/compose.py`
- or `bootstrap/platforms.py`

This module would return the set of backends used by the application runtime.

---

## Recommended commit order

### Commit 1
`refactor(desktop): introduce clipboard backend boundary`

Contents:
- clipboard capability contract
- Linux clipboard implementation behind the contract
- core code updated to depend on the contract

### Commit 2
`refactor(desktop): introduce notification backend boundary`

Contents:
- notification capability contract
- Linux notification implementation separated

### Commit 3
`refactor(desktop): introduce dialogs backend boundary`

Contents:
- file picker / confirmation behavior moved behind contract

### Commit 4
`refactor(desktop): introduce shell/open backend boundary`

Contents:
- URL open, folder open, terminal launch moved behind contract

### Commit 5
`refactor(desktop): start tray backend separation`

Contents:
- isolate tray-specific platform glue as much as practical

### Commit 6
`refactor(desktop): isolate Linux file-manager integration backend`

Contents:
- Nautilus/Nemo/Dolphin integration logic separated

### Commit 7
`refactor(desktop): add Linux platform composition module`

Contents:
- centralized Linux backend wiring
- core code no longer imports Linux implementations directly

---

## What should not be addressed here

This refactor **should not** address:

- Windows implementation,
- macOS implementation,
- platform-specific UI redesign,
- protocol changes,
- tray UX redesign,
- poller redesign,
- file-transfer redesign,
- new cross-platform packaging workflows,
- replacing pystray/GTK tooling.

Those belong to later work.

This refactor is about boundaries, not replacement.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. Core logic no longer imports Linux implementation details directly
Core modules depend on capability contracts or backend objects instead of Linux helper modules.

### 2. Clipboard behavior is isolated behind a backend boundary
Clipboard implementation details no longer live directly inside core feature logic.

### 3. Notification behavior is isolated behind a backend boundary
Notification transport is no longer a hard-coded Linux detail in core logic.

### 4. Dialog and shell behavior are isolated behind backend boundaries
Core logic no longer directly invokes Linux dialogs or shell-opening tools.

### 5. Linux backend composition is explicit
There is a clear startup-time place where Linux implementations are chosen and wired.

### 6. User-visible behavior remains the same
Pairing, send/receive, notifications, dialogs, tray behavior, and integration behavior remain unchanged.

---

## Test checklist

After each major phase, verify:

### Clipboard behavior
- clipboard text send still works,
- clipboard image send still works,
- incoming clipboard text still writes to system clipboard,
- incoming clipboard image still writes to system clipboard,
- fallback behavior between Linux clipboard tools still works as before.

### Notification behavior
- generic notifications still appear,
- file-received notification still works,
- connection-lost notification still works,
- connection-restored notification still works.

### Dialog behavior
- file selection still works,
- confirmation dialogs still work,
- dialog failure/fallback behavior still works.

### Shell/open behavior
- URL auto-open still works,
- folder open still works,
- installer terminal launch still works.

### Tray behavior
- tray still starts,
- tray menu still works,
- actions triggered from tray still behave the same,
- tray shutdown still works.

### File-manager integration
- integration install path behavior remains correct,
- right-click "Send to Phone" still works where already supported,
- integration update/uninstall logic remains correct if applicable.

---

## Risks

### 1. Fake abstraction with no real boundary
If Linux-specific code is merely moved into different files but still imported directly by core logic, this refactor will not create real value.

The boundary must be dependency direction, not just file movement.

---

### 2. Over-engineering interface design
It is easy to overbuild backend contracts too early.

The contracts should stay small and driven by real application needs, not imagined future platforms.

---

### 3. Leaking Linux assumptions through capability names
If the capability surface itself encodes Linux details, the abstraction is weak.

For example, a core method should not be named around `notify-send` semantics.  
It should express application intent.

---

### 4. Tray separation becoming oversized
Tray/toolkit behavior is often the messiest platform area.  
Trying to perfect that boundary in one pass could make the refactor too large.

Partial improvement is acceptable as long as the direction is correct.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have a fully portable desktop platform framework."

The correct result is:

- desktop core depends on capabilities,
- Linux specifics are isolated,
- platform composition is explicit,
- future platform support becomes cheaper,
- Linux-only assumptions stop spreading through the codebase.

---

## Practical definition of done

Done looks like this:

- core logic no longer directly calls Linux implementation helpers,
- clipboard, notifications, dialogs, and shell/open behavior are behind explicit backend boundaries,
- Linux implementations are chosen in one composition point,
- tray and file-manager integration begin to follow the same pattern,
- behavior remains unchanged.

That is the purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the desktop app will be:

- easier to reason about,
- easier to test,
- less Linux-coupled at the core level,
- better prepared for Windows-related work in refactor 10,
- less likely to keep leaking environment-specific behavior into core logic,
- structurally healthier for future feature development.

That is why this refactor is sixth.

---

## Note about the next step

After this refactor, the next one should be:

**introduce a unified command/message model for `.fn.*` and fasttrack**

That is the logical next move because once both server internals and desktop boundaries are cleaner, the next high-value unification point is message semantics themselves: special transfers and fasttrack commands should stop evolving as two parallel conceptual systems.
