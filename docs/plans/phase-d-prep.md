# Phase D preparation — `_devel_` server-side debug tool

**Status: DESIGN — not implemented.** User will start the prep
tomorrow; this doc is the checklist of what they need to have
ready AND the scope of the PHP tool Claude will build.

## Why this exists

Phase D (Android streaming client) will be developed with:
- A physical Android device connected to the user's PC over USB.
  `adb logcat` streams the phone's side of every interaction back
  to the terminal the user runs in; user pastes relevant output
  into the chat so Claude can read it.
- The **deployed** server at
  `https://hawwwran.com/SERVICES/desktop-connector/` as the
  counterparty. We can't use the hermetic local server the
  protocol tests use, because the phone's FCM push flow requires
  the real Firebase project wired to the real deployed server.

What's missing is server-side observability. During Phase D Claude
needs to see, in real time:
- What requests the Android device is making (and what statuses
  the server returns).
- The server-side state of every transfer in flight — `mode`,
  `chunks_received`, `chunks_downloaded`, `stream_ready_at`,
  `aborted`, `abort_reason`, bytes on disk.
- Whether FCM wakes actually fired and what payload they carried.
- Device liveness (`last_seen_at`, FCM token registered).

The `_devel_` folder gives Claude that observability without
requiring SSH access to the server. It's a small, purposely-
scoped PHP web tool the user drops onto the deployed server
as `public/_devel_/`. The repo-side source lives in `temp/_devel_/`
(gitignored — the tool never enters git history). After Phase D
lands the deployed folder gets removed.

---

## Scope of the `_devel_` tool

Claude will implement all of this. User's only job is to deploy
the folder, set the secret token, and confirm access.

### Authentication

One shared-secret token, checked on every request. Token lives
in `secret.php` inside the deployed `_devel_/` folder. It's
**never committed** — the whole `temp/` tree (where Claude
develops) is already in `.gitignore`, and the deployed copy
exists only on the server.

Without the token — or with a wrong one — every endpoint returns
**404 Not Found** (not 401). This way the folder doesn't
advertise itself to crawlers / casual visitors. The token is
passed via:
- `?t=<token>` query param, or
- `X-Devel-Token: <token>` header.

### Location — source lives in `./temp/`, deploy copies to server's `public/`

**Repo source: `temp/_devel_/`.** The whole `temp/` tree is
already in the project's `.gitignore`, so nothing under
`_devel_/` — source, secrets, README — ever gets committed. The
tool is personal-dev-only; it stays out of the shipped codebase.

**Deploy target: `public/_devel_/` on the real server**, served
at `/SERVICES/desktop-connector/public/_devel_/`. User copies
the contents of `temp/_devel_/` into
`public_html/SERVICES/desktop-connector/public/_devel_/` using
whatever SFTP / file-manager flow they already use for server
deploys.

The deployed server's existing `public/.htaccess` serves real
files first (`!-f` escape), so the deployed `_devel_/*.php` files
bypass the front controller cleanly — no `.htaccess` changes to
existing shipped routes.

Layout (both in `temp/_devel_/` locally and in deployed
`public/_devel_/`):

```
_devel_/
  index.php         — HTML dashboard; links to every subtool
  secret.php        — the one-line token file (never committed;
                      temp/ is .gitignored as a whole)
  lib.php           — shared auth + DB open + JSON helpers
  logs.php          — tail server.log
  transfers.php     — list transfers with full row state
  devices.php       — list devices + pairings + FCM token presence
  fasttrack.php     — list pending fasttrack messages
  storage.php       — per-transfer bytes on disk under storage/
  config.php        — current effective config
  abort.php         — force-abort a transfer (server-side kill)
  fcm-probe.php     — fire a test FCM wake to a specific device
  .htaccess         — deny direct access to secret.php, lib.php
  README.md         — deploy + usage cheat sheet (local only)
```

Each endpoint is a single PHP file returning JSON (or HTML for
`index.php`). Small, reviewable, no framework.

