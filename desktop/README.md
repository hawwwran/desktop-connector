# Desktop Connector — Desktop

Linux desktop client for [Desktop Connector](../README.md). System tray app with GTK4/libadwaita windows for sending files, viewing history, and managing settings.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
```

The installer is idempotent — safe to run multiple times for install, update, or repair. It handles system packages (apt), Python packages (pip), app menu entry, autostart, and file manager integration.

Then run `desktop-connector` or find it in your app menu.

To uninstall:
```bash
~/.local/share/desktop-connector/uninstall.sh
```

## Usage

```bash
desktop-connector                      # tray mode (default)
desktop-connector --headless           # headless receiver (no tray icon)
desktop-connector --send="/path/file"  # send a file and exit
desktop-connector --pair               # pairing flow
```

GTK4 windows run as separate subprocesses (to avoid GTK3/4 conflict with pystray):

```bash
python3 -m src.windows send-files --config-dir=~/.config/desktop-connector
python3 -m src.windows settings --config-dir=~/.config/desktop-connector
python3 -m src.windows history --config-dir=~/.config/desktop-connector
```

## Features

- System tray icon with status indicator (green/white = both online, green/yellow = phone offline)
- Drag and drop files onto the tray to send
- Right-click "Send to Phone" in file managers (Nautilus, Nemo, Dolphin)
- Clipboard sharing (text and images)
- Transfer history with delivery status
- Long polling for near-instant delivery (~1s latency)
- Startup dependency checker with one-click install

## Dependencies

- Python 3, python3-tk
- pystray, qrcode, PyNaCl, cryptography, requests
- GTK4, libadwaita (for windows)
- wl-copy/xclip (clipboard)
- notify-send (notifications)

## Config

Stored in `~/.config/desktop-connector/`:
- `config.json` — server URL, device token, paired devices
- `keys/` — X25519 keypair
- `history.json` — transfer history (last 50 items)
