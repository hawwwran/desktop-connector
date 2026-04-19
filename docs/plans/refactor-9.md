# refactor-9.md

> **Status: Done** — landed on `main` in commits `f646054`..`deadc50`
> (7 commits: privacy redaction, vocabulary doc, transfer/delivery,
> fasttrack/pairing, ping/poll, clipboard/platform, CLAUDE.md). New
> `docs/diagnostics.events.md` catalog drives dot-notation event names
> across server / desktop / Android with shared severity and
> correlation-ID rules. Server went from 4 log lines (fasttrack only)
> to ~170 lines per round-trip covering the full transfer, delivery,
> pairing, ping, auth, and long-poll lifecycle. Commit 1 is
> safety-critical: removes QR JSON, GPS coordinates, fasttrack
> payload, clipboard URLs, and clipboard preview from all logs. All
> three runtimes now share `[timestamp] [level] [tag] message` format;
> correlation IDs truncated to 12 chars for cross-runtime grep.
> Android built green; `test_loop.sh` passes with 173 log lines per
> round-trip (84 debug, 71 info, 18 warning, 0 error).

## Refactor 9 / 10
# Consolidate logging and diagnostic events across platforms

## Why this is the ninth refactor

After the first eight refactors, the next highest-value step is to improve operational visibility across the whole system.

At this point, the project has:

- a cleaner server architecture,
- a clearer request boundary,
- a more explicit transfer lifecycle,
- a cleaner desktop startup boundary,
- a cleaner desktop platform boundary,
- a more unified message model,
- and a more explicit protocol compatibility layer.

That means the next major source of engineering friction is no longer only structure.  
It is **diagnostic inconsistency**.

Right now, the project already has logging on:

- server,
- desktop,
- Android,

but those logs are still primarily shaped by local implementation needs rather than by a shared diagnostic model.

The result is that cross-platform debugging still costs more than it should.

The purpose of this refactor is to consolidate logging and diagnostic events so that the system becomes easier to:

- debug,
- support,
- reason about in production-like conditions,
- and analyze across component boundaries.

---

## Relation to the previous refactors

This refactor is intentionally late in the sequence.

It only becomes worth doing after:

- the internal structure is healthier,
- message semantics are clearer,
- protocol compatibility is more explicit.

Without that groundwork, logging consolidation would mostly preserve messy boundaries and make inconsistent behavior easier to observe without making it easier to understand.

Now, by contrast, the system is structurally ready for a shared diagnostic model.

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
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. **Consolidate logging and diagnostic events across platforms**
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 9**.

---

## Main goal

Move the system from the current shape:

- each runtime logs in its own style,
- log messages are useful locally but inconsistent globally,
- cross-platform flows are difficult to reconstruct,
- the same event may be described differently on server, desktop, and Android,
- correlation between components is weak,

to this shape:

- important runtime events are defined consistently,
- diagnostic terminology is shared,
- cross-platform flows can be reconstructed more easily,
- correlation identifiers become more deliberate,
- logging becomes a system-level diagnostic tool rather than only a local debugging aid.

---

## What must not change

This refactor **must not change protocol behavior**.

It should also avoid changing user-visible behavior except for operational tooling surfaces such as:

- improved log clarity,
- better log export usefulness,
- better diagnostic consistency.

This refactor is not a feature redesign, not a protocol redesign, and not a telemetry commercialization step.

It is strictly about making the system easier to diagnose and support.

---

## Why this matters

### 1. Cross-platform bugs are harder than local bugs
This project spans:

- PHP server,
- Python desktop,
- Kotlin Android.

Many real bugs are not local to one runtime.  
They are flow bugs across multiple components.

Examples:
- transfer completed on server but sender status did not update,
- fasttrack wake happened but receiver action did not execute,
- FCM wake happened but recipient progress stalled,
- notify loop woke correctly but UI state did not converge,
- clipboard command arrived but platform write failed.

Without a shared diagnostic model, these are slower to debug than they should be.

---

### 2. The same event needs the same meaning everywhere
A system-level event such as:

- transfer initialized,
- upload completed,
- recipient progress advanced,
- ACK received,
- fasttrack message delivered,
- find-phone start command accepted,
- ping FCM sent,
- pong received,
- long poll unavailable,

should not be described in three unrelated styles across the stack.

The wording does not need to be identical, but the event model should be consistent.

---

### 3. Better logging reduces fear of refactoring
When diagnostics are weak, engineers are afraid to change behavior because regressions are hard to isolate.

When diagnostics are stronger and more consistent, refactors become cheaper because failures become easier to localize.