**Path resolution inside PHP:** each endpoint includes server
classes via `require_once __DIR__ . '/../../src/…'`. That's the
correct relative path on the deployed server, where `_devel_`
sits at `public/_devel_/` and `src/` at `../src/` from public —
i.e. `../../src/` from an endpoint inside `_devel_/`. The code
in `temp/_devel_/` ships with those paths hardcoded for the
deploy target.

**Local smoke-testing** the folder before handing it to the user:
Claude runs `php -S 127.0.0.1:8000 -t /tmp/sandbox` where
`/tmp/sandbox` is a fresh copy of `server/` with
`temp/_devel_/` merged into `public/_devel_/`. A small shell
helper (`temp/_devel_/run-local.sh`) automates this. Nothing
permanent in the repo.

### Endpoint behaviour

| Endpoint | Method | Returns | Notes |
|---|---|---|---|
| `/_devel_/` | GET | HTML dashboard | token in query; each link carries the token forward. |
| `/_devel_/logs.php?tail=500` | GET | text/plain | last N lines of `server/data/logs/server.log`. `tail=0` ⇒ default 200. Supports `&since=<line>` for incremental fetches. |
| `/_devel_/logs.php?follow=1` | GET | text/event-stream | SSE: pushes each new log line. Handy for live-watch during a phone test. |
| `/_devel_/transfers.php` | GET | JSON array | all rows from `transfers` table, redacted (no `encrypted_meta`), with derived fields: state from `TransferLifecycle::deriveState`, bytes-on-disk from `storage/<id>/`. Query param `&tid=<id>` filters to one row. |
| `/_devel_/devices.php` | GET | JSON array | `devices` rows + pairing counts, with `last_seen_at` as ISO string AND seconds-ago. `public_key` redacted to fingerprint. |
| `/_devel_/fasttrack.php` | GET | JSON array | `fasttrack_messages` rows with age + `recipient_id`. Encrypted blob sizes only, never decrypted. |
| `/_devel_/storage.php` | GET | JSON | per-transfer-id bytes on disk; global total; per-recipient quota sum (the exact number the server checks on streaming 507). |
| `/_devel_/config.php` | GET | JSON | current `data/config.json` merged with defaults; lists `streamingEnabled`, `storageQuotaMB`, etc. |
| `/_devel_/abort.php?tid=...&reason=test` | POST | JSON | force-abort a transfer bypassing the role check. Reason is written to `abort_reason` as `devel:<reason>`. Useful for test cleanup when both sides are stuck. |
| `/_devel_/fcm-probe.php?device_id=...` | POST | JSON | fire a minimal `type=test` FCM wake to the given device's registered token. Returns the FCM API response verbatim so we can see success / invalid-token / etc. Bypasses the paired-device check. |

### Privacy invariants (STRICT)

- **Never log the token.** `secret.php` is read and discarded
  per-request; no part of it ever enters the server.log.
- **Never serve encrypted blobs, public keys, or FCM tokens in
  plaintext.** Public keys get SHA-256-fingerprint-first-12-chars
  rendering. FCM tokens render as `present / absent` booleans.
  Encrypted payload bytes get `size + sha256_first_8` not the
  bytes themselves.
- **Never decrypt anything.** `_devel_` is server-side only; it
  has no keys. All endpoints that touch user data render
  metadata only.
- **All `_devel_` actions log to server.log** with the tag
  `devel.<endpoint> token=<sha256_first_8>` so any abuse leaves
  a trail even though the raw token is opaque.

### Non-goals

- Not a persistent tool. Ship it for Phase D; delete after D lands.
- Not production-ready. No rate-limiting, no audit log retention,
  no HTTPS enforcement beyond whatever Apache already does.
- No destructive actions beyond per-transfer abort. Can't wipe
  the DB, can't unpair devices, can't mutate config.
- Not a general admin tool. If Phase E finds that `_devel_` is
  too useful to retire, it graduates to a proper admin surface
  with real auth — out of scope here.

---

## What the user needs to prepare

Starting tomorrow, before Claude begins Phase D, these must be in
place. Ordered by "Claude can't proceed without this" first.

### Blocking

