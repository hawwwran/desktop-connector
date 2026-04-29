# Desktop Connector

E2E encrypted file and clipboard sharing between Android phone and Linux desktop, via a PHP relay server.

## Architecture

- **`server/`** — PHP blind relay. Stores only encrypted blobs + device IDs. SQLite. No framework.
- **`desktop/`** — Python. pystray tray icon; GTK4/libadwaita windows run as **subprocesses** (pystray loads GTK3 internally, so GTK4 must be isolated).
- **`android/`** — Kotlin + Jetpack Compose. Foreground service for receiving; WorkManager for uploads.

## Security

- X25519 key exchange (Bouncy Castle / PyNaCl), AES-256-GCM + HKDF-SHA256.
- Server never sees plaintext. Pairing via QR + verification code.

## Special filename convention (`.fn.*`)

Files named `.fn.<function>.<subtype>` trigger receiver-side behavior instead of saving:
- `.fn.clipboard.text` / `.fn.clipboard.image` — push to system clipboard
- `.fn.unpair` — remove pairing on the receiver (sender sends this on unpair so both sides sync)
- Always forced through **classic** transfer mode (never streaming — too small to benefit).

## Fasttrack: lightweight encrypted message relay

For commands too small for the full transfer pipeline. `fasttrack_messages` table stores opaque blobs; three endpoints (send/pending/ack); server fires `{type:"fasttrack"}` FCM wake — no content leaked. Payload `{fn, action, …}` is E2E-encrypted (same AES-256-GCM as transfers), so the server is function-agnostic. 10-minute expiry, 100 pending per recipient, bidirectional. 128 KB ceiling (`MessageTransportPolicy`). Used by find-my-phone; extensible via new `fn` values with zero server changes.

## Building

```bash
# Server
php -S 0.0.0.0:4441 -t server/public/

# Desktop dev tree (deps: python3-tk, pystray, qrcode, PyNaCl, cryptography, requests, keyring)
cd desktop && python3 -m src.main              # tray
cd desktop && python3 -m src.main --headless   # headless receiver
cd desktop && python3 -m src.main --send=PATH  # one-shot send
cd desktop && python3 -m src.main --pair       # pairing flow

# GTK4 windows (must be subprocesses)
python3 -m src.windows {send-files|settings|history|pairing|find-phone} --config-dir=~/.config/desktop-connector

# Android
export ANDROID_HOME=/opt/android-sdk
cd android && ./gradlew assembleDebug   # → app/build/outputs/apk/debug/app-debug.apk

# Desktop AppImage (release shape — packaging plan: docs/plans/desktop-appimage-packaging-plan.md)
./desktop/packaging/appimage/build-appimage.sh --source=$PWD --output=/tmp/dc-out

# Full integration loop
./test_loop.sh
```

## Installation (desktop)

The release shape is a **signed AppImage** fetched from GitHub Releases — single self-contained file, no host apt/pip touched, in-app updates via the tray menu. The dev-tree apt+pip path stays available as `install-from-source.sh` for contributors.

Idempotent installer:
```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
```
Fetches the latest `desktop/v*` release, GPG-verifies against `docs/release/desktop-signing.pub.asc` (fingerprint `FBEFCEC1 3D7A EC08 1081 2975 491C 9043 90F4 E03B`), drops the AppImage at `~/.local/share/desktop-connector/desktop-connector.AppImage`, runs it once. The AppImage's first-launch hook (`bootstrap/appimage_install_hook.py`) writes the `.desktop` menu entry + autostart entry + Nautilus/Nemo "Send to Phone" scripts + Dolphin service menu, all pointing at `$APPIMAGE`. The onboarding dialog (`bootstrap/appimage_onboarding.py`) asks for the relay server URL on a fresh machine. P.4b's migration removes any prior install-from-source layout cleanly. Uninstall: `~/.local/share/desktop-connector/uninstall.sh`. Releases are signed + reproducibly built by `.github/workflows/desktop-release.yml` on `desktop/v*` tag push; see `docs/release/desktop-signing-recovery.md` for the signing key's storage / rotation runbook + the in-app update flow's load-bearing details (`UPDATE_INFORMATION` embed, rolling `desktop-latest` GitHub Release for stream isolation, runtime relocate when asset filename ≠ install path).