This is especially valuable after the structural refactors already completed.

---

### 4. Log export becomes more useful
The system already has log export ideas and logging toggles.

But exported logs are much more valuable when:
- important events have predictable names,
- identifiers line up,
- categories are consistent,
- noisy low-value messages are separated from high-value lifecycle events.

---

## Core architectural idea

Logging should stop being only:

- "whatever message seems useful in this file"

and start becoming:

- a shared system of **diagnostic events with consistent meaning**.

That does not mean replacing all logs with a rigid event schema.  
It means that the most important flows should be described using shared diagnostic concepts.

The model should distinguish clearly between:

### 1. Event meaning
What happened?

Examples:
- transfer created
- chunk uploaded
- transfer completed
- recipient progress updated
- transfer delivered
- fasttrack message stored
- fasttrack message acked
- ping sent
- pong received
- long poll fallback triggered
- clipboard write failed
- pairing confirmed

### 2. Event context
Where and under what conditions did it happen?

Examples:
- runtime: server / desktop / android
- transfer ID
- sender ID
- recipient ID
- message ID
- device ID
- transport type
- message type
- reason / outcome / error category

### 3. Event severity
How important is it?

Examples:
- debug
- info
- warning
- error

### 4. Event audience
Is this useful for:
- developer debugging,
- support troubleshooting,
- user-exported diagnostics,
- protocol compatibility auditing,
- performance investigation?

Not every event must serve every audience.

---

## Recommended diagnostic model

A practical unified model should include:

### 1. Event category
Examples:
- `auth`
- `pairing`
- `transfer`
- `delivery`
- `fasttrack`
- `ping`
- `poll`
- `clipboard`
- `notification`
- `platform`
- `startup`
- `protocol`

This helps keep the logs searchable and comparable across runtimes.

---

### 2. Event name
Examples:
- `transfer.init.accepted`
- `transfer.chunk.uploaded`
- `transfer.upload.completed`
- `transfer.delivery.progress`
- `transfer.delivery.acked`
- `fasttrack.message.stored`
- `fasttrack.message.acked`
- `ping.request.sent`
- `ping.pong.received`
- `poll.notify.timeout`
- `poll.notify.unavailable`
- `clipboard.write_text.succeeded`
- `clipboard.write_image.failed`

The exact naming can be adjusted, but the structure should be intentional.

---

### 3. Shared context fields
Examples:
- `transfer_id`
- `message_id`
- `device_id`
- `sender_id`
- `recipient_id`
- `message_type`
- `transport`
- `chunks_downloaded`
- `chunk_count`
- `via`
- `reason`
- `error_kind`

Not every field applies to every event, but the common ones should be named consistently.

---

### 4. Outcome semantics
It should be easier to distinguish:
- accepted
- progressed
- completed
- failed
- skipped
- timed_out
- retried
- ignored

This is important because many current logs are understandable locally but inconsistent in how they express outcomes.

---

## What should be introduced

### 1. Shared diagnostic event vocabulary
Introduce a lightweight catalog of important event categories and names.

This can live as:

- a markdown document,
- a small constants module in each runtime,
- or both.

The important point is that major system events stop being named arbitrarily.

---

### 2. Correlation identifier strategy
Introduce a deliberate rule for correlation across logs.

At minimum, the system should consistently log identifiers such as:

- `transfer_id`
- `message_id`
- `device_id`
- `sender_id`
- `recipient_id`

Where available, these should appear in major lifecycle logs across runtimes.

This does not require distributed tracing infrastructure.  
It requires disciplined use of existing IDs.

---

### 3. Event-level logging guidelines
Introduce explicit guidance for what should be logged at:

- debug
- info
- warning
- error

This helps prevent:
- too much noise at info level,
- too little context at warning/error level,
- and important lifecycle events disappearing into verbose logs.

---

### 4. Diagnostic categories in code
Where practical, update logging helpers or conventions so categories become more visible and consistent.

This may be done using:
- logger names,
- structured prefixes,
- explicit event-name fields,
- or helper functions.

The mechanism can vary by runtime as long as the meaning stays aligned.

---

### 5. Cross-platform lifecycle coverage
Ensure that the most important cross-platform flows are represented consistently across:

- server logs,
- desktop logs,
- Android logs.

These flows include at least:
- registration/auth
- pairing
- transfer init/upload/download/ack
- delivery progress
- fasttrack send/pending/ack
- ping/pong
- long-poll fallback
- clipboard command handling
- find-phone command lifecycle

---

### 6. Log export usefulness improvements
Where logs are user-exportable, make sure the exported content is more useful by:

- keeping high-value event names readable,
- keeping identifiers present,
- reducing unnecessary ambiguity,
- avoiding noisy spam in normal operation where possible.

This does not necessarily require changing the export mechanism itself.  
The improvement may come mostly from better event structure.

---

## Suggested target structure

A practical target shape could be:

```text
docs/
  diagnostics.events.md

desktop/src/
  diagnostics/
    events.py
    logging_policy.py

android/app/src/main/kotlin/.../diagnostics/
  DiagnosticEvents.kt
  LoggingPolicy.kt

server/src/
  Diagnostics/
    Events.php
    LoggingPolicy.php
```

This does not need to be heavy or fully symmetrical across languages.  
The key is that the main event vocabulary becomes explicit and shared.

---

## What the first iteration should include

To keep this refactor practical, the first iteration should focus on the highest-value flows.

### Required in the first iteration
At minimum, unify diagnostic events for:

- transfer lifecycle
- delivery lifecycle
- fasttrack lifecycle
- ping/pong
- long-poll fallback
- clipboard command handling
- pairing lifecycle

### Strongly recommended
Also introduce:

- a small diagnostics event catalog document,
- shared naming for event outcomes,
- correlation-ID guidance,
- severity-level guidance.

### Not required yet
The first iteration does **not** need:

- full structured JSON logging everywhere,
- external log aggregation,
- distributed tracing,
- remote telemetry,
- metrics pipeline,
- centralized dashboarding,
- strict one-format logging across all runtimes.

Those may come later if useful, but they are not required for this refactor to deliver value.

---

## Concrete execution plan

## Phase 1 — define diagnostic event vocabulary
Create a small shared event vocabulary for the most important system events.

### Goal
Stop naming major events ad hoc in each runtime.

### Deliverable
For example:
- `docs/diagnostics.events.md`

This document should define:
- categories
- event naming pattern
- important event names
- expected key context fields

---

## Phase 2 — define correlation rules
Write down which identifiers must appear in which types of logs.

### Goal
Make cross-runtime log correlation deliberate and reliable.

### Deliverable
For example:
- for transfer events: always include `transfer_id`, and where relevant `sender_id` and `recipient_id`
- for fasttrack events: always include `message_id` where available
- for ping events: include `device_id`, `recipient_id`, `via`, `rtt_ms` where relevant

---

## Phase 3 — normalize transfer and delivery events
Update server, desktop, and Android logging so transfer-related events use a shared vocabulary.

### Goal
Make transfer debugging flow-oriented rather than runtime-specific.

### Deliverable
Examples:
- `transfer.init.accepted`
- `transfer.chunk.uploaded`
- `transfer.upload.completed`
- `transfer.delivery.progress`
- `transfer.delivery.acked`

---

## Phase 4 — normalize fasttrack and command events
Update fasttrack and command-style message logging to use shared event semantics.

### Goal
Ensure fasttrack and message handling are diagnosable across components.

### Deliverable
Examples:
- `fasttrack.message.stored`
- `fasttrack.message.pending`
- `fasttrack.message.acked`
- `message.dispatch.started`
- `message.dispatch.failed`

---

## Phase 5 — normalize ping/poll events
Update liveness and notify-loop logging to use shared terms.

### Goal
Make online/offline and wake-debugging far easier.

### Deliverable
Examples:
- `ping.request.sent`
- `ping.request.rate_limited`
- `ping.pong.received`
- `poll.notify.timeout`
- `poll.notify.unavailable`
- `poll.notify.fallback_started`

---

## Phase 6 — normalize clipboard and platform-operation events
Update logging around clipboard writes, notifications, dialogs, open actions, and similar platform operations.

### Goal
Make platform failures easier to distinguish from protocol or transport failures.

### Deliverable
Examples:
- `clipboard.write_text.succeeded`
- `clipboard.write_image.failed`
- `notification.send.failed`
- `shell.open_url.succeeded`

---

## Phase 7 — refine severity and noise policy
Review what currently logs at info/warning/error and align it with the new model.

### Goal
Keep logs useful without making them excessively noisy.

This is especially important for user-exported logs.

---

## Recommended commit order

### Commit 1
`docs(diagnostics): define shared diagnostic event vocabulary`

Contents:
- diagnostics event catalog
- categories, naming pattern, common context fields

### Commit 2
`docs(diagnostics): define correlation and severity guidelines`

Contents:
- which IDs to log
- severity expectations
- high-value vs noisy events

### Commit 3
`refactor(logging): normalize transfer and delivery events across runtimes`

Contents:
- transfer/delivery event naming aligned

