<p align="center">
  <img src="docs/assets/banner.png" alt="Desktop Connector" width="600"/>
</p>

# Desktop Connector

Self-hosted, end-to-end encrypted file and clipboard sharing for Android devices and Linux desktops.

Desktop Connector pairs your devices through a PHP relay you control. The relay routes encrypted blobs and tracks delivery state, but file and clipboard contents are encrypted on-device before upload, using X25519 + HKDF and AES-256-GCM.

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

## Who this is for

Desktop Connector is a good fit if you:

- use Android and Linux and want one workflow for files, clipboard text, clipboard images, and share intents;
- want a relay you can self-host on simple PHP hosting instead of depending on a hosted account;
- care about end-to-end encryption but still want practical desktop integration: tray status, drag and drop, history, right-click send targets, and Find my Device;
- pair more than one connected device and want explicit target selection.

## Why this exists

The project is built around a specific tradeoff: use a small blind relay for reachability, but keep trust and content confidentiality on the paired devices. That makes it work across networks without requiring devices to discover each other locally, while keeping the relay out of plaintext files, clipboard data, and location payloads.

The desktop client is not just a transfer script. It is meant to feel native on Linux: signed AppImage install, tray integration, GTK windows, file-manager send targets, and per-device history.

## How it differs

This is not meant to replace every nearby tool. It has a narrower fit: Android/Linux sharing through a relay you control, with desktop workflow integration.

