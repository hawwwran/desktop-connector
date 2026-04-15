# Desktop Connector — Roadmap

Potential features organized by effort. All use the existing `.fn.` transfer convention unless noted.

## Quick wins

### ~~Send URL~~ (Done)
- Implemented via smart link detection in clipboard transfers
- Share from Chrome/YouTube sends URL as clipboard text
- Both apps detect single URLs, show link icon, Open/Copy dialog
- Desktop auto-opens links by default (configurable in Settings)

### ~~Right-click "Send to Phone"~~ (Done)
- Nautilus script installed to `~/.local/share/nautilus/scripts/Send to Phone`
- Right-click any file(s) → Scripts → Send to Phone
- Calls `desktop-connector --headless --send` for each file
- Installed/removed automatically by install.sh/uninstall.sh

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