### Commit 4
`refactor(logging): normalize fasttrack and command events across runtimes`

Contents:
- fasttrack/message dispatch event naming aligned

### Commit 5
`refactor(logging): normalize ping and polling diagnostics`

Contents:
- ping/pong/notify-related logging aligned

### Commit 6
`refactor(logging): normalize clipboard and platform-operation diagnostics`

Contents:
- clipboard, notifications, shell/dialog events aligned

### Commit 7
`refactor(logging): adjust severity levels and reduce high-noise messages`

Contents:
- cleanup pass
- better balance between signal and noise

---

## What should not be addressed here

This refactor **should not** address:

- protocol changes,
- feature changes,
- remote telemetry rollout,
- analytics,
- metrics dashboards,
- full tracing infrastructure,
- mandatory structured JSON logging,
- logging persistence redesign,
- privacy-policy redesign.

Those are different concerns.

This refactor is about **diagnostic consistency and usefulness**, not telemetry expansion.

---

## Acceptance criteria

The refactor is complete if all of the following are true:

### 1. Major system events have a shared diagnostic vocabulary
Important lifecycle events are no longer named arbitrarily across runtimes.

### 2. Cross-platform flows are easier to correlate
Logs for the same flow include enough shared identifiers and event meaning to reconstruct the path.

### 3. Transfer, delivery, fasttrack, ping, and poll diagnostics are aligned
These high-value flows now use consistent event categories and naming.

### 4. Platform-operation failures are easier to distinguish
Clipboard, notification, dialog, and shell/open failures are clearly logged as platform events rather than being confused with transport or protocol failures.

### 5. Log exports become more useful
Exported logs contain more predictable and searchable event information.

### 6. User-visible behavior remains unchanged
Only diagnostics improve; features and protocol behavior remain the same.

---

## Test checklist

After each major phase, verify:

### Transfer diagnostics
- transfer creation logs use the new shared vocabulary,
- upload progress logs include useful identifiers,
- delivery progress logs include useful identifiers,
- ACK completion logs include useful identifiers.

### Fasttrack diagnostics
- send/store logs are aligned,
- pending/read logs are aligned,
- ack logs are aligned,
- message-dispatch failures are clearly visible.

### Ping and polling diagnostics
- ping send logs are visible and identifiable,
- rate-limit logs are distinguishable,
- pong logs are identifiable,
- notify timeout/unavailable/fallback logs are distinguishable.

### Clipboard and platform diagnostics
- clipboard success/failure is clearly visible,
- notification send failures are clear,
- URL/folder open failures are clear,
- dialog failures are clear.

### Severity policy
- normal operation is not excessively noisy at info level,
- warnings remain meaningful,
- errors carry enough context to be actionable.

### Log export usefulness
- exported logs remain readable,
- major lifecycle events are searchable by consistent names,
- identifiers can be used to correlate flows manually.

---

## Risks

### 1. Over-standardizing low-value messages
Not every local debug log needs to become part of a grand shared event vocabulary.

The focus should stay on important lifecycle and failure events.

---

### 2. Too much noise at info level
If event names improve but log volume grows too much, the result may still be hard to use.

Severity discipline is part of the refactor, not an afterthought.

---

### 3. Mistaking local debug convenience for system-level diagnostics
Some logs are valuable only when debugging one runtime in isolation.

Those should not be forced into the shared diagnostic vocabulary unless they help cross-platform reasoning.

---

### 4. Missing privacy discipline
Better logging must not drift into logging sensitive decrypted content or user data casually.

Improved event structure must not weaken privacy expectations.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "we now have a complete observability platform."

The correct result is:

- important events are named consistently,
- cross-runtime flows are easier to reconstruct,
- identifiers are logged more deliberately,
- severity is more disciplined,
- log exports become significantly more useful.

---

## Practical definition of done

Done looks like this:

- major system events have a shared vocabulary,
- correlation guidance is explicit,
- transfer/fasttrack/ping/poll/platform events are more consistent,
- log severity is cleaner,
- exported logs are more diagnosable,
- behavior remains unchanged.

That is the purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the project will be:

- easier to debug,
- easier to support,
- easier to refactor safely,
- better prepared for diagnosing cross-platform issues,
- better prepared for broader real-world use,
- operationally more trustworthy.

That is why this refactor is ninth.

---

## Note about the next step

After this refactor, the next one should be:

**prepare for a Windows desktop client through a platform abstraction boundary**

That is the logical final step because once structure, semantics, compatibility, and diagnostics are all in much better shape, the project is finally in a reasonable position to prepare for a second desktop platform without doing it on top of hidden architectural debt.
