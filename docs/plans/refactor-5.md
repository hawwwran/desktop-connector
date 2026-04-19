# refactor-5.md

> **Status: Done** — landed on `main` in commit `2ea1e3a` (PR #12). New `desktop/src/bootstrap/` (`args.py`, `dependency_check.py`, `logging_setup.py`, `startup_context.py`) and `desktop/src/runners/` (`registration_runner.py`, `pairing_runner.py`, `send_runner.py`, `receiver_runner.py`). `main.py` is now a ~35-line orchestrator: check deps → parse args → `setup_logging` → `build_startup_context` → `register_device` → `rebuild_authenticated_api` → pair if needed → `resolve_startup_mode` → dispatch to send or receiver. CLI flags and behavior unchanged; verified both directions (desktop → android, android → desktop).

## Refactor 5 / 10
# Thin the desktop bootstrap (`main.py`) down to a clean entrypoint

## Why this is the fifth refactor

After the first four refactors, the next highest-value step is to clean up the desktop application's startup and orchestration boundary.

At the moment, `desktop/src/main.py` is responsible for too many distinct concerns at once, including:

- dependency checks,
- dependency-install UI fallback,
- logging setup,
- config initialization,
- registration,
- pairing flow selection,
- send mode,
- receiver mode,
- headless mode,
- tray startup,
- signal handling,
- and some lifecycle wiring.

That is not yet catastrophic, but it is exactly the type of file that tends to become the long-term gravity center of desktop applications.

The purpose of this refactor is to make `main.py` a true bootstrap file:

- small,
- readable,
- predictable,
- and focused only on startup composition.

---

## Relation to the previous refactors

This refactor comes after the server-focused work for a reason.

The first four refactors improve:

- server domain boundaries,
- request boundaries,
- persistence boundaries,
- internal lifecycle clarity.

Once the server side becomes structurally safer, the next most obvious maintainability hotspot is the desktop startup path.

That makes this the right time to shift attention to the desktop application boundary.

---

## Position within the full sequence of 10 refactors

The full sequence remains:

1. Split the transfer domain into smaller server services without changing the protocol
2. Introduce an explicit server layer for auth, request context, and input validation
3. Introduce a repository layer above SQLite access
4. Formalize internal transfer states and state transitions
5. **Thin the desktop bootstrap (`main.py`) down to a clean entrypoint**
6. Separate desktop core from Linux-specific backends
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 5**.

---

## Main goal

Move the desktop application from the current shape:

- one entrypoint file coordinating many unrelated concerns,
- startup decisions mixed with runtime orchestration,
- mode selection mixed with feature logic,
- setup logic mixed with application logic,

to this shape:

- `main.py` only parses arguments and delegates,
- dependency handling is isolated,
- startup composition is explicit,
- each execution mode has its own runner,
- signal handling is centralized but minimal,
- runtime behavior is no longer assembled ad hoc inside the bootstrap file.

---

## What must not change

This refactor **must not change protocol behavior** and should not change user-visible application behavior except where tiny startup-structure side effects are unavoidable.

That means:

- same CLI flags,
- same pairing behavior,
- same send behavior,
- same headless behavior,
- same tray startup behavior,
- same dependency-check behavior,
- same registration semantics,
- same logging semantics,
- same notifications and polling startup semantics.

This is a structural desktop refactor, not a desktop feature redesign.

---

## Why this refactor matters

A large bootstrap file causes long-term friction in several ways.

### 1. Startup decisions become harder to reason about
When one file chooses:

- config initialization,
- registration,
- pairing,
- send vs receive,
- headless vs tray,
- dependency fallback,
- signal handling,

it becomes harder to see where startup logic ends and runtime logic begins.

---

### 2. New modes get added to the wrong place
As the app grows, every new mode or startup condition naturally gets added to `main.py`.

That causes an unhealthy pattern:
- bootstrap becomes orchestration,
- orchestration becomes application logic,
- and the entrypoint slowly becomes untestable and fragile.

---

### 3. Testing the startup path becomes awkward
If the whole startup flow is concentrated in one file, it is harder to test:

- registration path,
- pairing path,
- send path,
- receiver path,
- dependency-failure path.

Breaking them into explicit runners lowers the cost of testing and future changes.

---

### 4. Future platform work becomes more expensive
If the desktop application later gains:

- a Windows implementation,
- alternate packaging flows,
- different startup environments,
- or richer CLI tooling,

a thick bootstrap becomes a major source of duplication and platform leakage.

This refactor reduces that risk early.

---

## Core architectural idea

The entrypoint should answer only these questions:

1. What mode was requested?
2. What startup prerequisites must be checked?
3. Which runner should be invoked?

It should **not** implement the full behavior of each mode itself.

In practical terms:

- `main.py` becomes a dispatcher,
- runners own mode-specific startup flow,
- shared setup moves into dedicated modules.

---

## Target shape

The desired end state is something like:

```text
desktop/src/
  main.py
  bootstrap/
    args.py
    logging_setup.py
    dependency_check.py
    startup_context.py
  runners/
    pairing_runner.py
    send_runner.py
    receiver_runner.py
    tray_runner.py
  ...
```

The exact names are flexible.  
The important thing is the separation of concerns.

---

## What should be introduced

### 1. `StartupContext`
A lightweight object or structure holding the common startup dependencies, for example:

- config,
- crypto/key manager,
- API client,
- connection manager,
- parsed CLI args,
- resolved mode,
- maybe history or other shared services where appropriate.

This prevents runner construction from repeatedly rebuilding the same wiring manually.

---

### 2. Dedicated runner modules
Each main mode should have its own runner.

At minimum:

- `pairing_runner`
- `send_runner`
- `receiver_runner`
- `tray_runner`

Depending on how you want to structure it, `receiver_runner` may internally handle headless and tray branches, but explicit separation is preferred.

---

### 3. Isolated dependency-check module
Dependency checking and dependency-install dialog fallback should move out of `main.py`.

This area is important, but it is not core app orchestration.

It should become something like:

- `dependency_check.py`
- perhaps with `check_dependencies()` and `show_missing_dependencies_ui(...)`

The current logic is useful, but it does not belong inside the main bootstrap flow directly.

---

### 4. Isolated logging setup
Logging initialization should move to a dedicated module.

It should remain configurable exactly as now, but not occupy bootstrap control flow.

For example:

- `logging_setup.py`
- `setup_logging(...)`

This also makes startup behavior easier to read.

---

### 5. Signal-handling helper
Signal handling should be centralized in a very small helper or bootstrap utility.

The goal is not to overabstract it, but to avoid repeating shutdown-wiring decisions inside each startup branch.

---

### 6. Explicit mode resolution
Argument parsing should determine a clear mode before application startup branching begins.

For example, mode resolution might produce something like:

- `pair`
- `send`
- `headless_receiver`
- `tray_receiver`

This does not have to be a full enum class immediately, but it should become explicit.

---

## What the first iteration should include

To keep this refactor disciplined, the first iteration should stay practical.

### Required in the first iteration
At minimum, introduce:

- a dedicated dependency-check module,
- a dedicated logging-setup module,
- explicit mode resolution,
- separate runner modules for:
  - pairing
  - send
  - receiver/tray startup
- a thinner `main.py` that mostly parses args and dispatches.

### Strongly recommended
Also introduce:

- a shared startup context object,
- a small signal-handling helper,
- a clearer split between "startup composition" and "runtime loop startup".

### Not required yet
The first iteration does **not** need:

- a dependency-injection framework,
- a plugin architecture,
- cross-platform abstractions,
- CLI subcommand redesign,
- packaging changes,
- UI redesign,
- lifecycle redesign of poller or tray internals.

This refactor is about the bootstrap boundary only.

---

## Proposed mode model

A practical internal mode model could be:

- `pairing`
- `send_file`
- `headless_receive`
- `tray_receive`

Potentially also:
- `invalid`
- `dependency_error`

depending on how explicit you want startup result modeling to be.

The point is that mode selection becomes intentional rather than just a chain of nested conditions.

---

## Concrete execution plan

## Phase 1 — isolate dependency handling
Move dependency checking and fallback UI into its own module.

### Goal
So `main.py` no longer mixes startup orchestration with import checks and installation dialogs.

### Deliverable
For example:
- `bootstrap/dependency_check.py`

with:
- `check_dependencies()`
- `show_missing_dependencies_dialog(...)`

---

## Phase 2 — isolate logging setup
Move logging initialization into its own module.

### Goal
Make logging setup reusable and remove setup noise from the entrypoint.

### Deliverable
For example:
- `bootstrap/logging_setup.py`

---

## Phase 3 — make mode resolution explicit
Refactor argument handling so startup mode is determined once, centrally, before branching into application logic.

### Goal
Replace ad hoc startup branching with a single explicit decision point.

### Deliverable
For example:
- `resolve_mode(args) -> StartupMode`

---

## Phase 4 — extract send flow into `send_runner`
Move the single-file send-and-exit path out of `main.py`.

### Goal
Keep mode-specific flow out of bootstrap.

The send runner should own:
- registration precondition checks,
- pairing precondition checks,
- connection check,
- send orchestration,
- result exit code.

### Deliverable
For example:
- `runners/send_runner.py`

---

## Phase 5 — extract pairing flow into `pairing_runner`
Move pairing branching and completion behavior out of `main.py`.

### Goal
Keep pairing startup separate from entrypoint code.

The pairing runner should own:
- headless vs GUI pairing choice,
- pairing completion behavior,
- config reload if needed,
- cancellation handling.

### Deliverable
For example:
- `runners/pairing_runner.py`

---

## Phase 6 — extract receiver startup into `receiver_runner`
Move receiver startup orchestration out of `main.py`.

### Goal
Keep the runtime startup path explicit and isolated.

The receiver runner should own:
- connection creation,
- API client creation,
- history creation,
- poller wiring,
- notification callback registration,
- connection-state notification wiring,
- startup of headless loop or tray loop.

### Deliverable
For example:
- `runners/receiver_runner.py`

---

## Phase 7 — isolate tray startup if useful
If needed, split tray startup from receiver orchestration.

### Goal
Ensure tray-specific behavior is not mixed into generic receiver startup.

This may be a separate `tray_runner.py` or may remain part of `receiver_runner` if that still stays clean enough.

---

## Phase 8 — reduce `main.py` to dispatch only
Once the major flows are extracted, simplify `main.py` to:

1. dependency check,
2. parse args,
3. build startup context,
4. ensure registration if needed,
5. resolve mode,
6. dispatch to runner,
7. return exit code.

That should be the final shape.

---

## Recommended commit order

### Commit 1
`refactor(desktop): extract dependency check from main bootstrap`

Contents:
- dependency logic moved into its own module
- no behavior change

### Commit 2
`refactor(desktop): extract logging setup from main bootstrap`

Contents:
- logging setup moved into dedicated module

### Commit 3
`refactor(desktop): introduce explicit startup mode resolution`

Contents:
- clearer mode-selection logic
- bootstrap branching simplified

### Commit 4
`refactor(desktop): extract send runner`

Contents:
- send-and-exit flow moved out of `main.py`

### Commit 5
`refactor(desktop): extract pairing runner`

Contents:
- pairing flow moved out of `main.py`

### Commit 6
`refactor(desktop): extract receiver startup runner`

Contents:
- poller/tray/headless startup orchestration moved out of `main.py`

### Commit 7
`refactor(desktop): reduce main.py to bootstrap dispatcher`

Contents:
- final cleanup pass
- entrypoint becomes small and readable

---

## What should not be addressed here

This refactor **should not** address:

- Linux-specific backend abstraction,
- cross-platform support,
- tray implementation redesign,
- poller redesign,
- config schema redesign,
- notification redesign,
- clipboard redesign,
- CLI UX redesign,
- dependency installation UX redesign beyond structural isolation.

Those belong to other refactors.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. `main.py` is significantly smaller
The entrypoint should mostly parse args, build startup context, resolve mode, and dispatch.

### 2. Mode-specific flows are isolated
Send, pairing, and receiver startup each live in dedicated runners or equivalent isolated modules.

### 3. Dependency logic is no longer embedded in bootstrap flow
Dependency checking is isolated in a dedicated module.

### 4. Logging setup is no longer embedded in bootstrap flow
Logging setup is isolated in a dedicated module.

### 5. Signal handling is not duplicated across branches
Shutdown behavior is organized centrally enough to remain readable.

### 6. User-visible behavior is unchanged
CLI behavior, pairing behavior, send behavior, tray behavior, and headless behavior remain the same.

---

## Test checklist

After each major phase, verify:

### Dependency handling
- missing dependencies are still detected,
- GTK fallback dialog still works,
- tkinter fallback still works,
- terminal fallback still works.

### Registration flow
- unregistered app still registers correctly,
- already registered app still skips redundant registration,
- server URL override still works.

### Pairing flow
- GUI pairing still works,
- headless pairing still works,
- pairing cancellation still behaves the same,
- config reload after GUI pairing still behaves the same.

### Send flow
- `--send` still requires registration,
- `--send` still requires pairing,
- send success still returns success,
- send failure still returns failure.

### Receiver flow
- headless receiver still starts correctly,
- tray receiver still starts correctly,
- poller still starts,
- notifications still wire correctly,
- shutdown via signal still works.

### Logging
- verbose mode still behaves the same,
- file logging opt-in still works,
- logging path behavior is unchanged.

---

## Risks

### 1. Accidental behavior drift in startup order
Bootstrap refactors often change small sequencing details unintentionally.

That could affect:
- registration timing,
- config mutation timing,
- reload timing,
- signal wiring timing,
- startup notification timing.

This must be watched closely.

---

### 2. Over-abstracting too early
It is easy to turn a simple bootstrap refactor into a mini framework for app startup.

That is not the goal.

The point is not to create "startup architecture" for its own sake, but to make the entrypoint small and maintainable.

---

### 3. Splitting files without improving boundaries
If logic is moved into new files but boundaries remain unclear, the refactor will only create indirection.

Each extracted runner must own a real startup concern.

---

### 4. Too much shared mutable startup state
A startup context is useful, but it should stay small and intentional.

It should not become a dumping ground for every object in the app.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have a sophisticated application bootstrap framework."

The correct result is:

- `main.py` is small,
- startup flow is readable,
- each mode has a clear runner,
- setup concerns are isolated,
- future desktop changes become cheaper.

---

## Practical definition of done

Done looks like this:

- `main.py` no longer contains the full startup logic,
- mode resolution is explicit,
- send flow is isolated,
- pairing flow is isolated,
- receiver startup is isolated,
- dependency and logging setup are isolated,
- behavior remains the same.

That is the purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the desktop application will be:

- easier to read,
- easier to test,
- easier to extend with new startup modes,
- less likely to turn `main.py` into a permanent dumping ground,
- better prepared for platform separation in refactor 6,
- better prepared for future packaging and startup changes.

That is why this refactor is fifth.

---

## Note about the next step

After this refactor, the next one should be:

**separate desktop core from Linux-specific backends**

That is the logical follow-up because once startup is cleaner, the next maintainability boundary is the platform boundary itself: clipboard, notifications, dialogs, tray, file-manager integration, and other Linux-specific behavior.
