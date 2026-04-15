# Windows Client Roadmap

Cross-platform refactor of the desktop client (Option B): extract shared core, add Windows platform layer.

## Current state

All desktop code lives in `desktop/src/*.py` as a flat module structure, with Linux-specific calls scattered throughout. The dependency graph is clean — platform-specific modules (`clipboard`, `notifications`, `dialogs`, `windows`) are leaf nodes that don't import each other.

## Target directory structure

```
desktop/
  src/
    __init__.py
    __main__.py
    main.py                    # platform-aware entry point

    core/                      # platform-agnostic (no OS-specific calls)
      __init__.py
      api_client.py            # as-is (depends on connection, crypto)
      config.py                # replace os.uname().nodename with platform.node()
      connection.py            # as-is
      crypto.py                # as-is
      history.py               # data layer only (strip GTK4 window code out)
      pairing.py               # protocol logic only (strip tkinter GUI out)
      poller.py                # strip xdg-open call, use platform.open_file()

    platform/                  # OS abstraction interface
      __init__.py              # detect OS, export current platform
      base.py                  # abstract base: clipboard, notifications, dialogs, open_file
      linux.py                 # current Linux implementations
      windows.py               # new Windows implementations

    ui/                        # UI layer, per platform
      __init__.py
      linux/
        __init__.py
        tray.py                # current pystray + GTK4 subprocess pattern
        windows_gtk.py         # current GTK4/libadwaita windows (renamed from windows.py)
        dialogs.py             # zenity + tkinter fallback
        pairing_gui.py         # tkinter pairing window
      windows/
        __init__.py
        tray.py                # pystray (native Win32, no GTK conflict)
        windows_tk.py          # tkinter or PyQt windows
        dialogs.py             # tkinter file dialogs
        pairing_gui.py         # tkinter pairing window (likely reusable from Linux)

  install.sh                   # Linux installer (unchanged)
  install.ps1                  # new: Windows installer (PowerShell)
  uninstall.sh                 # Linux (unchanged)
  uninstall.ps1                # new: Windows uninstaller
```

## Phases

### Phase 1 — Extract core (Linux-only, no behavior change)

Goal: Move platform-agnostic code into `core/`, verify nothing breaks on Linux.

1. Create `desktop/src/core/` package
2. Move these files as-is (they have zero platform-specific code):
   - `crypto.py` -> `core/crypto.py`
   - `connection.py` -> `core/connection.py`
   - `api_client.py` -> `core/api_client.py`
3. Move with minor edits:
   - `config.py` -> `core/config.py`
     - Replace `os.uname().nodename` with `platform.node()`
     - Change config dir: `~/.config/desktop-connector/` on Linux, `%APPDATA%/desktop-connector/` on Windows
   - `history.py` -> `core/history.py`
     - Extract `show_history_window()` (GTK4) out to `ui/linux/`
     - Keep only the `TransferHistory` data class
   - `pairing.py` -> `core/pairing.py`
     - Extract `run_pairing_gui()` (tkinter) out to `ui/`
     - Keep only the pairing protocol logic
   - `poller.py` -> `core/poller.py`
     - Replace direct `xdg-open` call with `platform.open_file(path)`
     - Replace direct clipboard/notification calls with platform interface calls
4. Update all imports across remaining files
5. Verify: `python3 -m src.main` still works identically on Linux
6. Run `test_loop.sh` to confirm end-to-end still passes

### Phase 2 — Platform abstraction layer

Goal: Define the OS abstraction interface and wrap current Linux code behind it.

1. Create `desktop/src/platform/base.py` — abstract base class:
   ```python
   class PlatformBase(ABC):
       @abstractmethod
       def copy_text_to_clipboard(self, text: str) -> bool: ...
       @abstractmethod
       def copy_image_to_clipboard(self, path: str) -> bool: ...
       @abstractmethod
       def read_clipboard_text(self) -> str | None: ...
       @abstractmethod
       def read_clipboard_image(self) -> bytes | None: ...
       @abstractmethod
       def send_notification(self, title: str, body: str, icon: str = None) -> None: ...
       @abstractmethod
       def open_file(self, path: str) -> None: ...
       @abstractmethod
       def open_folder(self, path: str) -> None: ...
       @abstractmethod
       def pick_files(self) -> list[str]: ...
       @abstractmethod
       def get_downloads_dir(self) -> Path: ...
   ```
2. Create `desktop/src/platform/linux.py` — implement using current `clipboard.py`, `notifications.py`, `dialogs.py` code
3. Create `desktop/src/platform/__init__.py` — detect OS and export singleton:
   ```python
   import sys
   if sys.platform == 'linux':
       from .linux import LinuxPlatform as Platform
   elif sys.platform == 'win32':
       from .windows import WindowsPlatform as Platform
   platform = Platform()
   ```
4. Update `core/poller.py` to use `platform.copy_text_to_clipboard()` etc. instead of direct `clipboard.py` imports
5. Verify Linux still works identically

### Phase 3 — Reorganize UI layer

Goal: Move all GUI code into `ui/linux/`, keeping it working.

1. Create `desktop/src/ui/linux/` package
2. Move:
   - `tray.py` -> `ui/linux/tray.py`
   - `windows.py` -> `ui/linux/windows_gtk.py`
   - GTK4 `show_history_window()` from history.py -> `ui/linux/windows_gtk.py` (or its own file)
   - `run_pairing_gui()` from pairing.py -> `ui/linux/pairing_gui.py`
   - `dialogs.py` -> `ui/linux/dialogs.py` (also used by platform layer)
3. Update `main.py` to import UI from `ui.linux`
4. Verify Linux still works, run `test_loop.sh`

