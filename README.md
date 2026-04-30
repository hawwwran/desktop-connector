<p align="center">
  <img src="docs/assets/banner.png" alt="Desktop Connector" width="600"/>
</p>

# Desktop Connector

End-to-end encrypted file and clipboard sharing between your Android phone and Linux desktop.

The relay server never sees your data — all content is encrypted on-device before it leaves, using X25519 key exchange and AES-256-GCM.

<p align="center">
  <b>Android</b><br/>
  <img src="images/android-app-dark.jpg" alt="Android app - Dark" width="200"/>
  &nbsp;&nbsp;
  <img src="images/android-app-light.jpg" alt="Android app - Light" width="200"/>
</p>

<p align="center">
  <b>Desktop</b><br/>
  <img src="images/desktop-app-menu.png" alt="Tray Menu" width="160"/>
  &nbsp;&nbsp;
  <img src="images/desktop-app-send-to-phone.png" alt="Send files to" width="280"/>
  &nbsp;&nbsp;
  <img src="images/desktop-app-history.png" alt="Transfer History" width="280"/>
  &nbsp;&nbsp;
  <img src="images/find-my-phone.png" alt="Find my device" width="280"/>
</p>

## Features

- **Send files** both ways (between any pair of paired devices)
- **Multi-device** — pair as many phones / tablets / desktops as you want; pickers select the target device per send / per history view / per locate session
- **Clipboard sharing** — send text or images between devices, pushed directly to system clipboard
- **Share intent** — share any file from any Android app to your desktop
- **Recent files** strip on Android for quick sending
- **Drag and drop** on desktop to send files
- **Right-click "Send to <device>"** — file manager integration (Nautilus, Nemo, Dolphin), one entry per paired device
- **Smart link detection** — shared URLs show a link icon, tap to open or copy
- **Transfer history** with delivery status (Sent / Delivered / Received), swipe to delete
- **Near-instant delivery** — long polling with ~1s latency, graceful fallback to regular polling
- **Offline resilient** — exponential backoff, queues transfers, catches up on reconnect
- **Zero battery drain** — Android does not poll when screen is off, polls immediately on wake
- **Unpair syncs both sides** — unpair from either device, the other side reacts automatically
- **APK install** — send APK to phone, tap to install with permission handling
- **Self-hosted relay** — config-less PHP server, just upload to any PHP 8.0+ hosting and it works
- **One-command install** — idempotent installer with dependency checking

## Install (Linux Desktop)

A single signed AppImage. No system Python, no apt packages, no setup.

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
```

The installer fetches the latest release from GitHub, GPG-verifies the signature against the public key shipped in this repo, places the AppImage at `~/.local/share/desktop-connector/`, and runs it once. A welcome dialog asks for your relay server URL.

**Prefer to download manually?** Grab the latest `desktop-connector-*-x86_64.AppImage` from [Releases](../../releases), `chmod +x`, double-click. Verify with `gpg --verify` against [`docs/release/desktop-signing.pub.asc`](docs/release/desktop-signing.pub.asc) (fingerprint `FBEFCEC1 3D7A EC08 1081 2975 491C 9043 90F4 E03B`).

Future updates land via the in-app updater — tray menu → "Check for updates" pulls only the changed blocks (~few hundred KB), no full re-download.

To uninstall:
```bash
~/.local/share/desktop-connector/uninstall.sh
```

**For contributors / dev work**, `install-from-source.sh` in this repo installs the source tree via apt + pip instead of the AppImage. See [`desktop/README.md`](desktop/README.md).

## Install (Android)

Download the APK from [Releases](../../releases) and install it on your phone. The app registers as a "Share to" target — send files from any app (gallery, file manager, browser) directly to your desktop.

## Setup

1. Start the desktop app — it will show a QR code
2. Open the Android app — scan the QR code
3. Verify the pairing code matches on both screens
4. Done — start sending files and clipboard

## Server

The relay server is a config-less PHP app — no configuration files, no database setup, no management needed. Just upload and it works. It stores only encrypted blobs and device IDs. It never sees your files or clipboard content.

### Self-hosting

Upload the `server/` directory to any PHP 8.0+ hosting with SQLite support. No configuration needed — the database and storage directories are created automatically on first request. See [CLAUDE.md](CLAUDE.md) for deployment details.

### Local development

```bash
php -S 0.0.0.0:4441 -t server/public/
```

## How it works

```
[Android Phone]                [PHP Relay Server]              [Linux Desktop]
     |                               |                              |
     |  -- encrypted upload -->      |                              |
     |                               |  <-- poll for pending ---    |
     |                               |  --- encrypted download -->  |
     |                               |                              |
     |  X25519 keypair               |  Sees only device IDs       |  X25519 keypair
     |  AES-256-GCM encrypt          |  + encrypted blobs          |  AES-256-GCM decrypt
```

- **Pairing**: Desktop shows QR code with its public key. Phone scans it. Both derive a shared secret via X25519 + HKDF. Verification code confirms the keys match.
- **Transfers**: Files are chunked (2MB), encrypted with AES-256-GCM, uploaded to the relay. The other side polls, downloads, decrypts.
- **Clipboard**: Uses the `.fn.clipboard.text` / `.fn.clipboard.image` naming convention to signal the receiver to push content to the system clipboard instead of saving a file.
- **Delivery tracking**: Server tracks download status. Sender polls to update "Sent" to "Delivered".

## Security

| What the server sees | What the server does NOT see |
|---|---|
| Device IDs (public key fingerprints) | File contents |
| Which devices are paired | Filenames, sizes, types |
| Approximate file size (chunk count) | Clipboard content |
| Timing of transfers | Encryption keys |

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for planned features.

## License

MIT