| Project | Best fit | Connection model | Main distinction |
|---|---|---|---|
| [KDE Connect](https://kdeconnect.kde.org/) | Broad device integration: files, links, notifications, media control, commands, remote input, and device-finding features. | Device pairing and discovery across local network, Bluetooth, or manually configured/VPN paths. | Desktop Connector is narrower, but focuses on self-hosted relay reachability, encrypted transfer/clipboard workflows, and explicit per-device send/history targets. |
| [LocalSend](https://localsend.org/) | Cross-platform file and text sharing when devices are nearby. | Local network/offline transfer; no account, login, internet, or server required. | Desktop Connector uses a relay so paired devices do not need to be on the same LAN, and keeps Linux desktop integration as a first-class workflow. |
| Desktop Connector | Android/Linux file, clipboard, share-intent, history, and Find my Device workflows with a relay you operate. | User-controlled PHP relay stores encrypted blobs and routing metadata; paired devices hold the content keys. | Optimized for self-hosting, relay-based E2E encryption, signed Linux AppImage install, and per-device desktop send targets. |

## Current status

Desktop Connector is usable for Android and Linux desktop workflows now. The repo currently ships:

- Android APK releases for sideloading.
- A signed Linux AppImage with installer and in-app updater.
- A config-less PHP 8.0+ relay using SQLite.
- Multi-device pairing across Android devices and desktops.
- End-to-end encrypted file transfer, clipboard text, clipboard image handling, share intents, history, and Find my Device.

The project is still evolving; release versions are tracked in [`version.json`](version.json), and detailed protocol behavior is tracked in [`docs/protocol/protocol.md`](docs/protocol/protocol.md).

## Tradeoffs

- Desktop support is Linux-focused. Windows is planned separately.
- Android install is APK sideloading from GitHub Releases, not Play Store distribution.
- The PHP relay is optimized for simple self-hosting and personal or small deployments, not managed multi-tenant hosting.
- The relay cannot read contents, but it still handles routing metadata such as device IDs, pairing relationships, timing, and approximate transfer size.
- AppImage releases target modern Linux distributions; older distros can use the source install path.

## Features

**Transfer and clipboard**

- Send files both ways between any pair of paired devices.
- Send clipboard text between devices.
- Send clipboard images as image transfers with normal history thumbnails.
- Share any file from any Android app to your desktop.
- Send APKs to an Android device, then tap to install with permission handling.
- Detect shared URLs and show a link action for open/copy.

**Multi-device workflow**

- Pair multiple Android devices and desktops.
- Pick the target device per send, per history view, and per Find my Device session.
- Keep transfer history with delivery status: Sent, Delivered, Received.
- Unpair from either side and sync the removal to the other side.

**Linux desktop integration**

- Use the tray app for status, send, history, settings, pairing, and Find my Device.
- Drag and drop files onto the desktop send window.
- Use Nautilus, Nemo, or Dolphin right-click send targets, with one `Send to <device>` entry per paired device.

**Delivery and reliability**

- Long polling gives near-instant delivery, with graceful fallback to regular polling.
- Exponential backoff handles offline periods and catches up on reconnect.
- Android avoids polling while the screen is off and wakes immediately on screen-on or FCM wake where available.

**Self-hosting**

- Run a config-less PHP relay on PHP 8.0+ hosting with SQLite.
- Store encrypted blobs and routing metadata only; content keys stay on paired devices.
- Install the Linux desktop app with one signed AppImage installer command.

## Quick install paths

| Component | Path | Notes |
|---|---|---|
| Linux desktop | <code>curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh &#124; bash</code> | Installs the signed AppImage, verifies the release signature, and starts first-launch relay setup. |
| Android | [GitHub Releases](../../releases) | Download and sideload the APK. |
| Relay server | `server/` directory | Upload to PHP 8.0+ hosting with SQLite and URL rewriting. |
| Desktop development | [`desktop/README.md`](desktop/README.md) | Use the source install path when hacking on the Python desktop client or supporting older distros. |

## Install (Linux Desktop)

A single signed AppImage. No system Python, no apt packages, no setup.

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
```

The installer fetches the latest release from GitHub, GPG-verifies the signature against the public key shipped in this repo, places the AppImage at `~/.local/share/desktop-connector/`, and runs it once. A welcome dialog asks for your relay server URL.

**Prefer to download manually?** Grab the latest `desktop-connector-*-x86_64.AppImage` from [Releases](../../releases), `chmod +x`, double-click. Verify with `gpg --verify` against [`docs/release/desktop-signing.pub.asc`](docs/release/desktop-signing.pub.asc) (fingerprint `FBEF CEC1 3D7A EC08 1081 2975 491C 9043 90F4 E03B`).

Future updates land via the in-app updater — tray menu → "Check for updates" pulls only the changed blocks (~few hundred KB), no full re-download.

To uninstall:
```bash
~/.local/share/desktop-connector/uninstall.sh
```

**For contributors / dev work**, `install-from-source.sh` in this repo installs the source tree via apt + pip instead of the AppImage. See [`desktop/README.md`](desktop/README.md).

## Install (Android)

Download the APK from [Releases](../../releases) and install it on your Android device. The app registers as a "Share to" target — send files from any app (gallery, file manager, browser) directly to your desktop.

## Setup

To pair an Android device:

1. Start the desktop app — it will show a QR code
2. Open the Android app and scan the QR code
3. Verify the pairing code matches on both screens
4. Done — start sending files and clipboard

To pair two desktops, open the pairing window on both machines and use the
"Pair desktop" mode to exchange a pairing key.

## Server

The relay server is a config-less PHP app — no configuration files, no database setup, no management needed. Just upload and it works. It stores encrypted blobs and routing metadata needed for delivery. It never sees your files or clipboard content.

### Self-hosting

Upload the `server/` directory to any PHP 8.0+ hosting with SQLite support. No configuration needed — the database and storage directories are created automatically on first request. See [`server/README.md`](server/README.md) for server-specific hosting notes.

### Local development

```bash
php -S 0.0.0.0:4441 -t server/public/
```

## Architecture at a glance

Desktop Connector has three runtime pieces:

- **Android app**: Kotlin + Jetpack Compose client for pairing, sending, receiving, share intents, clipboard actions, transfer history, and Find my Device.
- **Linux desktop app**: Python tray app with GTK4/libadwaita subprocess windows for sending, history, settings, pairing, file-manager integration, and Find my Device.
- **PHP relay server**: small SQLite-backed HTTP relay that stores encrypted transfer blobs, routing metadata, pairing requests, delivery state, and optional FCM configuration.

```text
[Connected device]           [PHP relay server]              [Linux desktop]
      |                              |                              |
      | -- encrypted upload ------>  |                              |
      |                              |  <--- poll / wake ---------- |
      |                              |  --- encrypted download ---> |
      |                              |                              |
      | X25519 keypair               | stores ciphertext            | X25519 keypair
      | AES-256-GCM encrypt/decrypt  | + routing metadata           | AES-256-GCM encrypt/decrypt
```

The core flow:

1. Each registered device has a long-lived X25519 key pair and a server auth token.
2. Pairing exchanges public keys by QR code, or by a desktop-to-desktop pairing key, then both sides derive the same pairwise AES-256-GCM key with HKDF-SHA256.
3. File metadata is encrypted before upload. File bytes are split into 2 MiB chunks, encrypted, uploaded to the relay, then downloaded and decrypted by the paired recipient.
4. Clipboard behavior rides on the same encrypted transfer path through `.fn.clipboard.text` and `.fn.clipboard.image` synthetic filenames.
5. Delivery tracking is explicit: the server records recipient download/ack state so senders can move from Sent to Delivered.
6. Lightweight commands, such as Find my Device, use the encrypted fasttrack message path instead of full file transfers.

## Security model in one minute

The relay is a delivery service, not a trusted content processor. It can route encrypted data between paired devices, but it should not be able to read files, clipboard contents, location payloads, filenames, MIME types, or encryption keys.

The important boundary is pairing: once two devices verify the same pairing code, they share a symmetric key that the relay does not store. That key protects transfer metadata, file chunks, clipboard payloads, and fasttrack command payloads.

The relay still handles metadata needed to operate the system. That includes device IDs, pairing relationships, timing, delivery status, and approximate transfer size from encrypted blob sizes and chunk counts.

| What the server sees | What the server does NOT see |
|---|---|
| Device IDs (public key fingerprints) | File contents |
| Which devices are paired | Plaintext filenames, MIME types, content metadata |
| Approximate transfer size (chunk count / encrypted blob sizes) | Clipboard content |
| Timing of transfers and delivery state | Encryption keys |

## Project docs

| Doc | Use it for |
|---|---|
| [`docs/protocol/protocol.md`](docs/protocol/protocol.md) | Wire protocol, cryptographic envelope, state transitions, and compatibility expectations. |
| [`docs/protocol/explain.protocol.md`](docs/protocol/explain.protocol.md) | Why the protocol is documented separately and how to extend it safely. |
| [`docs/protocol.compatibility.md`](docs/protocol.compatibility.md) | Classifying protocol-adjacent changes as preserving, extending, or breaking. |
| [`docs/protocol.examples.md`](docs/protocol.examples.md) | Canonical request/response examples for endpoints and transfer modes. |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Planned and completed feature areas. |
| [`docs/PLANS.md`](docs/PLANS.md) | Current implementation ledgers and living protocol docs. |
| [`docs/diagnostics.events.md`](docs/diagnostics.events.md) | Shared diagnostic event vocabulary across desktop, Android, and server. |
| [`desktop/README.md`](desktop/README.md) | Desktop install, AppImage trust model, usage, and source-install workflow. |
| [`android/README.md`](android/README.md) | Android build, install, and client architecture notes. |
| [`server/README.md`](server/README.md) | Relay requirements, self-hosting layout, local development, and API summary. |
| [`docs/release/desktop-signing-recovery.md`](docs/release/desktop-signing-recovery.md) | Desktop AppImage signing key, verification, and recovery runbook. |
| [`docs/release/android-signing-recovery.md`](docs/release/android-signing-recovery.md) | Android release signing key and recovery runbook. |

## Contributing

Start with [`CONTRIBUTING.md`](CONTRIBUTING.md), then read [`CLAUDE.md`](CLAUDE.md) before changing transfers, pairing, fasttrack messages, logging, release packaging, or platform boundaries.

Good contribution areas include:

- protocol compatibility and documentation updates;
- desktop platform abstraction work, especially Windows-readiness;
- Android reliability and UX polish;
- diagnostics, logging, and troubleshooting improvements;
- self-hosting and release-process polish.

For protocol-visible behavior changes, update [`docs/protocol/protocol.md`](docs/protocol/protocol.md) and the relevant compatibility/examples docs along with the implementation.

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for planned features.

## License

MIT
