# Desktop Connector

E2E encrypted file and clipboard sharing between Android phone and Linux desktop, via a PHP relay server.

## Architecture

Three components in one monorepo:

- **`server/`** — PHP blind relay. Stores only encrypted blobs and device IDs. SQLite DB. No framework.
- **`desktop/`** — Python app with pystray tray icon. GTK4/libadwaita windows (run as subprocesses to avoid GTK3/4 conflict with pystray).
- **`android/`** — Kotlin + Jetpack Compose. Foreground service for receiving. WorkManager for uploads.

## Security

- X25519 key exchange (Bouncy Castle on Android, PyNaCl on desktop)
- AES-256-GCM symmetric encryption with HKDF-SHA256 key derivation
- Server never sees plaintext — all content encrypted client-side
- Pairing via QR code with verification code confirmation

## Special transfer naming convention

Files named `.fn.<function>.<subtype>` trigger special behavior on the receiver:
- `.fn.clipboard.text` — push text to system clipboard
- `.fn.clipboard.image` — push image to system clipboard
- `.fn.unpair` — remove pairing on the receiving side
- Extensible for future functions

## Fasttrack: lightweight encrypted message relay

For commands that are too lightweight for the full transfer pipeline (encrypt → chunk → upload → download → decrypt → ack), the fasttrack system provides a simple encrypted message queue between paired devices.

- **Server-side**: `fasttrack_messages` table stores opaque encrypted blobs. Three endpoints: send, pending, ack.
- **Function-agnostic**: The server never knows what function is being executed. The `fn` field lives inside the E2E encrypted payload.
- **FCM-triggered**: On send, the server fires an FCM wake with `{type: "fasttrack"}` — no content leaked.
- **Auto-cleanup**: Messages expire after 10 minutes. Max 100 pending per recipient.
- **Encrypted payload format**: `{fn: "find-phone", action: "start", ...}` — same AES-256-GCM as transfers.
- **Bidirectional**: Both desktop and phone can send/receive. Each polls `/api/fasttrack/pending`.
- **Extensible**: Future features use new `fn` values with zero server changes.

## Building

### Server
```bash
php -S 0.0.0.0:4441 -t server/public/
```

### Desktop
```bash
# Dependencies: python3-tk, pystray, qrcode, PyNaCl, cryptography, requests
cd desktop && python3 -m src.main              # tray mode
cd desktop && python3 -m src.main --headless   # headless receiver
cd desktop && python3 -m src.main --send="/path/to/file"  # send and exit
cd desktop && python3 -m src.main --pair       # pairing flow
```

GTK4 windows (settings, history, send files, find phone) run as separate processes:
```bash
python3 -m src.windows send-files --config-dir=~/.config/desktop-connector
python3 -m src.windows settings --config-dir=~/.config/desktop-connector
python3 -m src.windows history --config-dir=~/.config/desktop-connector
python3 -m src.windows find-phone --config-dir=~/.config/desktop-connector
```

### Android
```bash
export ANDROID_HOME=/opt/android-sdk
cd android && ./gradlew assembleDebug
# APK at: app/build/outputs/apk/debug/app-debug.apk
```

### Integration test
```bash
./test_loop.sh    # full closed-loop: register, pair, encrypt, upload, download, decrypt, verify
```

## Installation (desktop)

One-liner installer (idempotent — safe to run multiple times for install, update, or repair):
```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
```

What it does:
- Installs system packages via apt (python3-tk, libadwaita, etc.)
- Installs Python packages via pip (pystray, qrcode, PyNaCl, etc.)
- Downloads app to `~/.local/share/desktop-connector/`
- Creates `desktop-connector` launcher in `~/.local/bin/`
- Adds app menu entry and autostart
- Installs file manager right-click "Send to Phone" (auto-detects Nautilus, Nemo, Dolphin)

Uninstall:
```bash
~/.local/share/desktop-connector/uninstall.sh
```

Startup dependency checker: if anything is missing, the app shows a dialog listing missing dependencies with an "Install" button that runs the installer in a terminal.

## Server deployment

For shared hosting (Apache), upload `server/` contents to a directory. Structure:
```
desktop-connector/
  .htaccess            — rewrites all URLs to public/index.php, blocks protected dirs
  public/
    index.php          — front controller
    .htaccess          — rewrites non-file URLs to index.php
  src/                 — protected by .htaccess (403)
  data/                — protected (SQLite DB)
  storage/             — protected (encrypted blobs)
  migrations/          — protected
  firebase-service-account.json  — optional, FCM push (protected by .htaccess)
  google-services.json           — optional, FCM push (protected by .htaccess)
```

Requires: PHP 8.0+, SQLite3 extension, mod_rewrite enabled. For FCM push wake: curl extension, openssl extension.

The Router auto-detects the base path from `SCRIPT_NAME`, so it works in any subdirectory without configuration.

## Key design decisions

