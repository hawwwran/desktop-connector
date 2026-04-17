# Desktop Connector — Roadmap

Potential features organized by effort. All use the existing `.fn.` transfer convention unless noted.

## Done

### ~~Send URL~~
- Implemented via smart link detection in clipboard transfers
- Share from Chrome/YouTube sends URL as clipboard text
- Both apps detect single URLs, show link icon, Open/Copy dialog
- Desktop auto-opens links by default (configurable in Settings)

### ~~Right-click "Send to Phone"~~
- File manager integration for Nautilus, Nemo, and Dolphin
- Right-click any file(s) → Scripts → Send to Phone
- Auto-detected and installed by install.sh

### ~~Long polling~~
- Server endpoint blocks up to 25s, returns instantly on new data
- ~1s delivery latency instead of 10-30s polling
- Graceful fallback to regular polling if server doesn't support it
- `?test=1` instant probe for availability testing
- Status visible in both apps' settings with retry button

### ~~Delivery tracking~~
- Server records `delivered_at` timestamp on ack
- Long poll wakes sender immediately on delivery
- "Sent" → "Delivered" updates in ~1s

### ~~FCM push wake~~
- Server sends silent FCM data message when a transfer completes
- Android wakes from screen-off and polls immediately (~2s delivery)
- Firebase initialized dynamically from server config — no baked-in credentials
- Each server deployment can use its own Firebase project
- Optional: falls back to long-polling if server has no FCM config
- Status and manual "Check" button in Android settings

### ~~Find my phone~~
- Uses fasttrack encrypted message relay (not `.fn.` transfers — too heavy for lightweight commands)
- Desktop tray menu: "Find my Phone" (only visible when FCM available)
- GTK4 window with volume/timeout sliders, start/stop, Leaflet/OSM map (WebKit fallback to text coordinates)
- Phone: `MediaPlayer` with `STREAM_ALARM` at configurable volume, vibration, looping
- Encrypted GPS reporting every 5s via fasttrack (server never sees coordinates)
- Auto-stop on configurable timeout (max 5 min)
- Notification with "Stop Alarm" action on phone
- GPS permission: prompted on app open (FCM active + not yet granted + not dismissed), grantable from Settings
- Requires FCM — feature gated on server FCM availability

### ~~Logging infrastructure~~
- Opt-in file logging on desktop and Android ("Allow logging" toggle in Settings, off by default)
- Desktop: Python `RotatingFileHandler` — 1MB + 1 backup = 2MB max, "Download Logs" button in Settings
- Android: `AppLog` gated on `loggingEnabled` preference, immediate toggle effect
- Server: `AppLog.php` with 2-file rotation (1MB each), logs fasttrack and FCM operations
- Addresses data privacy: GPS coordinates and decrypted payloads only written to log files when user opts in

### ~~Download folder manager (Android)~~
- FolderScreen: lists all files in `DesktopConnector/` with thumbnails, file size, date
- Swipe-to-delete with animation (250ms shrink+fade), permanently removes from storage + MediaStore
- Tap to open via FileProvider, Delete-all with confirmation
- Folder icon in HomeScreen top bar

### ~~Download progress~~
- Incoming transfers appear in history immediately with progress bar (chunk-by-chunk)
- Wake lock + WiFi lock prevent Doze from throttling downloads
- Per-chunk retry (3 attempts with backoff), DB row reused on retry
- User can cancel by deleting downloading item
- Clear-all history with confirmation (preserves active transfers)

## Medium effort

*Note: The fasttrack encrypted message relay (`/api/fasttrack/`) is now available for lightweight bidirectional commands. Future features can use new `fn` values inside encrypted payloads — no server changes needed.*

### Notification mirroring
- Android `NotificationListenerService` reads notifications
- Sends as `.fn.notification.{app}` with JSON payload (title, text, icon, app name)
- Desktop shows as native notification via `notify-send`
- Privacy concern: user must explicitly enable in Android settings
- Filter by app (don't forward every notification)

### Transfer resume
- Track which chunks were successfully uploaded per transfer
- On retry, skip already-uploaded chunks
- Server already stores individual chunks — just need client-side tracking
- Important for large video files on unstable connections
- *Partial: retry reuses DB row and restarts from chunk 0. Full resume (skip downloaded chunks) not yet implemented.*

### Multi-device support
- Pair with multiple desktops or multiple phones
- Each pairing has its own X25519 keypair and symmetric key
- UI: device picker when sending, or send to all
- Server already supports multiple devices — just need client UI

### Auto-update
- `version.json` in repo tracks current versions for all components
- Both apps check on startup, show update banner if newer version available
- Android: download APK and trigger package installer
- Desktop: show notification with link to installer

### Windows client
- Cross-platform refactor: extract shared core, add Windows platform layer
- 8-phase plan: core extraction -> platform abstraction -> UI reorganization -> Windows implementation
- Detailed roadmap: [ROADMAP-windows-client.md](ROADMAP-windows-client.md)

## Larger features

### Notification mirroring with actions
- Beyond just showing notifications — allow dismissing or replying from desktop
- Requires `NotificationListenerService` with action support
- Complex: need to serialize and execute `PendingIntent` actions remotely

### SMS from desktop
- Read SMS on desktop, reply from desktop
- Android: SMS content provider + `BroadcastReceiver` for incoming
- Desktop: conversation UI in GTK4
- Privacy/permission heavy

## Known issues to investigate

### Android process crash
- Observed in `dumpsys batterystats` on 2026-04-17: `Proc com.desktopconnector: 6 starts, 1 crashes`
- Session included many APK reinstalls, find-phone tests, and ~10h on battery
- No ANR recorded, no user-visible error reported — crash may have been during an APK self-replace
- Next step: capture `logcat --buffer=crash` or a tombstone after reproducing, check whether `PollService`/`FcmService` survives APK upgrade cleanly

