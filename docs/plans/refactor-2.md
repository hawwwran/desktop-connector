# refactor-2.md

> **Status: Done** — landed on `main`. New `server/src/Http/`
> (`RequestContext`, `ApiError` hierarchy, `ErrorResponder`, `Validators`)
> and `server/src/Auth/` (`AuthIdentity`, `AuthService`). `Router` takes
> `Database` in its constructor, adds `authGet`/`authPost`, and catches
> `ApiError` in `dispatch()`. Every controller now takes
> `(Database $db, RequestContext $ctx)`, validates via `Validators::*`,
> and throws `ApiError` subclasses instead of hand-serializing JSON
> errors. `TransferService`'s input-shape check and `validateTransferId`
> path-traversal guard were hoisted up to the pipeline so every
> `{transfer_id}` route is protected, not just the ones that reach the
> service. `DeviceController::health`'s ad-hoc heartbeat collapsed onto
> `AuthService::optional`. Net **−103 lines** across 8 modified files +
> 6 new. Wire protocol unchanged; Android and desktop clients untouched.
> Verified by `./test_loop.sh`.

## Refactor 2 / 10
# Introduce an explicit server layer for auth, request context, and input validation

## Why this is the second refactor

After refactor 1, the next highest-value step is to unify the server request pipeline.

Today the server repeats the same pattern in multiple places:

- the route in `index.php` calls `Router::authenticate(...)` manually,
- the controller receives `deviceId`,
- the controller reads JSON body on its own,
- the controller validates required fields on its own,
- the controller returns validation errors on its own.

That works, but over time it creates these problems:

- inconsistent validation style between endpoints,
- repeated boilerplate,
- an unclear boundary between HTTP concerns and business logic,
- growing risk that two endpoints will return equivalent errors in slightly different ways,
- harder extension of the API with new inputs.

The goal of this refactor is therefore to introduce a **unified server input layer** that handles:

- authentication,
- request context,
- JSON/raw body parsing,
- basic input validation,
- error standardization.

---

## Relation to refactor 1

Refactor 1 is **done**. `TransferController` is now a thin HTTP adapter that
delegates to `src/Services/` (`TransferService`, `TransferStatusService`,
`TransferNotifyService`, `TransferWakeService`, `TransferCleanupService`).
`index.php` requires those services directly.

Refactor 2 builds on that by cleaning up the **HTTP input boundary**.

That means:

- refactor 1 focused on business logic and server services,
- refactor 2 focuses on how inputs enter those services.

Two concrete consequences of refactor 1 for this plan:

- `TransferController` is already the thinnest controller. The migration cost
  is small — mostly wiring `RequestContext` in place of `$_GET`/raw-body
  reads and letting the controller call `TransferService` with already
  parsed values.