Contributors can use `install-from-source.sh` (the old apt+pip+`src/`-copy path) — drops `src/` into `~/.local/share/desktop-connector/`, creates `~/.local/bin/desktop-connector` shell wrapper, runs `python3 -m src.main`. Bouncing between AppImage and source-tree installs is safe (last install wins for system integration; `~/.config/desktop-connector/` is shared and preserved).

## Server deployment

Shared-host layout (Apache):
```
desktop-connector/
  .htaccess              # root rewrite + protects src/ data/ storage/ migrations/ *.json
  public/index.php       # front controller
  public/.htaccess       # rewrite non-file URLs to index.php
  src/ data/ storage/ migrations/
  firebase-service-account.json  google-services.json   # optional (FCM)
```
Requires PHP 8.0+, SQLite3, mod_rewrite (+ curl/openssl for FCM). Router auto-detects base path from `SCRIPT_NAME` — works in any subdirectory.

## Key design decisions

### Server pipeline & persistence
- **Request pipeline**: handlers take `(Database $db, RequestContext $ctx)`. `Router` builds `RequestContext` (route params, query, lazy body) and routes registered via `authGet`/`authPost` resolve identity through `AuthService::requireAuth` before dispatch. Controllers validate via `Validators::*` and throw `ApiError` subclasses; top-level `try/catch` hands them to `ErrorResponder`. No controller touches `$_SERVER`/`$_GET`/`php://input` or hand-writes error JSON. The path-traversal guard on `{transfer_id}` is a pipeline validator, so every endpoint accepting that param is protected.
- **Persistence**: all SQL lives in `server/src/Repositories/`. Services/controllers express intent (`markTransferDelivered`, `tryClaimCooldown`, `sumPendingBytesForRecipient`); repos own row shape. The ping-rate UPSERT and `MAX(chunks_downloaded, :progress)` idiom stay byte-exact in repos — WAL serialization and the `chunks_downloaded == chunk_count ⇔ downloaded == 1` invariant depend on it. No raw `$db->query*`/`$db->execute` outside repos.
- **PHP single-threaded** (built-in dev server): dashboard auto-refresh can block API calls. For prod, use nginx+php-fpm or Apache+mod_php.

### Logging
Shared dot-notation event vocabulary (`transfer.init.accepted`, `ping.request.rate_limited`, `clipboard.write_text.succeeded`). Catalog: `docs/diagnostics.events.md`. Events are prose anchors in the log message (not structured fields); all runtimes share `[timestamp] [level] [tag] message`. Correlation IDs (`transfer_id`, `device_id`…) truncate to 12 chars so flows grep across server/desktop/Android. **Never log**: keys, auth/FCM tokens, decrypted clipboard/file/GPS content, public keys, encrypted payloads. Server-side `apierror.caught` is a central Router catch that logs every 4xx/5xx — no per-throw instrumentation needed.

