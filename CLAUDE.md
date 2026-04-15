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

GTK4 windows (settings, history, send files) run as separate processes:
```bash
python3 -m src.windows send-files --config-dir=~/.config/desktop-connector
python3 -m src.windows settings --config-dir=~/.config/desktop-connector
python3 -m src.windows history --config-dir=~/.config/desktop-connector
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
- **Chunked transfers**: 2MB chunks for resume capability and memory efficiency.
- **Delivery ACK**: Server tracks `downloaded` flag. Sender polls `GET /api/transfers/sent-status` to update "Sent" → "Delivered" in history.
- **Battery**: Android app pauses polling when screen is off. With FCM configured, the server sends a silent push on new transfers, waking the app to poll immediately. Without FCM, polls resume on screen wake. Zero battery drain while idle.
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
- **Long polling**: Server endpoint `GET /api/transfers/notify` blocks up to 25s, checks every 1s for new transfers or deliveries. Clients test with `?test=1` (instant response) before committing to long poll. Falls back to regular polling if unavailable. Status visible in both apps' settings.
- **Delivery tracking**: Server records `delivered_at` timestamp on ack. Long poll wakes sender immediately when delivery is detected. Both clients check delivery status on every poll cycle.
- **Connection state isolation**: Long poll uses raw HTTP requests, not the connection manager. Only the short health check affects connection state. Prevents state oscillation.

## Project structure

```
server/
  public/index.php          — front controller, all routes
  public/.htaccess           — URL rewriting
  .htaccess                  — root rewrite + directory protection
  src/Router.php             — URL routing + auth + base path detection
  src/Database.php           — SQLite wrapper
  src/Controllers/
    DeviceController.php     — register, health, stats, FCM token
    PairingController.php    — QR pairing flow
    TransferController.php   — upload, download, ack, sent-status, FCM wake
    DashboardController.php  — HTML dashboard
    FcmController.php        — FCM config endpoint
  src/FcmSender.php          — FCM HTTP v1 API sender (JWT + OAuth2)
  migrations/001_initial.sql

desktop/
  install.sh       — idempotent Linux installer (one-liner curl | bash)
  uninstall.sh     — clean removal
  nautilus-send-to-phone.py — file manager "Send to Phone" script (Nautilus/Nemo/Dolphin)
  src/
    main.py          — entry point (--headless, --send, --pair) + dependency checker
  crypto.py        — X25519 + AES-256-GCM + HKDF
  connection.py    — exponential backoff state machine
  config.py        — persistent config (~/.config/desktop-connector/)
  api_client.py    — server API wrapper
  poller.py        — poll, download, decrypt, delivery status check
  clipboard.py     — wl-copy/xclip read/write
  history.py       — JSON-based transfer history (50 items)
  pairing.py       — QR code generation + tkinter pairing window
  tray.py          — pystray tray icon, spawns GTK4 windows as subprocesses
  windows.py       — GTK4/libadwaita windows (send-files, settings, history)
  dialogs.py       — zenity file picker + confirmation dialogs
  notifications.py — notify-send wrapper

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
  service/
    PollService.kt           — foreground service, polls for incoming transfers
    FcmService.kt            — FCM message receiver, wakes PollService
  data/
    AppDatabase.kt           — Room DB
    QueuedTransfer.kt        — transfer entity + DAO
    AppPreferences.kt        — SharedPreferences config
    AppLog.kt                — file-based log (2000 lines max)
  ui/
    HomeScreen.kt            — main screen, recent files, history, swipe-to-delete
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
version.json                 — version tracking for all three components
temp/                        — numbered install scripts (dev only, run with sudo)
```

## Config locations

- Desktop: `~/.config/desktop-connector/` (config.json, keys/, history.json)
- Android: app internal storage (EncryptedSharedPreferences for keys, Room DB for transfers)
- Server: `server/data/connector.db` (SQLite), `server/storage/` (encrypted blobs)

## API endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | /api/devices/register | No | Register device |
| GET | /api/health | Optional | Health check + heartbeat |
| GET | /api/devices/stats | Yes | Connection statistics |
| POST | /api/devices/fcm-token | Yes | Store FCM token for push wake |
| GET | /api/fcm/config | No | Firebase client config (for dynamic init) |
| POST | /api/pairing/request | Yes | Phone sends pairing request |
| GET | /api/pairing/poll | Yes | Desktop polls for pairing |
| POST | /api/pairing/confirm | Yes | Confirm pairing |
| POST | /api/transfers/init | Yes | Init transfer |
| POST | /api/transfers/{id}/chunks/{i} | Yes | Upload chunk |
| GET | /api/transfers/pending | Yes | Get pending transfers |
| GET | /api/transfers/{id}/chunks/{i} | Yes | Download chunk |
| POST | /api/transfers/{id}/ack | Yes | Acknowledge receipt |
| GET | /api/transfers/sent-status | Yes | Delivery status |
| GET | /api/transfers/notify | Yes | Long poll for new transfers/deliveries (?test=1 for instant probe) |
| GET | /dashboard | No | HTML dashboard |
