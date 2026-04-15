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

## Quick wins

### Find my phone
- `.fn.ring` — phone plays a loud alarm sound for 15 seconds, even on silent
- Desktop tray menu: "Ring Phone"
- Phone: `MediaPlayer` with `AudioManager.STREAM_ALARM` at max volume

### Battery status
- Phone includes battery level + charging state in health check headers
- Server passes it through to the desktop via stats endpoint
- Desktop shows in tray tooltip: "Phone: 73% charging"
- No extra transfers needed — piggybacks on existing polling

## Medium effort

### Notification mirroring
- Android `NotificationListenerService` reads notifications
- Sends as `.fn.notification.{app}` with JSON payload (title, text, icon, app name)
- Desktop shows as native notification via `notify-send`
- Privacy concern: user must explicitly enable in Android settings
- Filter by app (don't forward every notification)

### Auto-sync folder
- Watch a designated folder on both sides for changes
- New/modified files automatically transferred
- Conflict resolution: last-modified wins, or keep both
- Desktop: `inotifywait` or `watchdog` library
- Android: `FileObserver`
- Needs deletion sync too (`.fn.delete.filename`)

### Transfer resume
- Track which chunks were successfully uploaded per transfer
- On retry, skip already-uploaded chunks
- Server already stores individual chunks — just need client-side tracking
- Important for large video files on unstable connections

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

### Remote terminal
- Run desktop shell commands from the phone
- `.fn.exec` with command, desktop runs it and sends `.fn.exec.result` back
- Security: whitelist commands, require confirmation on desktop
- Useful for: restart a service, check disk space, run a script