At this point the refactor is complete. Linux works exactly as before, code is cleanly separated, and the Windows slots are empty but defined.

### Phase 4 — Windows platform layer

Goal: Implement Windows equivalents for clipboard, notifications, file operations.

1. `desktop/src/platform/windows.py`:
   - **Clipboard**: `win32clipboard` (from pywin32) for text and images
   - **Notifications**: `winotify` for Windows toast notifications (supports action buttons, app icon)
   - **open_file/open_folder**: `os.startfile(path)`
   - **pick_files**: `tkinter.filedialog.askopenfilenames()` (bundled with Python)
   - **get_downloads_dir**: `Path.home() / "Downloads"` or read from registry via `winreg`
2. Test each function independently on Windows

Dependencies to add (Windows only): `pywin32`, `winotify`

### Phase 5 — Windows UI

Goal: Build the tray and window UI for Windows.

1. `desktop/src/ui/windows/tray.py`:
   - pystray with Win32 backend (no GTK conflict, no subprocess needed)
   - Same menu structure as Linux: Send Clipboard, Send Files, History, Settings, Quit
   - Icon: same donut-ring design (PIL-generated, works cross-platform)
   - Key difference: can launch windows in-process (no GTK3/4 conflict on Windows)
2. `desktop/src/ui/windows/windows_tk.py`:
   - Tkinter-based windows for: send files, settings, history, pairing
   - Alternatively, PyQt6 for a more polished look (heavier dependency)
   - Start with tkinter (zero extra dependencies), upgrade later if needed
3. `desktop/src/ui/windows/pairing_gui.py`:
   - Likely reusable from Linux version (already tkinter)
   - May need minor path/styling adjustments

### Phase 6 — Windows entry point and installer

Goal: Make it launchable and installable on Windows.

1. Update `main.py`:
   - Platform-aware dependency checking (skip apt, check pip packages)
   - Platform-aware UI imports (`ui.linux` vs `ui.windows`)
   - Windows config directory (`%APPDATA%/desktop-connector/`)
2. `desktop/install.ps1` (PowerShell installer):
   - Check Python 3.10+ is installed
   - `pip install` required packages (pystray, qrcode, PyNaCl, cryptography, requests, pywin32, winotify)
   - Copy app to `%LOCALAPPDATA%/desktop-connector/`
   - Create Start Menu shortcut
   - Add to startup (registry `HKCU\...\Run` or Start Menu Startup folder)
   - Create SendTo shortcut for right-click "Send to Phone"
3. PyInstaller config for single-exe distribution (optional, for users without Python):
   - `desktop-connector.spec` for PyInstaller
   - Bundle into `desktop-connector.exe`
   - Consider Inno Setup or NSIS for a proper installer GUI
4. `desktop/uninstall.ps1`:
   - Remove app directory, Start Menu entry, startup entry, SendTo shortcut
   - Optionally preserve config in `%APPDATA%`

### Phase 7 — Windows file manager integration

Goal: Right-click "Send to Phone" in Explorer.

- **Simple**: Shortcut in `shell:sendto` (`%APPDATA%/Microsoft/Windows/SendTo/`)
  - Points to `desktop-connector.exe --send="%1"` (or a small .bat wrapper)
  - Shows up in right-click > Send to > "Send to Phone"
  - Zero code, just a shortcut file created by the installer
- **Advanced** (later): Windows shell extension via `IContextMenu` for a top-level right-click entry
  - Requires a COM DLL (C++ or C#) — significantly more work
  - Not worth it for v1

### Phase 8 — Testing and polish

1. Cross-platform integration test:
   - Adapt `test_loop.sh` to also run on Windows (or write `test_loop.ps1`)
   - Test: register, pair, encrypt, upload, download, decrypt, verify
2. Test matrix:
   - Windows 10 + Windows 11
   - Send file: desktop -> Android, Android -> desktop
   - Send clipboard: both directions
   - Pair/unpair from both sides
   - Startup on boot
   - SendTo context menu
3. Edge cases:
   - Long file paths (Windows 260-char limit unless long paths enabled)
   - Unicode filenames
   - Large files (chunked transfer)
   - Clipboard with rich content (HTML, images)

## Dependency summary

| Package | Linux | Windows | Purpose |
|---------|-------|---------|---------|
| pystray | yes | yes | System tray |
| qrcode | yes | yes | QR code generation |
| PyNaCl | yes | yes | X25519 key exchange |
| cryptography | yes | yes | AES-256-GCM, HKDF |
| requests | yes | yes | HTTP client |
| Pillow | yes | yes | Image handling |
| python3-gi (GTK4) | yes | no | Linux UI |
| libadwaita | yes | no | Linux UI |
| pywin32 | no | yes | Windows clipboard |
| winotify | no | yes | Windows notifications |

## Risk areas

- **Crypto interop is already proven**: PyNaCl and cryptography are cross-platform, same bytes in/out. No risk here.
- **pystray on Windows**: Well-supported, uses native Win32 API. Lower risk than Linux (no GTK3 conflict).
- **tkinter on Windows**: Bundled with standard Python on Windows. Functional but not pretty. Acceptable for v1.
- **PyInstaller bundling**: Can be finicky with hidden imports (PyNaCl, cryptography). May need `--hidden-import` flags. Test early.
- **Windows Defender**: Unsigned .exe from PyInstaller may trigger SmartScreen. Consider code signing for distribution, or distribute as a pip-installable package first.

## What NOT to change

- Server: completely platform-agnostic, no changes needed
- Android: no changes needed
- Encryption protocol: identical on all platforms
- API protocol: identical on all platforms
- Config file format: same JSON structure (only the directory path differs)