- Some validation that used to sit in the old `TransferController` now
  lives inside `TransferService` (`init`'s required-field check,
  `validateTransferId`'s path-traversal regex guard). Refactor 2 has to
  decide where those checks live *after* the pipeline lands — see "What
  problem currently repeats" below.

Without refactor 1, refactor 2 would have less impact, because it would only
beautify the entry path into overloaded controllers.

---

## Position within the full sequence of 10 refactors

The full sequence remains:

1. Split the transfer domain into smaller server services without changing the protocol
2. **Introduce an explicit server layer for auth, request context, and input validation**
3. Introduce a repository layer above SQLite access
4. Formalize internal transfer states and state transitions
5. Thin the desktop bootstrap (`main.py`) down to a clean entrypoint
6. Separate desktop core from Linux-specific backends
7. Introduce a unified command/message model for `.fn.*` and fasttrack
8. Introduce a compatibility layer between `protocol.md` and the implementation
9. Consolidate logging and diagnostic events across platforms
10. Prepare for a Windows desktop client through a platform abstraction boundary

This document covers **only item 2**.

---

## Main goal

Move the server from the current shape:

- auth in route closures,
- JSON parsing inside controllers,
- validation `if (!$body || empty(...))` blocks inside controllers,
- mixed route/context/validation/business flow,

to this shape:

- route only chooses the handler,
- request context is built uniformly,
- auth is handled consistently,
- input is read consistently,
- validation rules are explicit,
- controller receives already prepared values or DTO-like request data.

---

## What must not change

This refactor **must not change the protocol**.

That means no change to:

- endpoints,
- HTTP methods,
- headers `X-Device-ID` and `Authorization: Bearer ...`,
- the meaning of request/response fields,
- error status codes where clients may rely on them,
- timing and behavior observable by clients.

In other words:

**the internal server pipeline changes, not the public API.**

---

## What problem currently repeats

The same pattern is visible on the server repeatedly (verified against
`main` after refactor 1 and the recent ping/pong + path-traversal fixes):

### Auth
In `index.php`, every protected route does:

```php
$deviceId = Router::authenticate($db);
if ($deviceId === null) return;
SomeController::action($db, $deviceId, ...);
```

This is functional, but request context is assembled in a fragmented way
and outside the controller boundary. `Router::authenticate()` also has the
side effect of bumping `last_seen_at`, which is load-bearing for ping/pong.

`DeviceController::health()` adds a second, slightly different copy of
this logic: it reads `X-Device-ID` + `Authorization` itself and bumps
`last_seen_at` without calling `Router::authenticate()`. That drift is
exactly the kind of thing an `AuthService` should absorb.

### JSON parsing
The controllers that still read and validate JSON bodies themselves:

- `PairingController::request` and `::confirm`
- `FasttrackController::send`
- `DeviceController::register`, `::updateFcmToken`, `::ping`
- `TransferService::init` (called by the thin `TransferController::init`)

They each call `Router::getJsonBody()` (or receive the already-parsed body
from the controller) and then individually decide what is valid.

### Query parameters
Controllers reach into `$_GET` directly in several places:

- `TransferController::notify` — reads `$_GET['since']` and `$_GET['test']`
- `DeviceController::stats` — reads `$_GET['paired_with']`

These are part of the HTTP input surface and should flow through
`RequestContext` too, not be pulled from superglobals inside controllers.

### Validation
Controllers (and `TransferService`) repeatedly do:

- `empty($body['field'])` with a hand-written 400 error,
- `(int)$params['id']` + custom bounds check,
- `preg_match(self::TRANSFER_ID_PATTERN, …)` inside `TransferService` as a
  path-traversal guard (added in `ca7ade7`),
- `DeviceController::ping` hand-emits `header('Retry-After: N')` plus a
  JSON body with a `retry_after` field on 429.

This works, but validation policy is not centralized anywhere, and the
existing defensive checks (transfer-id safety, ping rate-limit shape) must
be preserved byte-for-byte when the pipeline lands — clients depend on
those exact responses.

---

## What should be introduced

### 1. `RequestContext`
An object or structure carrying the essential request data.

It should contain at least:

- HTTP method,
- path / route params (`transfer_id`, `chunk_index`, fasttrack `id`),
- query params (today's callers: `since`, `test`, `paired_with`),
- raw body (for chunk uploads — `TransferService::uploadChunk` needs the
  raw bytes, not JSON),
- parsed JSON body,
- authenticated `deviceId` if present,
- optionally common headers.

### Purpose
The controller or handler no longer needs to read from `$_GET`, `$_SERVER`,
`php://input`, or `Router::getJsonBody()` / `Router::getRawBody()`. Both
JSON endpoints and the binary chunk endpoints must be expressible through
the same `RequestContext` — the raw-body path is not a second-class
citizen.

---

### 2. `AuthContext` or `AuthService`
A dedicated layer that handles:

- reading `X-Device-ID`,
- reading the `Authorization` header,
- validating the bearer token,
- updating `last_seen_at`,
- returning the authenticated identity.

### Purpose
Authentication will no longer be scattered across route closures or hidden as a side effect of `Router::authenticate()`.

---

### 3. `RequestBody` helper / parser
A unified layer for:

- loading raw body,
- JSON decoding,
- detecting invalid JSON,
- handling empty body,
- optionally distinguishing JSON and binary request types.

### Purpose
JSON parsing is no longer implemented separately in every controller.

---

### 4. `Validator` / `InputValidator`
A lightweight validation layer for required fields and basic input shape.

This should not become an overengineered framework.  
A small internal layer is enough, for example:

- required string,
- required int,
- optional nullable string (e.g. `fcm_token` may be explicitly `null` to clear),
- non-empty field,
- integer >= min,
- enum from a fixed set,
- safe-id check (alphanumeric + hyphen, length-capped) — today's
  `TransferService::TRANSFER_ID_PATTERN` guard belongs here.

### Purpose
Validation logic becomes readable, consistent, and reusable. The
path-traversal guard currently implemented as a private helper in
`TransferService` should be expressed once as a reusable validator and
applied at the pipeline boundary for every endpoint that accepts a
`{transfer_id}` route param.

---

### 5. `ApiError` / `HttpError`
A unified mechanism for returning standardized errors.

For example internally:

- `ValidationError` → 400
- `UnauthorizedError` → 401
- `ForbiddenError` → 403 (e.g. "Devices are not paired")
- `NotFoundError` → 404
- `ConflictError` → 409 (e.g. "Transfer ID already exists")
- `RateLimitError` → 429, carrying a `Retry-After` seconds value
- `StorageLimitError` → 507 (recipient storage limit exceeded)

with a single central mapping to JSON responses.

The rate-limit error must round-trip today's on-wire shape exactly:

- `Retry-After` HTTP header set to the cooldown delta in seconds,
- JSON body containing `{"error": "...", "retry_after": N}`.

`DeviceController::ping` currently hand-emits both; the central error
responder should keep that behavior identical so the desktop client does
not notice.

### Purpose
Controllers will no longer need to keep writing:

- `Router::json(['error' => ...], 400)`
- `Router::json(['error' => ...], 403)`
- `Router::json(['error' => ...], 404)`
- `header('Retry-After: ...'); Router::json([...], 429)`

---

### 6. `AuthenticatedRoute` or route helper
A layer that removes repeated boilerplate from `index.php`.

For example, instead of:

```php
$router->post('/api/fasttrack/send', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    FasttrackController::send($db, $deviceId);
});
```

the target shape would be something like:

```php
$router->authPost('/api/fasttrack/send', function (RequestContext $req) use ($db) {
    FasttrackController::send($db, $req);
});
```

or something equivalent.

### Purpose
The auth request pipeline becomes uniform and route definitions become shorter and more readable.

---

## Target structure

A possible target structure (adds `Http/` and `Auth/` alongside the
`Services/` tree that refactor 1 already landed):

```text
server/src/
  Http/
    RequestContext.php
    RequestParser.php
    JsonBody.php
    ApiError.php
    ErrorResponder.php
    Validators.php

  Auth/
    AuthService.php
    AuthIdentity.php

  Controllers/
    DeviceController.php
    PairingController.php
    TransferController.php
    FasttrackController.php
    FcmController.php
    DashboardController.php

  Services/                  # from refactor 1 — unchanged
    TransferService.php
    TransferStatusService.php
    TransferNotifyService.php
    TransferWakeService.php
    TransferCleanupService.php

  Router.php
```

It is not necessary to use exactly these names, but the direction should be:

- HTTP concerns separated,
- auth concerns separated,
- controller no longer acting as parser + validator + endpoint handler all at once,
- validation that today lives inside `Services/` (transfer-id safety,
  init body shape) gets hoisted up to the pipeline so the services receive
  already-validated values.

---

## Core architectural idea

The controller should be the place where you decide:

- which use case runs,
- with which already prepared values.

The controller should not be the place where you:

- dig headers out of the request,
- parse JSON,
- read directly from `$_SERVER`,
- duplicate validation rules.

---

## What the first iteration should include

To keep scope under control, the first iteration should stay practical and small.

### Required in the first iteration
At minimum, introduce:

- `RequestContext`
- `AuthService`
- a simple `Validator`
- centralized error / error responder logic
- a route helper for authenticated endpoints

### What can remain simple for now
No need yet for:

- full DTO objects for every request,
- a complex validation DSL,
- a middleware framework,
- exception-heavy architecture everywhere,
- a global dependency-injection container.

The goal is to **unify the pipeline**, not to invent a mini framework.

---

## Concrete execution plan

## Phase 1 — introduce `RequestContext`
First introduce a simple request object.

It should carry:

- route params (`transfer_id`, `chunk_index`, fasttrack `id`),
- parsed JSON body,
- raw body (required by `TransferService::uploadChunk`),
- authenticated device ID,
- query params (`since`, `test`, `paired_with` are the live callers today).

### Benefit
Controllers stop reading directly from `$_GET`, `$_SERVER`, `php://input`,
`Router::getJsonBody()`, and `Router::getRawBody()`. Both JSON and binary
endpoints feed off the same object.

---

## Phase 2 — introduce `AuthService`
Move authentication out of `Router::authenticate()` into an explicit service.

### Goal
Instead of "router helper as side effect", there is a clear auth layer that:

- validates headers,
- finds the device,
- updates `last_seen_at` (load-bearing for ping/pong — see CLAUDE.md's
  "Liveness probe" section),
- returns an identity object or auth failure.

`Router::authenticate()` may remain temporarily as a thin wrapper until
all routes are migrated.

### Also fold in `DeviceController::health`'s heartbeat path
`health()` has its own slightly different copy of the "read headers,
look up device, bump `last_seen_at`" logic, because the endpoint is
public but doubles as a heartbeat when auth headers *are* sent. The new
`AuthService` should expose an "optional auth" variant so `health` can
reuse it instead of duplicating the query.

---

## Phase 3 — introduce authenticated route helper
Add a helper path in `Router` for protected routes.

For example:
- `authGet(...)`
- `authPost(...)`

or an equivalent mechanism.

### Goal
Remove the repeated boilerplate in `index.php`:

- authenticate,
- null-check,
- manual passing of `deviceId`.

---

## Phase 4 — introduce a basic validator
Do not overcomplicate it at first.

A few helpers are enough, for example:

- `requireString(body, 'desktop_id')`
- `requireString(body, 'phone_pubkey')`
- `requireIntParam(params, 'id', min: 1)` — fasttrack ack uses this shape today
- `requireNullableString(body, 'fcm_token')` — `null` is a valid clear signal
- `requireSafeTransferId(params, 'transfer_id')` — replaces
  `TransferService::validateTransferId`; the pipeline rejects unsafe ids
  before the service runs, so services can assume the id is safe.

### Goal
Remove repeated controller / service code such as:
- `if (!$body || empty(...))`
- `if (!preg_match(self::TRANSFER_ID_PATTERN, $transferId))`

---

## Phase 5 — introduce unified errors
Instead of hand-written JSON error responses in controllers, add centralized error mapping.

For example internally:

- `ValidationError('Missing phone_id') -> 400`
- `UnauthorizedError('Missing authentication') -> 401`
- `ForbiddenError('Devices are not paired') -> 403`
- `NotFoundError('Transfer not found') -> 404`
- `ConflictError('Transfer ID already exists') -> 409`
- `RateLimitError(retryAfter: 30) -> 429 with Retry-After header and retry_after field`
- `StorageLimitError('Recipient storage limit exceeded') -> 507`

### Goal
The controller should say **what happened**, not **how to serialize the
HTTP error**. The responder must preserve today's exact on-wire shapes,
most notably `DeviceController::ping`'s 429 (header + body field) and the
507 from `TransferService::init`.

---

## Phase 6 — migrate controllers
Once the basic building blocks exist, migrate controllers in this order:

1. `PairingController` (3 methods, straightforward bodies)
2. `FasttrackController` (3 methods, already well-shaped)
3. `FcmController` (1 public method, trivial — good smoke test for the
   pipeline's public-route path)
4. `TransferController` (now a thin adapter after refactor 1 — the main
   work is threading `RequestContext` through and hoisting
   `TransferService::init`'s body-shape check + `validateTransferId`
   up into pipeline validators)
5. `DeviceController` (largest and most delicate — register, stats with
   `$_GET['paired_with']`, updateFcmToken, ping with rate-limit + 429
   shape, pong, health with its bespoke heartbeat)

This order is revised from the original plan. After refactor 1,
`TransferController` is no longer the most complex controller; it is
nearly mechanical. `DeviceController` now carries the ping/pong
rate-limit logic and the quasi-auth heartbeat in `health`, so it is the
riskiest to migrate and should come last.

`DashboardController` renders HTML and does not participate in the
auth/JSON pipeline — leave it alone.

---

## Recommended commit order

### Commit 1
`refactor(server): introduce RequestContext and AuthService`

Contents:
- new request context
- new auth service
- without major controller migration yet

### Commit 2
`refactor(server): add authenticated route helpers`

Contents:
- reduction of repeated auth boilerplate in `index.php`

### Commit 3
`refactor(server): add lightweight input validator`

Contents:
- helpers for required fields
- helpers for parameter parsing

### Commit 4
`refactor(server): add centralized API error mapping`

Contents:
- unified HTTP error mechanism
- controllers handle less response serialization directly

### Commit 5
`refactor(server): migrate PairingController to RequestContext + validator`

Contents:
- pairing as the first pilot controller

### Commit 6
`refactor(server): migrate FasttrackController to request pipeline`

Contents:
- fasttrack moved to the new style

### Commit 7
`refactor(server): migrate FcmController to request pipeline`

Contents:
- public-route smoke test for the pipeline

### Commit 8
`refactor(server): migrate TransferController + hoist service-level validation`

Contents:
- `TransferController` takes `RequestContext`
- `TransferService::init`'s body-shape check moves to pipeline validators
- `TransferService::validateTransferId` is replaced by a reusable
  `requireSafeTransferId` validator applied at the route boundary
- `$_GET['since']` / `$_GET['test']` / `$_GET['paired_with']` come from
  `RequestContext::query`
- `507 recipient storage limit exceeded` still surfaces unchanged

### Commit 9
`refactor(server): migrate DeviceController to request pipeline`

Contents:
- registration, stats, FCM token, ping, pong, health
- `ping`'s 429 (`Retry-After` header + `retry_after` body field) goes
  through the centralized `RateLimitError` responder
- `health`'s ad-hoc heartbeat consolidates onto `AuthService`'s
  optional-auth path

---

## What should not be addressed here

This refactor **should not** address:

- endpoint changes,
- JSON field-name changes,
- auth-header renaming,
- a new auth model,
- a new token model,
- sessions,
- rewriting the router into a full framework,
- a full dependency-injection architecture,
- DB model changes,
- protocol changes.

All of that would only expand the scope unnecessarily.

---

## Acceptance criteria

The refactor is complete if the following are true:

### 1. `index.php` is significantly cleaner
Authenticated endpoints no longer contain the same auth boilerplate repeatedly.

### 2. Controllers no longer parse request data manually
Controllers no longer directly read from:
- `Router::getJsonBody()`,
- `$_GET`,
- `$_SERVER`,
- raw route params without normalization.

### 3. Validation is no longer written ad hoc
Repeated blocks such as:
- `if (!$body || empty(...))`
- `if (!preg_match(TRANSFER_ID_PATTERN, ...))`

are gone, at least in the main migrated controllers and services.

### 4. Errors are serialized consistently
Validation and auth errors go through a central mechanism. The
rate-limit response for `/api/devices/ping` still returns the exact
`Retry-After` header + `retry_after` body field shape the desktop
client reads today.

### 5. The transfer-id path-traversal guard still holds
Unsafe `transfer_id` values are rejected at the pipeline boundary for
every endpoint that accepts `{transfer_id}`, not just inside
`TransferService`.

### 6. Clients do not notice the change
Android and desktop work unchanged.

### 7. The protocol remains unchanged
`protocol.md` does not change.

---

## Test checklist

After each major phase, verify:

### Auth
- missing `X-Device-ID` still returns the same 401 behavior,
- missing bearer token still returns the same 401 behavior,
- invalid token still returns the same 401 behavior,
- `last_seen_at` is still updated during authenticated requests.

### Validation
- missing required field still returns the same 400 behavior,
- invalid ID parameter still returns the expected error,
- nullable `fcm_token` still works (explicit `null` clears the token),
- `transfer_id` path-traversal attempts (`..`, `/`, over-long strings)
  still rejected with `{error: "Invalid transfer_id format"}` 400.

### Pairing
- pairing request works the same,
- poll works the same,
- confirm works the same.

### Fasttrack
- send works the same,
- pending works the same,
- ack works the same,
- 403 for unpaired devices still works the same.

### Device endpoints
- register unchanged (base64 pubkey length check preserved),
- stats unchanged (including `?paired_with=<id>` branch),
- FCM token update unchanged,
- ping rate-limit unchanged — 429 still carries both the `Retry-After`
  header and `{error, retry_after}` body,
- ping `via` values unchanged (`fresh`, `no_fcm`, `fcm_failed`,
  `fcm_timeout`, `fcm`),
- pong unchanged,
- health unchanged — including the heartbeat bump when authenticated
  headers are present.

### Transfer endpoints
- init / upload / download / ack / sent-status unchanged,
- notify unchanged (`?since=`, `?test=` branches and inline sent_status payload),
- `507` recipient storage limit still surfaces from init,
- `409` transfer-id conflict still surfaces from init,
- 1-in-20 cleanup sampling on `pending` still fires.

### FCM config
- `/api/fcm/config` still returns `{available: false}` when
  `google-services.json` is missing or malformed.

---

## Risks

### 1. Subtle change in error text or status code
Clients may not rely on error text, but it is still better to keep
behavior as close as possible to the current implementation. Particular
care for response shapes the desktop client *does* parse:
- ping 429 body's `retry_after` field + the `Retry-After` header,
- ping response's `via` enum values,
- `available: false` shape from `/api/fcm/config`.

### 2. Overbuilding a framework
It is easy to drift into building a custom micro-framework.  
That is not the goal.

### 3. Scope creep
This refactor should focus on request pipeline concerns, not business
logic and not persistence.

### 4. Introducing DTOs too early
If every request is modeled as a full object immediately, the first
iteration may become unnecessarily large.

### 5. Losing the `last_seen_at` bump
`Router::authenticate` and the ad-hoc heartbeat in
`DeviceController::health` both update `last_seen_at`. Ping/pong
liveness depends on this side effect. The `AuthService` must preserve
it, including on the `health` path.

### 6. Weakening the transfer-id path-traversal guard
The guard was added recently (`ca7ade7`) and lives inside
`TransferService`. Moving it up to the pipeline must not create any
endpoint that accepts `{transfer_id}` without running the check first.

---

## Recommended simplicity boundary

The correct result of this refactor is not:

- "now we have an enterprise framework".

The correct result is:

- request pipeline is uniform,
- auth is centralized,
- validation is readable,
- controllers are smaller,
- future input changes will be cheaper than they are today.

---

## Practical definition of done

Done looks like this:

- the route defines the endpoint,
- request context carries the input,
- auth layer resolves identity,
- validator prepares values,
- controller calls the use case,
- error responder returns HTTP errors,
- business logic no longer begins by parsing JSON.

That is the core purpose of this refactor.

---

## What the benefit will be after completion

Once complete, the server will be:

- more readable,
- less repetitive,
- less prone to inconsistent validation errors,
- better prepared for additional endpoints and future protocol extensions,
- better prepared for introducing a repository layer in refactor 3.

That is why this refactor is second.

---

## Note about the next step

After this refactor, the next one should be:

**introduce a repository layer above SQLite access**

That only makes sense once the request input layer is cleaner.  
There is no point abstracting persistence while the HTTP boundary itself is still messy.