### Transfers
- **Chunked**: 2 MB chunks for resume + memory. `PROJECTED_CHUNK_SIZE = 2 MB` on server must match client `CHUNK_SIZE`.
- **Three-phase state** (outgoing): `uploading → delivering → delivered`, each with its own progress bar and field set. Uploading owns `chunksUploaded`/`totalChunks` (Android) or `chunks_downloaded`/`chunks_total` (desktop, historical name). Delivering owns `deliveryChunks`/`deliveryTotal` or `recipient_chunks_downloaded`/`recipient_chunks_total`. Each phase clears its own fields on completion — no shared state.
- **Authoritative delivery state**: `sent-status` returns `delivery_state ∈ {not_started, in_progress, delivered}` + `chunks_downloaded`/`chunk_count`. `chunks_downloaded` caps at `chunk_count - 1` during chunk serving; only `ack` bumps to `chunk_count`. Invariant: `chunks_downloaded == chunk_count ⇔ downloaded == 1 ⇔ delivery_state == "delivered"`.
- **Delivery tracker**: dedicated 500 ms loop (Android `PollService.deliveryTrackerLoop`, desktop `Poller._delivery_tracker_loop`). 750 ms abort per poll; overlapping polls skipped and logged. Idle when no active deliveries (Android also gates on screen-on). Paints "Delivering X/Y"; does **not** mark delivered — on `delivery_state == "delivered"` it clears its fields and delegates to the standard sent-status path (single source of truth). DB writes only on value change.
- **Stall safeguard**: per-transfer timer on `chunks_downloaded` advancement. 2 min of no advance → tracker gives up (in-memory `gaveUp`), clears its fields (UI falls back to "Sent"), caps HTTP at ~240 polls per stuck transfer. Row stays sent/undelivered; long-poll inline `sent_status` + app-restart check still catch eventual delivery.
- **Long polling**: `GET /api/transfers/notify` blocks up to 25 s, ticks every 500 ms for new transfers, deliveries, or recipient progress. Inlines `sent_status` when progress/delivery detected. `?test=1` is an instant probe. Uses raw HTTP (not the connection manager) so only the short health check affects connection state — prevents oscillation.
- **Sender cancel**: `DELETE /api/transfers/{id}` tears down a still-delivering transfer. Service 404s both unknown IDs and transfers owned by another sender (can't enumerate). Recipient gets 404 on next chunk fetch.

### Streaming relay (mode negotiation)
- Transfers negotiate `mode ∈ {classic, streaming}` at init. Streaming collapses upload+delivery into one overlapping pipeline: server fires `stream_ready` FCM on first stored chunk; recipient pulls immediately; each chunk is per-chunk ACKed (`POST /api/transfers/{id}/chunks/{i}/ack`) so the server deletes the blob immediately. Peak on-disk: `~1 × CHUNK_SIZE` vs classic's `N × CHUNK_SIZE`.
- **Negotiation is server-authoritative**: client requests `mode=streaming`; server downgrades to `classic` if recipient isn't `last_seen_at >= now-15s`, if operator set `streamingEnabled=false`, or if the client didn't advertise capability. Clients gate on `stream_v1` in `GET /api/health.capabilities`.
- **Abort protocol**: `DELETE /api/transfers/{id}` accepts both sender and recipient with typed `reason ∈ {sender_abort, sender_failed, recipient_abort}`; cross-role reasons 400. Next chunk op on an aborted transfer returns **410 Gone** with `abort_reason` — each side learns on its next API call.
- **425 Too Early** distinguishes "chunk not uploaded yet" from 404 unknown, with `Retry-After` + ms-precision `retry_after_ms`.
- **Retry budgets** per-side: recipient tolerates 5 min of continuous 425s → aborts as `recipient_abort`; sender tolerates 30 min of continuous 507s (mid-stream quota backpressure) → aborts as `sender_failed` with `failure_reason=quota_timeout`.
- `TransferLifecycle` allows `UPLOADING → DELIVERING` and `INITIALIZED → DELIVERING` precisely because streaming overlaps phases. Classic keeps the conservative `UPLOADING → UPLOADED → DELIVERING` path, byte-for-byte unchanged.
- **Streaming status vocab** (desktop `history.py`): `TransferStatus.{UPLOADING, SENDING, WAITING, WAITING_STREAM, DELIVERING, COMPLETE, DELIVERED, DOWNLOADING, FAILED, ABORTED}`. Streaming sends walk `uploading → sending → (waiting_stream ↔ sending)* → complete → delivered`. `waiting_stream` is **mid-stream 507** backoff (yellow); `waiting` is **init-time 507**. `ABORTED` is terminal orange with optional suffix ("sender cancelled", "recipient cancelled", "sender gave up").
- **Sender UI labels**: `Uploading X/N` until tracker sees `chunks_downloaded > 0` → `Sending X→Y/N` (real overlap) → `Delivering Y/N` after sender's upload completes → `Delivered` when tracker marks `delivered=1`. Tracker paints `chunks_downloaded` whenever the server reports it regardless of `delivery_state` (no-op for classic, which never reports `>0` while `complete=0`). Stall: classic gives up after 2 min; streaming only clears the Y display and keeps polling (sender may still be WAITING_STREAM). Tracker also observes `delivery_state == "aborted"` so small-file recipient-aborts flip the row terminal even when there are no more chunks to upload.
- **Android streaming state machines**: `UploadStreamLoop.kt` / `DownloadStreamLoop.kt` are pure functions over typed `ChunkUpload/DownloadResult` outcomes — JVM-unit-testable with virtual clock + fake API. `UploadWorker.runStreamingUpload` wraps sender with wake + WiFi lock + Room writes; `PollService.receiveStreamingTransfer` wraps recipient with file IO + `.incoming_<tid>.part` + atomic rename.
- **Field-ownership contract (Android)**: upload loop owns `status` / `chunksUploaded` / `totalChunks` / `abortReason` / `failureReason` / `waitingStartedAt`. Delivery tracker owns `deliveryChunks` / `deliveryTotal` / `delivered` (via `markDelivered` on `delivery_state="delivered"`). `maybeFlipToSending` in `UploadWorker` reads tracker-owned `deliveryChunks` per chunk Ok and flips `UPLOADING → SENDING` when it crosses zero; on `Delivered` outcome the row stays `SENDING` until the tracker flips `delivered=1` — no intermediate `COMPLETE` for streaming.
- Room schema v7→v8 adds `mode`, `negotiatedMode`, `abortReason`, `failureReason`, `waitingStartedAt`.

### Quota & storage
- **Quota** default 500 MB, configurable via `server/data/config.json` — `Config.php` auto-creates it with defaults on first access (fresh deploys don't 500 on missing file).
- `TransferService::init` projects the eventual size: `reserved = max(sumPendingBytes, reservedChunkCount * PROJECTED_CHUNK_SIZE)`. Without projection, N parallel inits at 0 bytes would sail past the cap.
- **413 `PayloadTooLargeError`**: new transfer alone exceeds cap — terminal, client fails immediately ("exceeds server quota").
- **507 `StorageLimitError`**: only the current queue won't fit — transient, clients enter WAITING and retry until drained.
- **WAITING + zombie scrub**: clients render `init → 507` as `Waiting` (orange/amber). Retry `init` with exponential backoff capped at `STORAGE_FULL_MAX_WINDOW_S = 30 min` (desktop) / `…_MS` (Android); beyond that the row is `Failed (quota exceeded)` with `failure_reason = "quota_timeout"`. Desktop `_scrub_zombie_waiting()` runs every `build_list()` tick (1–3 s) to flip orphaned WAITING (crashed sender subprocess, legacy `chunks_downloaded = -1` sentinel) → Failed live. Android mirrors via `TransferViewModel.scrubZombieWaiting`.

### Liveness (ping/pong)
- On-demand only — no periodic heartbeats. `POST /api/devices/ping` → HIGH-priority FCM `{type:"ping"}` → phone's `FcmService` replies `POST /api/devices/pong` (auth middleware bumps `last_seen_at`) → ping handler polls `last_seen_at` up to 5 s, returns `{online, rtt_ms, via}`.
- Fires on desktop connect, tray menu open (30 s cache), and every 5 min background. HIGH FCM bypasses Doze, RTT ~1 s. Worst case ~288 wakes/day; ~0 when desktop isn't running.
- **Shortcut**: if `last_seen_at >= baseline` (phone hit server via transfers/long poll this second), server skips FCM and returns `via: fresh`.
- Pong uses 3 s-timeout OkHttp, synchronous on `onMessageReceived` thread (inside FCM's ~10 s wakelock) — guaranteed completion without detached workers.
- **Rate limit + debounce** = one primitive: atomic UPSERT on `ping_rate(sender_id, recipient_id, cooldown_until)` with `ON CONFLICT ... DO UPDATE ... WHERE cooldown_until <= now`. `changes()==0` → live slot exists → 429 with `Retry-After`. 30 s cooldown > 5 s max blocking wait, so concurrent pings for a pair are impossible AND a pair is capped at 1 ping / 30 s. Defends against compromised-paired-device abuse (PHP-worker starvation + phone battery drain). Legit clients don't notice — desktop caches 30 s client-side anyway.
- **Security boundary**: `/api/devices/ping` and `/pong` are behind `Router::authenticate`. Registration is public but produces creds bound to a fresh device that isn't paired with anyone; ping handler 403s if no `pairings` row links caller and target. Threat model for rate limiting is "compromised/malicious already-paired device", not "public internet".

### FCM push wake
Optional. Server reads `firebase-service-account.json` + `google-services.json` at root. Data-only FCM with HIGH priority on transfer complete (bypasses Doze). Android initializes Firebase dynamically from `GET /api/fcm/config` — no baked-in `google-services.json`, so each server deployment can use its own Firebase project. Falls back to long-polling if unavailable.

### Auth recovery
Both clients treat 401/403 as **terminal pairing failure**, not transient. 3-in-a-row latching streak (`authFailureStreak`) flips a persistent `auth_failure_kind` flag and surfaces a home-screen banner ("Pairing lost — re-pair to continue"). Desktop: `on_auth_failure` callbacks. Android: `AuthObservation` `SharedFlow` on `ApiClient`'s companion object. Tapping "Re-pair" wipes keys + paired devices (`KeyManager.resetKeypair` + `removeAllPairedDevices`), re-registers, navigates to pairing. Prevents the "says online but silently broken" mode after a server wipe.

### Find my phone
Desktop sends encrypted start/stop via fasttrack. Phone: alarm (STREAM_ALARM, bypasses silent), vibrates, reports encrypted GPS every 5 s. Desktop: location on Leaflet/OSM map (WebKitWebView, text fallback). Requires FCM — menu hidden without it. Configurable volume, hardcoded 5-min phone-side timeout. Silent search mode (GPS only, no alarm/vibration/notification, for stolen phone); Android "Allow silent search" toggle (default on). Desktop heartbeat-based status with "Lost communication" detection. Generation-counter thread safety for poll loop.
- **GPS permission**: Android prompts on app open when FCM active + not granted + not dismissed. "Dismiss" is permanent (user can grant later in Settings). Alarm works without GPS (just no coords).
- **Overlay**: full-screen when alarm active (stop button; "Silent search in progress" in silent mode — no overlay notification for silent).

### Download reliability & UX
- Incoming transfers appear in history immediately with chunk progress. Android reuses `UPLOADING` + `chunksUploaded`/`totalChunks` for incoming (differentiated by `direction`; bar green vs orange). Desktop shows "Downloading X/Y" with `Gtk.ProgressBar`. On completion the download logic clears its own fields — transitions to `COMPLETE` with no stale counters.
- Wake lock (`PARTIAL_WAKE_LOCK`, 2-min refresh per chunk) + WiFi lock prevent Doze throttling. Per-chunk retry (3 attempts, backoff). Retry reuses the DB row (no duplicate history). Cancel by deleting the downloading item — current chunk finishes, transfer ACKed, no more fetched.

### Android UX bits
- **Notification icons monochrome** — shapes encode state (filled sparkle = connected, outline = disconnected). No intermediate "connecting" state.
- **Notification clearing**: transfer notifications cleared on app open/resume, keeping only persistent service notification.
- **MediaStore**: files saved to `DesktopConnector/` are scanned so they appear in gallery/picker.
- **Recent files**: HomeScreen lazy-loads from MediaStore, excluding `DesktopConnector/`. Refreshes on resume.
- **Pull-to-refresh** wraps the full HomeScreen (buttons + recent + history) and also resets connection backoff.
- **Folder screen**: `DesktopConnector/` browser with async thumbnails, swipe-to-delete (storage + MediaStore), delete-all with confirm.
- **APK install**: tapping an APK in history checks `REQUEST_INSTALL_PACKAGES`, redirects to settings if needed, opens system package installer.
- **History clear**: both platforms have "Clear all" with confirm. Android preserves active uploads/downloads; desktop removes all. Swipe-to-delete animated (250 ms shrink+fade).

### Desktop UX bits
- **Tray icon**: donut-shaped. Green/yellow = connected to server, phone offline; green/white = both online. Checked every 30 s via stats.
- **Config reload**: `paired_devices` reloads from disk on every access to pick up subprocess-window changes (settings, pairing).
- **Dependency checker** at startup — GTK4 dialog with "Install" button opens installer in terminal.

### Logging (opt-in)
Off by default, "Allow logging" toggle in Settings. Desktop: Python `RotatingFileHandler` (1 MB + 1 backup = 2 MB max) at `~/.config/desktop-connector/logs/`. Android: `AppLog` writes gated on pref. Server: `data/logs/server.log` with 2-file rotation (1 MB each). Desktop settings has "Download Logs" (copies to `~/Downloads/`, opens folder).

## Visual identity

See [`docs/visual-identity-guide.md`](docs/visual-identity-guide.md). Blue-dominant (70%) + neutral soft-white (20%) + warm accents (10%); 4-point sparkle = status/spark mark; monitor + phone + orbital arc = full symbol.

**Palette:** `DcBlue970 #000733` (dark bg), `DcBlue950 #00146C` (dark surface), `DcBlue900 #0920AC` (surfaceVariant / launcher bg), `DcBlue800 #1032D0` (primary), `DcBlue700 #2058F0` (notification accent), `DcBlue500 #3986FC` (success), `DcBlue400 #5898FB` (sky), `DcBlue200 #A4D0FB` (pale). `DcYellow500 #FDD00C` (uploading / spark in dark), `DcYellow600 #FAA602` (light amber). `DcOrange700 #EA7601` (destructive / error — **replaces red entirely**). `DcWhiteSoft #E8EEFD` (body text on dark, bg on light).

| Role | Dark | Light |
|---|---|---|
| Background | `#000733` | `#E8EEFD` |
| Surface | `#00146C` | `#FFFFFF` |
| Connected / Delivered | `#3986FC` | `#3986FC` |
| Reconnecting / Uploading / Verification / Spark | `#FDD00C` | `#FAA602` |
| Disconnected (muted) | `#A4D0FB` | `#5898FB` |
| Delivering | `#3986FC` | `#3986FC` |
| Downloading | `#5898FB` | `#1032D0` |
| Failed / destructive | `#EA7601` | `#EA7601` |

Monochrome notifications encode state via shape (full sparkle = connected, outline = disconnected). `Notification.Builder.setColor(#2058F0)` tints the shade header brand blue.

**Rollout:** Android v0.2.0 complete (`ThemeMode` pref, `BrandColors` CompositionLocal, star notification icons, adaptive launcher `master-spark.png` on `#0920AC`, `core-splashscreen` backport, themed system bars). Desktop + server not started — see `docs/plans/brand-rollout.md`.

## Project structure

```
server/
  public/index.php, public/.htaccess, .htaccess
  src/Router.php                      # routing, base-path detect, RequestContext, ApiError catch
  src/Database.php Config.php AppLog.php
  src/Http/                            # RequestContext, ApiError (Validation/Unauth/Forbidden/NotFound/Conflict/RateLimit/PayloadTooLarge/StorageLimit), ErrorResponder, Validators
  src/Auth/                            # AuthIdentity, AuthService (requireAuth/optional; bumps last_seen_at)
  src/Controllers/                     # Device, Pairing, Transfer, Dashboard, Fcm, Fasttrack
  src/Services/                        # TransferService, TransferStatusService, TransferNotifyService, TransferWakeService, TransferCleanupService
  src/Repositories/                    # Device, Pairing, Transfer, Chunk, Fasttrack, PingRate
  src/Messaging/MessageTransportPolicy.php    # fasttrack 128 KB ceiling
  src/FcmSender.php                    # FCM HTTP v1 (JWT + OAuth2)
  migrations/001_initial.sql

desktop/
  install.sh                           # AppImage installer: fetch + GPG-verify + place + run (release path)
  install-from-source.sh               # apt+pip dev-tree path (contributors / older distros)
  uninstall.sh  nautilus-send-to-phone.py
  packaging/appimage/                  # build-appimage.sh, AppRun.sh, recipe + vendored linuxdeploy
  assets/brand/                        # bundled icon + sparkle PNGs (tray composites these)
  src/
    main.py                            # thin entrypoint: relocate → enforce-single → deps → args → logging → context → onboard → migrate → install hook → register → pair? → dispatch
    bootstrap/                         # cross-cutting startup pieces
      args.py logging_setup.py app_version.py startup_context.py dependency_check.py
      appimage_relocate.py             # self-install + single-instance enforcement on AppImage launch
      appimage_install_hook.py         # writes .desktop / autostart / Nautilus / Nemo / Dolphin entries pointing at $APPIMAGE
      appimage_migration.py            # surgical removal of apt-pip artefacts on first AppImage launch (preserves AppImage + uninstall.sh)
      appimage_onboarding.py           # first-launch GTK4 dialog (subprocess via windows.py); commit_onboarding_settings + probe_server are unit-testable
    updater/                           # in-app updater (AppImage installs only)
      version_check.py                 # GitHub Releases JSON poll + 24h cache + If-Modified-Since; UpdateInfo dataclass; dismissal helpers
      update_runner.py                 # wraps appimageupdatetool; UpdateOutcome.{UPDATED,NO_CHANGE,FAILED} via sha256 compare
    runners/                           # registration, pairing, send, receiver (tray/headless)
    interfaces/                        # Protocols: clipboard, dialogs, notifications, shell
    backends/linux/                    # Linux implementations of the 4 Protocols
    platform/                          # DesktopPlatform contract + compose (non-Linux raises NotImplementedError)
    messaging/                         # MessageType/Transport, DeviceMessage, fn_transfer_adapter, fasttrack_adapter, dispatcher
    crypto.py  connection.py  config.py  api_client.py
    poller.py                          # poll/download/decrypt/delivery; platform via self.platform.*
    history.py                         # JSON history (50 items); TransferStatus enum
    pairing.py                         # QR + tkinter pairing window
    tray.py                            # pystray; spawns GTK4 windows as subprocesses; update menu items + 24h check thread
    windows.py                         # GTK4/libadwaita windows (send-files, settings, history, pairing, find-phone, onboarding)
    clipboard.py  dialogs.py  notifications.py   # Linux helpers (wl-copy/xclip, zenity, notify-send)

android/app/src/main/kotlin/com/desktopconnector/
  DesktopConnectorApp.kt  MainActivity.kt  ShareReceiverActivity.kt
  crypto/                  # CryptoUtils, KeyManager
  network/                 # ApiClient (OkHttp), ConnectionManager, FcmManager, UploadWorker
  messaging/               # DeviceMessage, MessageAdapters, MessageDispatcher
  service/                 # PollService, FcmService, FindPhoneManager
  data/                    # AppDatabase (Room), QueuedTransfer, AppPreferences, AppLog
  ui/                      # HomeScreen, FolderScreen, SettingsScreen, Navigation, theme/
  ui/pairing/              # PairingScreen, PairingViewModel
  ui/transfer/             # TransferViewModel

test_loop.sh                          # full closed-loop integration test
tests/protocol/                       # test_desktop_message_contract, test_server_contract, test_platform_contract,
                                      #   test_desktop_appimage_{install_hook,migration,onboarding,relocate},
                                      #   test_desktop_updater_{version_check,runner}
docs/protocol.compatibility.md  docs/protocol.examples.md  docs/diagnostics.events.md
docs/plans/                           # local working notes (refactor + bugfix plans)
docs/release/                         # AppImage signing pubkey + recovery / trust-model runbook
docs/visual-identity-guide.md
.github/workflows/desktop-release.yml # CI: build + sign + publish AppImage on desktop/v* tag push
version.json
```

## Config locations

- Desktop: `~/.config/desktop-connector/` (config.json, keys/, history.json, logs/)
- Android: app internal storage (EncryptedSharedPreferences for keys, Room for transfers, app.log)
- Server: `server/data/connector.db`, `server/storage/`, `server/data/logs/`

## API endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | /api/devices/register | No | Register device |
| GET | /api/health | Optional | Health check + heartbeat; `capabilities` advertises `stream_v1` |
| GET | /api/devices/stats | Yes | Connection statistics |
| POST | /api/devices/fcm-token | Yes | Store FCM token |
| POST | /api/devices/ping | Yes | Probe paired device — HIGH FCM, up to 5 s wait for pong |
| POST | /api/devices/pong | Yes | Phone acks a ping (bumps `last_seen_at`) |
| GET | /api/fcm/config | No | Firebase client config (dynamic init) |
| POST | /api/pairing/request | Yes | Phone sends pairing request |
| GET | /api/pairing/poll | Yes | Desktop polls for pairing |
| POST | /api/pairing/confirm | Yes | Confirm pairing |
| POST | /api/transfers/init | Yes | Init transfer (negotiates `mode`) |
| POST | /api/transfers/{id}/chunks/{i} | Yes | Upload chunk |
| GET | /api/transfers/pending | Yes | Get pending transfers |
| GET | /api/transfers/{id}/chunks/{i} | Yes | Download chunk (425 with `retry_after_ms` if not uploaded yet) |
| POST | /api/transfers/{id}/chunks/{i}/ack | Yes | Per-chunk ACK (streaming) — server deletes blob |
| POST | /api/transfers/{id}/ack | Yes | Whole-transfer ACK (classic) |
| DELETE | /api/transfers/{id} | Yes | Typed abort/cancel (`reason ∈ {sender_abort, sender_failed, recipient_abort}`) |
| GET | /api/transfers/sent-status | Yes | Delivery status (`delivery_state`, `chunks_downloaded`) |
| GET | /api/transfers/notify | Yes | Long poll (≤25 s, 500 ms tick); `?test=1` = instant probe |
| POST | /api/fasttrack/send | Yes | Send encrypted message (triggers FCM wake) |
| GET | /api/fasttrack/pending | Yes | Fetch pending messages |
| POST | /api/fasttrack/{id}/ack | Yes | Ack and delete |
| GET | /dashboard | No | HTML dashboard |