1. **Deploy `temp/_devel_/` to the live server.**
   Claude will build the folder under `temp/_devel_/` in the
   repo — `temp/` is gitignored so nothing under it gets
   committed. User copies the contents of `temp/_devel_/`
   into `public_html/SERVICES/desktop-connector/public/_devel_/`
   using whatever SFTP / file-manager flow they already use.
   - Claude will ship the folder as the first artifact of
     this prep phase. User then deploys it manually.
   - The tool is not code-reviewed in the usual sense
     because it doesn't enter git history. Claude keeps the
     files small and boring so a quick eyeball before
     upload catches anything odd.

2. **Generate a `_devel_` secret token and paste it into
   `secret.php` on the deployed server.**
   - Token format: 40+ random hex chars.
     One-liner: `openssl rand -hex 24`.
   - Paste the token into a chat message so Claude has it
     available when hitting endpoints during Phase D.
   - Token never needs to leave the user's clipboard + the
     deployed `secret.php`. Local `temp/_devel_/secret.php`
     can have a dummy / placeholder — the deployed copy is
     what matters. Whole `temp/` is .gitignored so even a
     real token there can't leak.
   - User posts the token ONCE in chat. Claude reads it, uses
     it, doesn't log it back.

3. **Confirm the `_devel_` dashboard returns HTTP 200** when
   hit from the user's browser at
   `https://hawwwran.com/SERVICES/desktop-connector/public/_devel_/?t=<token>`.
   If it 404s, the deploy hasn't taken — either the folder
   didn't upload, or the shared host's rewrite rules are
   intercepting. User tells Claude what URL they see; Claude
   troubleshoots.

### Useful but not blocking

4. **One paired test pair on the deployed server**, so Claude
   can drive end-to-end flows without touching the user's
   primary history.
   - Register a throwaway desktop device on the deployed
     server (the existing `--pair` flow from the desktop
     client works).
   - Pair that throwaway desktop with the user's phone. This
     uses QR + verification code like any normal pairing.
   - User tells Claude the throwaway device's `device_id`
     (first 12 chars is enough) so Claude can filter `_devel_`
     queries to that pair.
   - **Keep the throwaway desktop's `config.json` backed up.**
     If Claude needs to drive the desktop sender during
     testing, user can hand Claude the symmetric key
     (base64) for direct driving from the hermetic desktop
     send runner.

