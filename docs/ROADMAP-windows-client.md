# Windows Client Roadmap

Cross-platform refactor of the desktop client: extract shared core, add Windows platform layer.

## Status (post refactor-10)

Refactor-10 prepares the boundary but does **not** implement Windows behavior. The
contract is in place so Windows work can be attached without reopening core
architecture.

### Architecture-ready

- First-class desktop platform contract (`DesktopPlatform`) in `desktop/src/platform/contract/`.
- Explicit capability flags (`PlatformCapabilities`) consumed by `poller.py` (auto-open URLs) and `tray.py` (Send Clipboard, Open Save Folder visibility).
- Centralized composition (`desktop/src/platform/compose.py`) with a clean `NotImplementedError` on non-Linux hosts.
- Linux platform wired as `compose_linux_platform()` producing a `DesktopPlatform` with the existing Linux backends.
- Core runtime modules (`startup_context`, `receiver_runner`, `poller`, `tray`, `dependency_check`) consume the platform contract instead of ad-hoc Linux backends.
- The contract itself (`platform.contract`) imports nothing platform-specific — `from ..platform import DesktopPlatform` does not drag in Linux backends.

### Partially-ready (contract exists, only Linux implemented)

- Clipboard (text + image)
- Notifications
- Dialogs (file picker, confirm, info)
- Shell (open URL, open folder, launch installer terminal)

### Unresolved implementation areas for Windows

1. Windows backends for clipboard / notifications / dialogs / shell.
2. Windows tray lifecycle (no GTK3/4 conflict; pystray's Win32 backend should work in-process).
3. Windows file-manager integration (Send To shortcut or shell extension).
4. Windows installer / update path and bootstrap dependency story.
5. Windows-specific dependency checks and install UX.
6. Packaging / distribution / signing decisions.

### Validation expectations before Windows implementation starts

- Linux behavior remains unchanged for pairing, transfer, tray receive, headless receive, and clipboard flows.
- `PlatformCapabilities` is the default branching mechanism — new code should check `self.platform.capabilities.*`, not `sys.platform`.
- New Windows code lands as a platform implementation, not as core branching.

---

## Implementation roadmap (Windows)

Phases 1–3 of the original plan (extract core, define platform layer, reorganize UI) are already delivered by refactors 5, 6, and 10. What follows is the still-actionable phase 4+ work.

### Phase 4 — Windows platform backends

Goal: implement Windows equivalents for clipboard, notifications, file operations.

1. New `desktop/src/backends/windows/`:
   - **Clipboard**: `win32clipboard` (from `pywin32`) for text and images.
   - **Notifications**: `winotify` for Windows toast notifications (supports action buttons, app icon).
   - **Shell.open_url / open_folder**: `os.startfile(path)`.
   - **Dialogs.pick_files**: `tkinter.filedialog.askopenfilenames()` (bundled with Python).
   - Helper: Downloads dir via `Path.home() / "Downloads"` or registry via `winreg`.
2. New `desktop/src/platform/windows/`:
   - `compose.py` → `compose_windows_platform()` returning a `DesktopPlatform(name="windows", …)`.
3. Update `desktop/src/platform/compose.py` to branch: Linux → `compose_linux_platform()`, Windows → `compose_windows_platform()`, else raise.
4. Set `PlatformCapabilities` for Windows appropriately (e.g. `file_manager_integration=False` until phase 7 lands).
5. Smoke-test each backend function independently on a Windows host.

Dependencies to add (Windows only): `pywin32`, `winotify`.

### Phase 5 — Windows UI

Goal: build the tray and windows UI for Windows.

1. `desktop/src/ui/windows/tray.py`:
   - pystray with Win32 backend (no GTK conflict, no subprocess needed).
   - Same menu structure as Linux: Send Clipboard, Send Files, History, Settings, Quit.
   - Icon: same donut-ring design (PIL-generated, works cross-platform).
   - Windows can launch GUI windows in-process (no GTK3/4 conflict).
2. `desktop/src/ui/windows/`:
   - Tkinter-based windows for: send files, settings, history, pairing.
   - Alternative: PyQt6 for a more polished look (heavier dependency). Start with tkinter (zero extra dependencies), upgrade later if needed.
3. Pairing GUI may be reusable from the Linux version (already tkinter) with minor styling adjustments.

### Phase 6 — Windows entry point and installer

Goal: make it launchable and installable on Windows.

1. Update `main.py` for platform-aware paths:
   - Config directory: `%APPDATA%/desktop-connector/`.
   - Platform-aware dependency checking (skip `apt`, check pip packages).
2. `desktop/install.ps1`:
   - Check Python 3.10+ is installed.
   - `pip install` required packages (pystray, qrcode, PyNaCl, cryptography, requests, pywin32, winotify).
   - Copy app to `%LOCALAPPDATA%/desktop-connector/`.
   - Create Start Menu shortcut.
   - Add to startup (`HKCU\...\Run` or Startup folder).
   - Create SendTo shortcut for right-click `Send to <device>`.
3. Optional: PyInstaller single-exe distribution for users without Python.
   - Watch for hidden-import issues (PyNaCl, cryptography).
   - Consider Inno Setup or NSIS for a proper installer GUI.
4. `desktop/uninstall.ps1`:
   - Remove app, Start Menu entry, startup entry, SendTo shortcut.
   - Optionally preserve config in `%APPDATA%`.

### Phase 7 — Windows file manager integration

Goal: right-click `Send to <device>` in Explorer.

- **Simple**: shortcut in `shell:sendto` (`%APPDATA%/Microsoft/Windows/SendTo/`).
  - Points to `desktop-connector.exe --send="%1"` (or a small `.bat` wrapper).
  - Shows up in right-click -> Send to -> `Send to <device>`.
  - Zero code; just a shortcut file created by the installer.
- **Advanced** (later): shell extension via `IContextMenu` for a top-level right-click entry.
  - Requires a COM DLL (C++ or C#) — significantly more work. Not worth it for v1.

When phase 7 lands, flip `PlatformCapabilities.file_manager_integration` to `True` for the Windows platform.

### Phase 8 — Testing and polish

1. Cross-platform integration test:
   - Adapt `test_loop.sh` to also run on Windows (or write `test_loop.ps1`).
   - Test: register, pair, encrypt, upload, download, decrypt, verify.
2. Test matrix:
   - Windows 10 + Windows 11.
   - Send file: desktop → Android, Android → desktop.
   - Send clipboard: both directions.
   - Pair / unpair from both sides.
   - Startup on boot.
   - SendTo context menu.
3. Edge cases:
   - Long file paths (Windows 260-char limit unless long paths enabled).
   - Unicode filenames.
   - Large files (chunked transfer).
   - Clipboard with rich content (HTML, images).

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

- **Crypto interop is already proven**: PyNaCl and cryptography are cross-platform. No risk here.
- **pystray on Windows**: well-supported, native Win32 API. Lower risk than Linux (no GTK3 conflict).
- **tkinter on Windows**: bundled with standard Python on Windows. Functional but not pretty. Acceptable for v1.
- **PyInstaller bundling**: can be finicky with hidden imports (PyNaCl, cryptography). May need `--hidden-import` flags. Test early.
- **Windows Defender / SmartScreen**: unsigned `.exe` from PyInstaller may trigger warnings. Consider code signing, or distribute as a pip-installable package first.

## What NOT to change

- Server: platform-agnostic, no changes needed.
- Android: no changes needed.
- Encryption protocol: identical on all platforms.
- API protocol: identical on all platforms.
- Config file format: same JSON; only the directory path differs per OS.
