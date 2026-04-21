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
scoped PHP web tool the user drops into `server/public/_devel_/`.
After Phase D lands it gets removed.

---

## Scope of the `_devel_` tool

Claude will implement all of this. User's only job is to deploy
the folder, set the secret token, and confirm access.

### Authentication

One shared-secret token, checked on every request. Token lives in
`server/public/_devel_/secret.php` (or a small file read at
runtime) and is **never committed** to git — the file is
generated locally, deployed by the user, and `.gitignore`d.

Without the token — or with a wrong one — every endpoint returns
**404 Not Found** (not 401). This way the folder doesn't
advertise itself to crawlers / casual visitors. The token is
passed via:
- `?t=<token>` query param, or
- `X-Devel-Token: <token>` header.

### Endpoints

All endpoints live under `/SERVICES/desktop-connector/public/_devel_/`.
The server's existing `public/.htaccess` serves real files first
(`!-f` escape), so the `_devel_/*.php` files bypass the front
controller cleanly — no `.htaccess` changes to existing routes.

Layout:

```
server/public/_devel_/
  index.php         — HTML dashboard; links to every subtool
  secret.php        — the one-line token file, .gitignore'd
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
```

Each endpoint is a single PHP file returning JSON (or HTML for
`index.php`). Small, reviewable, no framework.

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

1. **Deploy `server/public/_devel_/` to the live server.**
   Claude will commit the folder to git (code-reviewed like
   everything else). User uploads `public/_devel_/*` into
   `public_html/SERVICES/desktop-connector/public/_devel_/`
   using whatever shared-hosting transfer the user uses today.
   - Claude will build the folder as part of this prep phase
     after the plan is accepted. User then deploys it.

2. **Generate a `_devel_` secret token and paste it into
   `secret.php` on the deployed server.**
   - Token format: 40+ random hex chars.
     One-liner: `openssl rand -hex 24`.
   - Paste the token into a chat message so Claude has it
     available when hitting endpoints during Phase D.
   - **Never commit the token.** Claude adds `secret.php` to
     the repo's `.gitignore` as part of the deployed folder.
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

The commit(s) from this prep phase will contain:

1. `server/public/_devel_/` — the PHP tool described above.
   Every file reviewable; no framework; minimal dependencies
   (just `src/Database.php` and `src/Config.php` via
   `require_once` with correct relative paths; no Composer).
2. `server/public/_devel_/.htaccess` — denies direct access
   to `secret.php` / `lib.php`.
3. `.gitignore` entry for `server/public/_devel_/secret.php`.
4. `server/public/_devel_/README.md` — 1-page deploy +
   usage cheat sheet for the user, mirroring this plan's
   "prepare" checklist.
5. A dedicated pair of tests under
   `tests/protocol/test_devel_tools.py`:
    - 404 without token / with bad token.
    - `/logs.php` auth'd response shape.
    - `/transfers.php` returns the expected JSON shape with
      `mode`, `state`, `bytes_on_disk`.
    - `/abort.php` marks the row aborted.
    - Privacy checks: no `encrypted_meta`, no raw public key,
      no FCM token string in any response body.
6. A short addition to this doc under "Post-prep notes" once
   the user has deployed + tested, capturing the deployed URL
   and any host-specific quirks discovered.

After the prep phase commits land, Claude will wait for the
user to:
1. Deploy the folder.
2. Set the token and paste it in chat.
3. Confirm the dashboard loads.
4. Confirm items 4–7 above as far as the user cares to.

Then Phase D (Android streaming client) begins.

---

## Post-prep notes

> _Filled in after the user has deployed the tool. Left empty
> so we both remember to write down any surprises._

- Deployed URL: _TBD_
- Token posted by user on: _TBD_
- FCM confirmed working: _TBD_
- Throwaway pair test ID: _TBD_
- Any host-specific quirks: _TBD_
