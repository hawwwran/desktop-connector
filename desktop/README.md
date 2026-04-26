# Desktop Connector — Desktop

Linux desktop client for [Desktop Connector](../README.md). System tray app with GTK4/libadwaita windows for sending files, viewing history, pairing, and finding your phone.

## Install

The released form is a signed AppImage — single self-contained file, no apt or pip touched on the host.

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
```

Or download the AppImage manually from [Releases](../../releases), `chmod +x`, run it. The first-launch onboarding dialog asks for your relay URL; after that the app drops a `.desktop` menu entry, autostart entry, and Nautilus/Nemo/Dolphin "Send to Phone" scripts on your behalf.

In-app updates land via the tray's "Check for updates" item or automatically once a day in the background — both pull only the binary delta (~few hundred KB), not the whole AppImage.

To uninstall:
```bash
~/.local/share/desktop-connector/uninstall.sh
```

### Trust + verification

The AppImage and `SHA256SUMS` are signed with the project's release key:

| Field | Value |
|---|---|
| Identity | `Desktop Connector Releases <github@hawwwran.com>` |
| Fingerprint | `FBEFCEC1 3D7A EC08 1081 2975 491C 9043 90F4 E03B` |
| Public key | [`docs/release/desktop-signing.pub.asc`](../docs/release/desktop-signing.pub.asc) |
| Recovery runbook | [`docs/release/desktop-signing-recovery.md`](../docs/release/desktop-signing-recovery.md) |

The installer verifies the fingerprint and signature before placing the AppImage; refuses to proceed on mismatch. Manual verification:

```bash
gpg --import docs/release/desktop-signing.pub.asc
gpg --verify desktop-connector-X.Y.Z-x86_64.AppImage.sig \
            desktop-connector-X.Y.Z-x86_64.AppImage
gpg --verify SHA256SUMS.sig SHA256SUMS
sha256sum -c SHA256SUMS
```

## Install from source (contributors / dev work)

If you're hacking on the Python source and want changes to land locally without rebuilding the AppImage every time, use `install-from-source.sh` instead:

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install-from-source.sh | bash
```

This is the **old** install path — copies `src/` into `~/.local/share/desktop-connector/`, `apt install`s the GTK + Python deps, `pip install`s pure-Python deps, drops a `~/.local/bin/desktop-connector` shell wrapper that runs `python3 -m src.main`. Bouncing between this and the AppImage path is safe — `~/.config/desktop-connector/` (config, keys, history, pairings) is shared and preserved either way; system integration files (`.desktop`, autostart, file-manager scripts) are owned by whichever install ran most recently.

## Usage

```bash
desktop-connector                      # tray mode (default)
desktop-connector --headless           # headless receiver (no tray icon)
desktop-connector --send="/path/file"  # send a file and exit
desktop-connector --pair               # pairing flow
```

Inside the AppImage these run via `$APPIMAGE` directly; the install-from-source path runs `python3 -m src.main` through the `~/.local/bin/desktop-connector` wrapper.

GTK4 windows run as separate subprocesses (pystray loads GTK3 internally; mixing GTK 3 + 4 in one Python process breaks the GI loader):

```bash
# Install-from-source dev tree:
python3 -m src.windows {send-files|settings|history|pairing|find-phone} \
    --config-dir=~/.config/desktop-connector

# Inside the AppImage:
$APPIMAGE --gtk-window={send-files|settings|history|pairing|find-phone} \
    --config-dir=~/.config/desktop-connector
```

## Features

- System tray icon with status indicator (sparkle star: filled blue = both online, sky-blue = phone offline, yellow = reconnecting/uploading, orange = disconnected)
- Drag-and-drop files onto the Send Files window
- Right-click "Send to Phone" in file managers (Nautilus, Nemo, Dolphin)
- Clipboard sharing (text and images)
- Configurable receive actions for URLs, text, images, videos, and documents
- Transfer history with delivery state (Sent / Delivered / Received), swipe-to-delete
- Long polling for near-instant delivery (~1 s latency)
- In-app updater (AppImage installs only)

### Receive actions

Settings includes a **Receive Actions** section for choosing what happens
after content arrives on the desktop.

Defaults preserve existing behavior where practical:

- URL: open in the default browser
- Text: copy to clipboard
- Image, video, document: no action beyond saving and history/notification updates

For text that contains a URL plus other text, the URL action runs for the
detected URL and the Text action runs for the full text. Clipboard updates
from receive actions are applied once after actions are evaluated, so a
single received item does not make repeated clipboard writes.

## AppImage build (for maintainers / CI)

The build script lives at [`packaging/appimage/build-appimage.sh`](packaging/appimage/build-appimage.sh). Locally:

```bash
./desktop/packaging/appimage/build-appimage.sh \
    --source=$PWD --output=/tmp/dc-out
```

Releases are published by [`.github/workflows/desktop-release.yml`](../.github/workflows/desktop-release.yml) on `desktop/v*` tag pushes. See [`docs/plans/desktop-appimage-packaging-plan.md`](../docs/plans/desktop-appimage-packaging-plan.md) for the full packaging story.

## Config

Stored in `~/.config/desktop-connector/`:
- `config.json` — server URL, device ID, auth token, paired devices
- `keys/` — X25519 keypair
- `history.json` — transfer history (last 50 items)
- `logs/` — opt-in debug logs (toggle in Settings)

Cache lives at `~/.cache/desktop-connector/` (update-check metadata, tray icon snapshots).