5. **Confirm FCM is configured on the deployed server.**
   Required for streaming wake flows. Check: a classic
   transfer from desktop → phone should wake the phone even
   when its screen is off. If it doesn't, `firebase-service-
   account.json` is missing or misnamed on the server. Fix
   before Phase D starts — debugging Android streaming
   without working FCM wakes is sharp-knife territory.

6. **ADB + phone dev setup ready.**
   - USB debugging enabled on the phone.
   - `adb devices` shows the phone as authorised.
   - User can `adb logcat` and paste relevant chunks.
   - For first flight: user may want
     `adb logcat -v time *:W DesktopConnector:D` so we see
     warnings + our app's debug lines without Android's
     spammy defaults. Claude will refine filters as needed.

7. **Optional: bump server log verbosity.** Default is
   `info`; streaming debug sometimes benefits from `debug`.
   Knob is `server/data/config.json` → `"logLevel": "debug"`.
   User flips it on for Phase D, flips it off after. Claude
   will ask if/when it matters.

### What Claude will NOT need from the user

- No SSH access to the server. Everything Claude needs goes
  through `_devel_` over HTTPS.
- No root on the phone. All debug is ADB + app-side logging
  via `AppLog` (already gated on the "Allow logging"
  preference in Android settings — user toggles it on for
  Phase D).
- No access to the user's paired devices' keys. Claude works
  with the throwaway desktop's keys (item 4) for any flows
  that require driving the sender side.

---

## What Claude will deliver in this prep phase (before D starts)

Because the tool lives only in `temp/` and doesn't enter git
history, there's no "prep phase commit" to land — Claude just
writes the files and tells the user it's ready. The deliverable
is the folder itself plus a single commit that updates THIS plan
doc's post-prep notes once the user confirms the deploy works.

Files written (all under `temp/_devel_/`, all uncommitted):

1. The PHP endpoints described in the **Endpoints** section
   above. No framework, minimal includes (reach into
   `server/src/…` via relative paths). Single PHP files,
   small enough to eyeball before each deploy.
2. `.htaccess` — denies direct access to `secret.php` /
   `lib.php` regardless of auth; hardens the folder against
   accidental exposure.
3. `README.md` — 1-page deploy + usage cheat sheet mirroring
   this plan's "prepare" checklist, so the user has it next
   to the files rather than having to cross-reference the
   docs tree.
4. `run-local.sh` — helper that merges `server/` + this
   folder into a tempdir and spins up `php -S` so Claude can
   smoke-test endpoints against a local copy before telling
   the user to redeploy.
5. `secret.php` — a placeholder with a clearly-fake token;
   user overwrites the deployed copy with a real token.

Testing:

- Claude drives local smoke tests via `run-local.sh` + ad-hoc
  curl / Python one-liners — these don't get committed either,
  they're part of the `temp/` scratch space.
- No `tests/protocol/test_devel_tools.py` in the committed
  tree. Auth + privacy invariants get hand-verified locally
  and spot-checked against the deployed server once it's
  running.

After Claude writes the folder, the user:
1. Copies `temp/_devel_/` into the server's
   `public/_devel_/`.
2. Sets the real token in the deployed `secret.php`.
3. Confirms the dashboard loads at the deployed URL.
4. Optionally works through items 4–7 above.

Then Phase D (Android streaming client) begins. Once the user
confirms the deploy, Claude commits the **Post-prep notes**
update below with the deployed URL + any host-specific quirks —
that's the one commit this prep phase produces.

---

## Post-prep notes

Filled in after deploy on 2026-04-22.

### Deployed URL

```
https://hawwwran.com/SERVICES/desktop-connector/public/_devel_/index.php?t=<TOKEN>
```

**Note the explicit `index.php`** — the bare
`/public/_devel_/?t=...` URL gets intercepted by the root
`.htaccess`'s generic `!-f → public/index.php` rewrite before
DirectoryIndex resolution, so it 404s through the Router. All
the dashboard's internal links resolve to `.php` files directly
(`logs.php`, `transfers.php`, …) so once you're in, every
subsequent click works as expected. The README.md inside the
`_devel_/` folder documents this gotcha.

### Token generation

Claude generated the token with `openssl rand -hex 24` and wrote
it to `temp/_devel_/secret.php`. Because the whole `temp/` tree
is gitignored, the token doesn't enter git history. User SFTP'd
the folder straight to the server; no manual `secret.php`
editing was needed.

### FCM confirmed working

`/_devel_/config.php` reports:
```
fcm_service_account_present: true
google_services_present: true
```
Streaming is enabled (`streamingEnabled: true`). Observed
live-classic transfers continue to wake the phone over FCM, so
Phase D can proceed without FCM-side work.

### Host-specific quirks

- Direct access to `secret.php` / `lib.php` returns **404**,
  not the expected 403 that our `.htaccess` `Require all denied`
  would produce. Likely a host-level `ErrorDocument` remapping
  403→404 (or mod_security). Functionally safe — the files
  are inaccessible; the 404 actually leaks less than a 403 would.
- Missing / wrong token returns 404 as intended. Cannot be
  distinguished from "folder not deployed" — that's the point.

### Throwaway pair test ID

_To be filled in once the user registers a throwaway desktop
and pairs it with the phone (useful-but-not-blocking item 4
from the checklist above)._

### Phase D kick-off readiness

Claude has read access to the deployed server via `_devel_`
over HTTPS. The tool covers every observability need the plan
called for: transfer-row state, per-chunk storage bytes,
per-recipient quota sum, live log tail, FCM probe, forced
abort for test cleanup. Blocking items 1–3 from the "What the
user needs to prepare" section are done. Phase D can begin
once items 4 (throwaway pair) and 6 (ADB workflow) are sorted.