- **PHP single-threaded**: The built-in PHP server handles one request at a time. Dashboard auto-refresh can block API calls. For production, use nginx + php-fpm or Apache + mod_php.
- **Server request pipeline**: Every HTTP handler takes `(Database $db, RequestContext $ctx)`. The `Router` builds the `RequestContext` (route params, query, lazy body) and — for routes registered via `authGet`/`authPost` — resolves identity through `AuthService::requireAuth` before dispatch. Controllers validate inputs with `Validators::*` and throw `ApiError` subclasses; the `Router`'s top-level `try/catch` hands them to `ErrorResponder` for serialization. Net effect: no controller touches `$_SERVER`, `$_GET`, or `php://input` directly, and no controller hand-writes `Router::json(['error'=>…], 4xx)`. The path-traversal guard on `{transfer_id}` is a pipeline validator, so every endpoint that accepts that param is protected, not just the ones that reach `TransferService`.
- **Server persistence layer**: All SQL against SQLite lives in `server/src/Repositories/` (`DeviceRepository`, `PairingRepository`, `TransferRepository`, `ChunkRepository`, `FasttrackRepository`, `PingRateRepository`). Services and controllers express intent (`markTransferDelivered`, `tryClaimCooldown`, `sumPendingBytesForRecipient`); repositories hold the queries and own row-shape assumptions. The atomic UPSERT behind ping rate-limit and the `MAX(chunks_downloaded, :progress)` idiom in download progress both sit behind named repository methods but keep their exact SQL shape — the WAL serialization and the `chunks_downloaded == chunk_count ⇔ downloaded == 1` invariant both depend on it. Controllers and services no longer issue raw `$db->query*` / `$db->execute` calls.
- **Diagnostic logging**: Cross-platform logs use a shared dot-notation event vocabulary (`transfer.init.accepted`, `ping.request.rate_limited`, `clipboard.write_text.succeeded`…). The catalog lives at `docs/diagnostics.events.md` — categories, naming pattern, severity rules, correlation-ID rules, and the never-log list. Each runtime embeds event names as prose anchors in the log message (not structured fields) so existing log-reader tooling still works; all three log formats share `[timestamp] [level] [tag] message`. Correlation IDs (`transfer_id`, `message_id`, `device_id`…) always truncate to the first 12 chars so the same flow can be grepped across server / desktop / Android logs. **Privacy rule**: never log confidential data (keys, auth/FCM tokens, decrypted clipboard/file/GPS content, public keys, encrypted payloads). Server-side `apierror.caught` is a centralized Router catch that logs every 4xx/5xx with status + URI + reason — no per-throw instrumentation needed.
- **Chunked transfers**: 2MB chunks for resume capability and memory efficiency.
- **Delivery ACK**: Server tracks `downloaded` flag. Sender polls `GET /api/transfers/sent-status` to update "Sent" → "Delivered" in history.
- **Battery**: Android app pauses polling when screen is off. With FCM configured, the server sends a silent push on new transfers, waking the app to poll immediately. Without FCM, polls resume on screen wake. Zero server traffic while idle — phone sends nothing, server pings on demand (see "Liveness probe" below).
- **Liveness probe (ping/pong)**: Instead of periodic heartbeats, the server probes the phone on demand. Desktop calls `POST /api/devices/ping` → server sends a HIGH-priority FCM `{type:"ping"}` → phone's FcmService replies with `POST /api/devices/pong` (auth middleware bumps `last_seen_at`) → server's ping handler polls `last_seen_at` for up to 5s and returns `{online, rtt_ms, via}`. Ping fires on desktop connect, when the tray menu is opened (30s cache), and every 5 min as a loose background refresh. FCM HIGH bypasses Doze so RTT stays ~1s. Battery: ~288 wakes/day worst case (5-min cadence), ~0 when the desktop isn't running. Shortcut: if `last_seen_at >= baseline` already (phone talked to server this second via transfers/long poll), server skips FCM entirely and returns `via: fresh`. Pong uses a 3s-timeout OkHttp client and runs synchronously on FCM's onMessageReceived thread (inside the ~10s wakelock), so completion is guaranteed without spawning a detached worker.
- **Ping rate limit + debounce**: Both are one primitive — an atomic UPSERT on `ping_rate(sender_id, recipient_id, cooldown_until)` with `ON CONFLICT ... DO UPDATE ... WHERE cooldown_until <= now`. `changes()==0` means a live slot exists → reject 429 with `Retry-After`. Cooldown is 30s, which exceeds the 5s max blocking wait, so concurrent pings for the same pair are impossible AND a single (sender, recipient) pair is capped at 1 ping / 30s. Defends against compromised-paired-device abuse (both PHP-worker starvation and phone-battery drain). Legitimate clients don't notice because desktop caches 30s client-side anyway.
- **Ping security boundary**: Both `/api/devices/ping` and `/api/devices/pong` sit behind `Router::authenticate`, so an outsider cannot call them without a valid `device_id` + `auth_token`. Registration is public but produces credentials bound to a fresh device that isn't paired with anyone; the ping handler explicitly 403s if no row in `pairings` links caller and target. Pairing requires the QR + verification-code exchange, so a remote attacker cannot reach the FCM send path at all. Threat model for rate limiting is therefore "compromised or malicious already-paired device", not "public internet".
- **FCM push wake**: Optional. Server reads `firebase-service-account.json` and `google-services.json` at server root. If present, sends data-only FCM with HIGH priority on transfer complete (HIGH priority bypasses Android Doze mode). Android app initializes Firebase dynamically from `GET /api/fcm/config` — no baked-in `google-services.json`. Each server deployment can use its own Firebase project. Falls back to long-polling if unavailable.
- **Desktop GTK4 subprocess pattern**: pystray loads GTK3 internally. All Adw/GTK4 windows must run as `python3 -m src.windows <name>` subprocesses to avoid version conflict.
- **Notification icons**: Android status bar icons are monochrome. Use distinct shapes for states (filled circle=connected, ring=disconnected). No intermediate "connecting" state in notifications — only updates on actual state change.
- **Unpair syncs both sides**: When either side unpairs, it sends `.fn.unpair` to the other. Android navigates to pairing screen automatically. Desktop shows notification and updates tray menu.
- **Notification clearing**: Android clears transfer notifications on app open/resume, keeping only the persistent service notification.
- **MediaStore indexing**: Files saved to `DesktopConnector/` on Android are scanned by MediaStore so they appear in gallery and photo pickers.
- **Tray icon style**: Donut-shaped (colored ring with white center) for better visibility.
- **Config reload**: Desktop config reloads `paired_devices` from disk on every access to pick up changes from subprocess windows (settings, pairing).
- **Recent files**: Android HomeScreen shows lazy-loaded recent files from MediaStore, excluding files in `DesktopConnector/` folder. Refreshes on app resume.
- **Dependency checker**: Desktop app checks all imports on startup. If missing, shows GTK4 dialog with "Install" button that opens installer in terminal.
- **APK install**: Tapping an APK file in Android history checks `REQUEST_INSTALL_PACKAGES` permission, redirects to settings if needed, then opens the system package installer.
- **Remote device status**: Desktop tray icon shows green/yellow donut (connected to server, phone offline) vs green/white (both online). Checked every 30s via stats endpoint.
- **Pull-to-refresh scope**: Android pull-to-refresh wraps the entire HomeScreen (buttons, recent files, history), not just the history list. Also resets connection backoff.
- **Long polling**: Server endpoint `GET /api/transfers/notify` blocks up to 25s, checks every 500ms for new transfers, deliveries, or recipient download progress. Inlines `sent_status` payload when progress or delivery is detected (clients avoid a second request). Clients test with `?test=1` (instant response) before committing to long poll. Falls back to regular polling if unavailable. Status visible in both apps' settings.
- **Three-phase transfer state**: Every outgoing transfer moves through `uploading → delivering → delivered`, each visualized independently with its own progress bar and its own set of fields. Uploading owns `chunksUploaded`/`totalChunks` (Android) / `chunks_downloaded`/`chunks_total` (desktop, historical name). Delivering owns `deliveryChunks`/`deliveryTotal` (Android) / `recipient_chunks_downloaded`/`recipient_chunks_total` (desktop). Each phase clears its own fields when it completes — no shared state between phases.
- **Streaming relay + per-chunk ACK**: Transfers negotiate `mode ∈ {classic, streaming}` at init. Streaming mode collapses upload and delivery into a single overlapping pipeline: server fires `stream_ready` FCM wake on first chunk stored; recipient starts pulling immediately; each chunk is per-chunk ACKed (`POST /api/transfers/{id}/chunks/{i}/ack`) so the server deletes the blob as soon as the recipient has it. Peak on-disk use collapses from `N × CHUNK_SIZE` (classic store-then-forward) to `~1 × CHUNK_SIZE` (in-flight window between sender's write head and recipient's read head). **Negotiation** is server-authoritative: client requests `mode=streaming`, server downgrades to `classic` if recipient isn't `last_seen_at >= now-15s`, if the operator disabled streaming via `streamingEnabled=false`, or if the client didn't advertise the capability. Clients check `stream_v1 in GET /api/health.capabilities` before requesting streaming. **Abort protocol**: `DELETE /api/transfers/{id}` accepts both sender and recipient with typed `reason ∈ {sender_abort, sender_failed, recipient_abort}`; cross-role reasons (recipient passing `sender_abort` etc.) return 400. Next chunk upload/download on an aborted transfer returns 410 Gone with the `abort_reason` in the body — each side learns the other aborted on its next API call. **425 Too Early** distinguishes "chunk not uploaded yet" from "404 unknown transfer" with a `Retry-After` header + ms-precision `retry_after_ms` body field so recipients can poll politely without treating upstream lag as a fatal error. **Retry budgets** are per-side: recipient tolerates 5 min of continuous 425s before aborting as `recipient_abort`; sender tolerates 30 min of continuous 507s (mid-stream quota backpressure) before aborting as `sender_failed` with `failure_reason=quota_timeout`. Classic transfers are byte-for-byte unchanged — `.fn.*` command transfers always force classic (too small to benefit, extra round-trips hurt).
- **Streaming status vocabulary**: Desktop `history.py` exposes `TransferStatus.{UPLOADING, SENDING, WAITING, WAITING_STREAM, DELIVERING, COMPLETE, DELIVERED, DOWNLOADING, FAILED, ABORTED}`. Streaming sends walk `uploading → sending → (waiting_stream ↔ sending)* → complete → delivered`; `waiting_stream` is the mid-stream 507-backoff state (yellow in UI, not to be confused with classic `waiting` which is init-time 507). `ABORTED` is terminal orange, rendered with an optional `abort_reason` suffix ("sender cancelled", "recipient cancelled", "sender gave up"). The `TransferLifecycle` server-side domain model allows `UPLOADING → DELIVERING` and `INITIALIZED → DELIVERING` precisely because streaming overlaps the two phases — classic transfers still take the conservative `UPLOADING → UPLOADED → DELIVERING` path.
- **Android streaming state machines**: Android client mirrors desktop C.1-C.7 in `UploadStreamLoop.kt` (sender) and `DownloadStreamLoop.kt` (recipient) — pure functions over typed `ChunkUploadResult` / `ChunkDownloadResult` outcomes, JVM-unit-testable with a virtual clock and fake API. `UploadWorker.runStreamingUpload` wraps the sender loop with wake + WiFi lock and Room writes; `PollService.receiveStreamingTransfer` wraps the recipient loop with file IO + `.incoming_<tid>.part` + atomic rename. **Field-ownership contract**: upload loop owns `status` / `chunksUploaded` / `totalChunks` / `abortReason` / `failureReason` / `waitingStartedAt`; delivery tracker owns `deliveryChunks` / `deliveryTotal` / `delivered` (via `markDelivered` called on observed `delivery_state="delivered"`). `maybeFlipToSending` in `UploadWorker` reads tracker-owned `deliveryChunks` each chunk Ok and flips status `UPLOADING → SENDING` when it crosses zero (real overlap observed); on `Delivered` outcome the row stays `SENDING` until the tracker flips `delivered=1` — no intermediate `COMPLETE` for streaming. Room schema (v7→v8 migration) adds `mode`, `negotiatedMode`, `abortReason`, `failureReason`, `waitingStartedAt`. `TransferStatus` enum gains `SENDING`, `WAITING_STREAM`, `DELIVERING`, `ABORTED`. Classic rows use the pre-streaming path byte-for-byte; `.fn.*` commands force classic per §9 non-goal. Protocol: same `stream_v1` capability probe, same per-chunk `ackChunk` + typed `abortTransfer(reason)` as desktop. FCM `stream_ready` / `abort` wakes land in `FcmService.onMessageReceived` alongside the existing `ping` / `fasttrack` handlers.
- **Streaming label semantics (sender UI)**: During streaming, both desktop and Android show `Uploading X/N` while no recipient progress is observed (server's `delivery_state` stays `not_started` until `complete=1`, but `chunks_downloaded` can already be climbing). Once the tracker sees `chunks_downloaded > 0`, UI flips to `Sending X→Y/N` (real overlap); after the sender's upload completes it flips to `Delivering Y/N` (recipient still draining); then `Delivered` once the tracker marks `delivered=1`. The streaming delivery tracker paints `chunks_downloaded` whenever the server reports it, regardless of `delivery_state` (classic behaviour is unchanged — classic never reports `chunks_downloaded > 0` while `complete=0`, so the "paint what the server says" write is a no-op for classic rows). Tracker stall semantics are mode-aware: classic gives up after 2 min, streaming only clears the Y display and keeps polling (sender may still be uploading or WAITING_STREAM). Tracker also observes `delivery_state == "aborted"` to flip the row terminal even when the sender has no more chunks to upload (small-file case where the recipient aborts post-upload).
- **Authoritative delivery state**: Server's `sent-status` response carries `delivery_state ∈ {not_started, in_progress, delivered}` plus `chunks_downloaded`/`chunk_count`. `chunks_downloaded` is capped at `chunk_count - 1` during chunk serving; only `ack` bumps it to `chunk_count`. Invariant: `chunks_downloaded == chunk_count ⇔ downloaded == 1 ⇔ delivery_state == "delivered"`. Clients can trust any of the three as a rock-solid "done" signal.
- **Delivery tracker**: Dedicated 500ms-tick loop on both Android (`PollService.deliveryTrackerLoop`) and desktop (`Poller._delivery_tracker_loop`). 750ms abort timeout per poll; overlapping polls are skipped and logged. Idle when no active deliveries (Android also gates on screen-on). Paints "Delivering X/Y" progress; does NOT mark delivered itself — on `delivery_state == "delivered"` it clears its progress fields and delegates to the standard sent-status path (same one used on app start) as the single source of truth. DB writes only fire on value change.
- **Delivery stall safeguard**: Per-transfer timer tracks last `chunks_downloaded` advancement. If no advancement for 2 minutes, the tracker gives up on that transfer (in-memory `gaveUp` set), clears its progress fields so UI falls back to "Sent", and logs. Transfer row stays sent/undelivered; long-poll inline `sent_status` and app-restart delivery check still catch eventual delivery when the recipient comes online. Caps tracker HTTP activity to ~240 polls per stuck transfer before going quiet.
- **Connection state isolation**: Long poll uses raw HTTP requests, not the connection manager. Only the short health check affects connection state. Prevents state oscillation.
- **Storage quota**: Server enforces a per-deployment quota (default 500 MB, configurable via `server/data/config.json` — auto-created by `Config.php` with defaults on first access, so a fresh deploy never 500s on a missing file). `TransferService::init` projects the *eventual* size of the new transfer + every still-in-flight transfer for the recipient (reserved = `max(sumPendingBytes, reservedChunkCount * PROJECTED_CHUNK_SIZE)`, where `PROJECTED_CHUNK_SIZE = 2 MB` must match client `CHUNK_SIZE`). Without the projection, N parallel inits at 0 bytes each would sail past the cap. Two distinct errors: `PayloadTooLargeError` (413) when the new transfer alone exceeds the cap — terminal, clients fail the send immediately with "exceeds server quota"; `StorageLimitError` (507) when only the current queue won't fit — transient, clients enter WAITING and retry until drained.
- **WAITING state + zombie scrub**: Clients render `init → 507` transfers as `Waiting` (orange/amber) in history. Clients retry `init` with exponential backoff capped at `STORAGE_FULL_MAX_WINDOW_S = 30 min` (desktop) / `STORAGE_FULL_MAX_WINDOW_MS` (Android) — beyond that the row is marked `Failed (quota exceeded)` with `failure_reason = "quota_timeout"`. Desktop's history window additionally runs a `_scrub_zombie_waiting()` pass on every `build_list()` tick (every 1–3 s) to flip orphaned WAITING rows (crashed sender subprocess, legacy `chunks_downloaded = -1` sentinel) to Failed live, without needing to close and reopen the window. Android's `TransferViewModel.refreshTransfers` performs the same scrub via `scrubZombieWaiting`.
- **Sender cancel**: `DELETE /api/transfers/{id}` lets the sender tear down a still-delivering transfer — every stored byte is removed so the recipient gets 404 on the next chunk fetch. Auth is enforced by the pipeline; the service 404s both for unknown IDs and for transfers owned by a different sender (so a poking client can't enumerate IDs). Desktop / Android UI exposes this via "Cancel" on an in-flight row.
- **Auth recovery**: Both clients treat 401/403 from the server as a *terminal pairing failure*, not a transient blip. A 3-in-a-row latching streak (`authFailureStreak`) flips a persistent `auth_failure_kind` flag and surfaces a banner on the home screen ("Pairing lost — re-pair to continue"). Desktop uses `on_auth_failure` callbacks; Android uses an `AuthObservation` `SharedFlow` on `ApiClient`'s companion object. Tapping "Re-pair" wipes keys + paired devices (`KeyManager.resetKeypair` + `removeAllPairedDevices`), re-registers with the server, and navigates to the pairing screen. Prevents the "says online but silently broken" failure mode after a server wipe.
- **Fasttrack message relay**: Lightweight encrypted message queue for commands too small for the full transfer pipeline. Server stores opaque blobs, sends FCM wake. Used by find-my-phone; extensible for future features. 10-minute expiry, 100-message limit per recipient.
- **Find my phone**: Desktop sends encrypted start/stop commands via fasttrack. Phone plays alarm (STREAM_ALARM, bypasses silent), vibrates, reports encrypted GPS every 5s. Desktop shows location on Leaflet/OSM map (WebKitWebView, fallback to text). Requires FCM — menu item hidden without it. Configurable volume, hardcoded 5-min timeout on phone. Auto-stops on timeout. Silent search mode (GPS only, no alarm/vibration/notification — for stolen phone). Android settings: "Allow silent search" toggle (default on). Desktop: heartbeat-based status with "Lost communication" detection. Generation-counter thread safety for poll loop.
- **Find my phone GPS permission**: Android prompts on app open (FCM active + not granted + not dismissed). "Dismiss" is permanent — user can grant later in Settings. Alarm works without GPS permission (just no coordinates). Settings shows GPS permission status with grant button when FCM is active.
- **Find my phone overlay**: Android shows full-screen overlay when alarm is active (stop button, "Silent search in progress" for silent mode). No overlay notification for silent search.
- **Download progress**: Incoming transfers appear in history immediately with chunk-by-chunk progress bar. Android reuses `UPLOADING` status + `chunksUploaded`/`totalChunks` fields for incoming (differentiated from outgoing-upload by `direction`; bar colored green vs orange). Desktop shows "Downloading X/Y" with `Gtk.ProgressBar`. Both update per chunk. On completion the download logic clears its own progress fields — the transfer transitions to `COMPLETE` with no stale counters.
- **Download reliability**: Wake lock (`PARTIAL_WAKE_LOCK`, 2-min timeout refreshed per chunk) and WiFi lock prevent Android Doze from throttling downloads. Per-chunk retry (3 attempts with backoff). DB row reused on retry (no duplicate history entries). User can cancel by deleting the downloading item — current chunk finishes, transfer ACKed, no more chunks fetched.
- **Download folder manager**: Android FolderScreen lists all files in `DesktopConnector/` with thumbnails (async-loaded), file size, date. Swipe-to-delete permanently removes from storage + MediaStore. Tap to open via FileProvider. Delete-all with confirmation. Accessible via folder icon in HomeScreen top bar.
- **History clear**: Both platforms have "Clear all" with confirmation dialog. Android preserves active uploads/downloads. Desktop removes all entries. Swipe-to-delete animated (250ms shrink+fade).
- **Logging**: Opt-in file logging on both desktop and Android (off by default, "Allow logging" toggle in Settings). Desktop uses Python `RotatingFileHandler` (1MB + 1 backup = 2MB max) at `~/.config/desktop-connector/logs/`. Android gates `AppLog` writes on preference. Server logs to `data/logs/server.log` with 2-file rotation (1MB each). Desktop settings has "Download Logs" button that copies to `~/Downloads/` and opens the folder.

## Visual identity

Brand palette, icons, and UI theming follow [`docs/visual-identity-guide.md`](docs/visual-identity-guide.md). The visual language: blue-dominant (70%), neutral/soft-white (20%), warm accents (10%); 4-point sparkle star as the status/spark mark; monitor + phone + orbital arc as the full brand symbol.

**Palette (brand tokens, theme-agnostic):**
- Blues: `DcBlue970` `#000733` (dark-theme bg), `DcBlue950` `#00146C` (dark surface tier), `DcBlue900` `#0920AC` (surfaceVariant / launcher bg), `DcBlue800` `#1032D0` (primary structural), `DcBlue700` `#2058F0` (notification accent), `DcBlue500` `#3986FC` (success / outline), `DcBlue400` `#5898FB` (sky), `DcBlue200` `#A4D0FB` (pale)
- Yellows: `DcYellow500` `#FDD00C` (uploading / spark in dark), `DcYellow600` `#FAA602` (light-theme amber)
- Orange: `DcOrange700` `#EA7601` (destructive, `error` slot — replaces red entirely)
- Neutral: `DcWhiteSoft` `#E8EEFD` (body text on dark, bg on light)

**Semantic mapping (cross-component):**

| Role | Dark | Light |
|---|---|---|
| Background | `#000733` | `#E8EEFD` |
| Surface (cards, top bar) | `#00146C` | `#FFFFFF` |
| Connected / Received / Delivered (success) | `#3986FC` | `#3986FC` |
| Reconnecting | `#FDD00C` | `#FAA602` |
| Disconnected (muted) | `#A4D0FB` | `#5898FB` |
| Uploading (bar + text) | `#FDD00C` | `#FAA602` |
| Delivering (bar + text) | `#3986FC` | `#3986FC` |
| Downloading (bar) | `#5898FB` | `#1032D0` |
| Verification code / spark accent | `#FDD00C` | `#FAA602` |
| Failed / destructive / error | `#EA7601` | `#EA7601` |

Notifications (monochrome) carry state via shape: full sparkle = connected, outline sparkle = disconnected. `Notification.Builder.setColor(#2058F0)` tints the shade header brand blue.

**Rollout status:**
- **Android** (v0.2.0) — complete. `ThemeMode` pref (system/light/dark) in Settings, brand `ColorScheme` with custom `BrandColors` CompositionLocal, star notification icons, adaptive launcher icon (`master-spark.png` foreground on `#0920AC`, monochrome themed-icon layer), splash screen (`core-splashscreen` backport) showing `master.png` on theme-aware bg, system bars tinted to theme.
- **Desktop** — NOT STARTED. GTK4/libadwaita windows and pystray tray icon still use default styling. See `docs/plans/brand-rollout.md`.
- **Server** — NOT STARTED. Dashboard (`server/public/index.php` HTML), pairing pages, and error envelopes use browser defaults. See `docs/plans/brand-rollout.md`.

## Project structure

```
server/
  public/index.php          — front controller, all routes
  public/.htaccess           — URL rewriting
  .htaccess                  — root rewrite + directory protection
  src/Router.php             — URL routing + base path detection; builds RequestContext and catches ApiError in dispatch
  src/Database.php           — SQLite wrapper
  src/Config.php             — operator config loader; auto-creates data/config.json on first access (storageQuotaMB default 500)
  src/AppLog.php             — file-based server logger (2-file rotation, 1MB each)
  src/Http/                  — request pipeline (refactor-2)
    RequestContext.php           — per-request object: method, params, query, lazy json/raw body, deviceId
    ApiError.php                 — ApiError + Validation/Unauthorized/Forbidden/NotFound/Conflict/RateLimit/PayloadTooLarge/StorageLimit
    ErrorResponder.php           — single point that serializes ApiError → JSON + headers
    Validators.php               — requireNonEmptyString, requireInt, requireNullableString, requireIntParam, requireSafeTransferId
  src/Auth/                  — authentication layer (refactor-2)
    AuthIdentity.php             — value object: { deviceId }
    AuthService.php              — requireAuth() / optional(); bumps last_seen_at on successful lookup
  src/Controllers/           — thin HTTP adapters; each method takes (Database $db, RequestContext $ctx)
    DeviceController.php     — register, health (via AuthService::optional), stats, FCM token, ping, pong
    PairingController.php    — QR pairing flow
    TransferController.php   — HTTP adapter for /api/transfers/*; delegates to Services/ (init, upload, download, pending, ack, cancel, sent-status)
    DashboardController.php  — HTML dashboard
    FcmController.php        — FCM config endpoint
    FasttrackController.php  — encrypted message relay (send, pending, ack)
  src/Services/              — transfer business logic (refactor-1); throws ApiError subclasses on failure
    TransferService.php          — init, upload, download, ack, listPending, cancel; init enforces quota via PROJECTED_CHUNK_SIZE × reservedChunks, throws PayloadTooLarge (413) or StorageLimit (507)
    TransferStatusService.php    — status / delivery_state mapping (single source of truth)
    TransferNotifyService.php    — long-poll loop (25s / 500ms tick)
    TransferWakeService.php      — silent FCM wake on upload completion
    TransferCleanupService.php   — expiry, chunk/file deletion
  src/Repositories/          — persistence layer; all SQL lives here (refactor-3)
    DeviceRepository.php         — devices table: find/insert/update last_seen + fcm_token
    PairingRepository.php        — pairings + pairing_requests: find/create, stats, request lifecycle
    TransferRepository.php       — transfers table: create, progress, delivery, listings, stats counts
    ChunkRepository.php          — chunks table: metadata, byte aggregation (incl. chunks⋈transfers JOIN)
    FasttrackRepository.php      — fasttrack_messages table: insert, list pending, ack, expiry cleanup
    PingRateRepository.php       — atomic UPSERT cooldown slot + retry-after lookup
  src/Messaging/             — message transport policy (refactor-7)
    MessageTransportPolicy.php — fasttrack encrypted-payload size ceiling (128 KB)
  src/FcmSender.php          — FCM HTTP v1 API sender (JWT + OAuth2)
  migrations/001_initial.sql

desktop/
  install.sh       — idempotent Linux installer (one-liner curl | bash)
  uninstall.sh     — clean removal
  nautilus-send-to-phone.py — file manager "Send to Phone" script (Nautilus/Nemo/Dolphin)
  src/
    main.py              — thin entrypoint (refactor-5): deps → args → logging → context → register → pair? → dispatch
    bootstrap/           — startup wiring (refactor-5)
      args.py                — CLI parsing + StartupArgs + StartupMode Literal + resolve_startup_mode
      dependency_check.py    — dep detection + GTK4/Tkinter install UI (intentionally Linux-scoped)
      logging_setup.py       — console + optional rotating file logging
      startup_context.py     — StartupContext (carries DesktopPlatform) + build_startup_context + rebuild_authenticated_api
    runners/             — per-mode startup flows (refactor-5)
      registration_runner.py — register_device
      pairing_runner.py      — run_pairing_flow
      send_runner.py         — run_send_file (one-shot --send)
      receiver_runner.py     — run_receiver (tray or headless; takes DesktopPlatform)
    interfaces/          — platform capability Protocols (refactor-6)
      clipboard.py           — ClipboardBackend (read_clipboard, write_text, write_image)
      dialogs.py             — DialogBackend (pick_files, confirm, show_info)
      notifications.py       — NotificationBackend (notify + convenience helpers)
      shell.py               — ShellBackend (open_url, open_folder, launch_installer_terminal)
    backends/linux/      — Linux backend implementations (refactor-6); wraps existing helper modules
      clipboard_backend.py, dialog_backend.py, notification_backend.py, shell_backend.py
    platform/             — desktop platform boundary (refactor-10)
      contract/
        desktop_platform.py  — DesktopPlatform dataclass (name + 4 backends + capabilities)
        capabilities.py      — PlatformCapabilities flags (clipboard_text, auto_open_urls, tray, open_folder, ...)
      compose.py             — compose_desktop_platform(): raises NotImplementedError on non-Linux
      linux/
        compose.py           — compose_linux_platform() -> DesktopPlatform(name="linux", ...)
    messaging/           — shared command/message model (refactor-7)
      message_types.py       — MessageType + MessageTransport enums
      message_model.py       — DeviceMessage dataclass (type + transport + payload + metadata)
      fn_transfer_adapter.py — parse .fn.* transfer filenames/bytes into DeviceMessage
      fasttrack_adapter.py   — parse decrypted fasttrack payloads into DeviceMessage
      dispatcher.py          — MessageDispatcher (register handler -> dispatch by type)
    crypto.py            — X25519 + AES-256-GCM + HKDF
    connection.py        — exponential backoff state machine
    config.py            — persistent config (~/.config/desktop-connector/)
    api_client.py        — server API wrapper
    poller.py            — poll, download, decrypt, delivery status check (platform calls via self.platform.*; auto-open-URL gated on capabilities.auto_open_urls)
    clipboard.py         — wl-copy/xclip read/write (wrapped by LinuxClipboardBackend)
    history.py           — JSON-based transfer history (50 items)
    pairing.py           — QR code generation + tkinter pairing window
    tray.py              — pystray tray icon, spawns GTK4 windows as subprocesses (platform calls via self.platform.*; menu items gated on capabilities.clipboard_text / capabilities.open_folder)
    windows.py           — GTK4/libadwaita windows (send-files, settings, history, find-phone)
    dialogs.py           — zenity file picker + confirmation dialogs (wrapped by LinuxDialogBackend)
    notifications.py     — notify-send wrapper (wrapped by LinuxNotificationBackend)

android/app/src/main/kotlin/com/desktopconnector/
  DesktopConnectorApp.kt     — application init, Bouncy Castle, service start
  MainActivity.kt            — permissions, Compose entry
  ShareReceiverActivity.kt   — share intent handler
  crypto/
    CryptoUtils.kt           — X25519, AES-256-GCM, HKDF (Bouncy Castle)
    KeyManager.kt            — key storage (EncryptedSharedPreferences)
  network/
    ApiClient.kt             — OkHttp server API
    ConnectionManager.kt     — backoff state machine
    FcmManager.kt            — dynamic Firebase init + FCM token management
    UploadWorker.kt          — WorkManager background uploads
  messaging/                 — shared command/message model (refactor-7)
    DeviceMessage.kt         — MessageType + MessageTransport enums + DeviceMessage data class
    MessageAdapters.kt       — fromFnTransfer / fromFasttrackPayload -> DeviceMessage?
    MessageDispatcher.kt     — register handler -> dispatch by MessageType
  service/
    PollService.kt           — foreground service, polls for transfers + fasttrack messages
    FcmService.kt            — FCM message receiver, wakes PollService (transfer or fasttrack)
    FindPhoneManager.kt      — alarm, vibration, GPS reporting for find-my-phone
  data/
    AppDatabase.kt           — Room DB
    QueuedTransfer.kt        — transfer entity + DAO
    AppPreferences.kt        — SharedPreferences config
    AppLog.kt                — file-based log (2000 lines max, gated on loggingEnabled pref)
  ui/
    HomeScreen.kt            — main screen, recent files, history, swipe-to-delete, clear history
    FolderScreen.kt          — download folder manager (browse, delete, open files)
    StatusBar.kt             — connection status composable (unused, dot in title instead)
    SettingsScreen.kt        — settings with server stats and logs
    Navigation.kt            — NavHost
    theme/Theme.kt           — dark Material3 theme
    pairing/
      PairingScreen.kt       — QR scanner + manual URL entry
      PairingViewModel.kt    — pairing flow logic
    transfer/
      TransferViewModel.kt   — uploads, clipboard, history, delivery tracking

test_loop.sh                 — automated integration test
tests/protocol/              — executable protocol + platform contract tests (refactor-8, -10)
  test_desktop_message_contract.py — FnTransferAdapter/FasttrackAdapter pinning
  test_server_contract.py    — hermetic PHP server + HTTP surface + error envelope
  test_platform_contract.py  — DesktopPlatform / PlatformCapabilities shape + compose behavior
  README.md                  — run command + PHP prereq
docs/protocol.compatibility.md — preserving/extending/breaking classification (refactor-8)
docs/protocol.examples.md    — canonical request/response examples (refactor-8)
version.json                 — version tracking for all three components
temp/                        — numbered install scripts (dev only, run with sudo)
docs/plans/                  — refactoring and bugfix plans (local working notes)
```

## Config locations

- Desktop: `~/.config/desktop-connector/` (config.json, keys/, history.json, logs/)
- Android: app internal storage (EncryptedSharedPreferences for keys, Room DB for transfers, app.log)
- Server: `server/data/connector.db` (SQLite), `server/storage/` (encrypted blobs), `server/data/logs/` (server.log)

## API endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | /api/devices/register | No | Register device |
| GET | /api/health | Optional | Health check + heartbeat |
| GET | /api/devices/stats | Yes | Connection statistics |
| POST | /api/devices/fcm-token | Yes | Store FCM token for push wake |
| POST | /api/devices/ping | Yes | Probe paired device liveness — sends HIGH FCM, waits up to 5s for pong |
| POST | /api/devices/pong | Yes | Phone acks a ping — auth middleware bumps last_seen_at |
| GET | /api/fcm/config | No | Firebase client config (for dynamic init) |
| POST | /api/pairing/request | Yes | Phone sends pairing request |
| GET | /api/pairing/poll | Yes | Desktop polls for pairing |
| POST | /api/pairing/confirm | Yes | Confirm pairing |
| POST | /api/transfers/init | Yes | Init transfer |
| POST | /api/transfers/{id}/chunks/{i} | Yes | Upload chunk |
| GET | /api/transfers/pending | Yes | Get pending transfers |
| GET | /api/transfers/{id}/chunks/{i} | Yes | Download chunk |
| POST | /api/transfers/{id}/ack | Yes | Acknowledge receipt |
| DELETE | /api/transfers/{id} | Yes | Sender-initiated cancel — deletes chunks + row |
| GET | /api/transfers/sent-status | Yes | Delivery status |
| GET | /api/transfers/notify | Yes | Long poll for new transfers/deliveries (?test=1 for instant probe) |
| POST | /api/fasttrack/send | Yes | Send encrypted message to paired device (triggers FCM wake) |
| GET | /api/fasttrack/pending | Yes | Fetch pending encrypted messages |
| POST | /api/fasttrack/{id}/ack | Yes | Acknowledge and delete a message |
| GET | /dashboard | No | HTML dashboard |
