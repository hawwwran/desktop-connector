# desktop/packaging/appimage/

Linux AppImage packaging for the desktop client.

**Status:** P.3 complete. `build-appimage.sh` produces a runnable
~111 MB AppImage with bundled Python + pure-Python deps + GTK4 +
libadwaita + WebKitGTK 6.0 + GTK3 + libayatana-appindicator3 (for
pystray's tray backend). All GTK4 subprocess windows (send-files,
settings, history, pairing, find-phone, locate-alert) render with
bundled libs and re-enter the AppImage via `$APPIMAGE`. First launch
drops a `.desktop` menu entry + autostart entry pointing at
`$APPIMAGE`; both rewrite-on-move and respect user deletion (see
`src/bootstrap/appimage_install_hook.py`). Per-paired-device
"Send to <device>" scripts (Nautilus / Nemo / Dolphin) are owned by
`src/file_manager_integration.py` and re-sync after every pairing
save / rename / unpair.

## Layout

| Path | Purpose |
|---|---|
| `AppRun.sh` | AppImage entrypoint. Sets `PYTHONHOME`/`PATH`/GI/GTK env, execs `python3.11 -m src.main`. |
| `desktop-connector.desktop` | AppImage-internal `.desktop` entry. Used by AppImageLauncher and the install hook (P.3b). |
| `linuxdeploy.recipe.sh` | Bundling driver for native libs + GTK4. Wired up in P.2a. |
| `build-appimage.sh` | Mechanical builder: `--source=<dir> --output=<dir>` → `desktop-connector-x86_64.AppImage`. |
| `build.sh` | Interactive driver wrapping `build-appimage.sh`. Prompts for source (github/local), output dir, persists answers at `~/.config/desktop-connector-build/state.json`. `--non-interactive` re-runs the saved choice. |
| `.tools/` | Vendored upstream AppImages (gitignored). Auto-downloaded on first run. |

## Tooling choice

`build-appimage.sh` uses `niess/python-appimage` to source the relocatable
Python interpreter, not `niess/linuxdeploy-plugin-python` (which is
deprecated upstream in favour of python-appimage). This is the same upstream
wheel layout, just delivered as a self-contained AppImage instead of a
linuxdeploy plugin.

linuxdeploy itself is still vendored — it lands in P.2a for GTK4 +
libadwaita + native lib bundling.

## Brand icons

Sourced at build time from `desktop/assets/brand/desktop-connector-{48,64,128,256}.png`
in the source checkout — not duplicated here. `build-appimage.sh` drops
them into `usr/share/icons/hicolor/<size>/apps/desktop-connector.png`.

## Usage

```bash
# Direct (mechanical, no prompts):
./build-appimage.sh --source=$PWD --output=/tmp/out

# Interactive (prompts for source + output, remembers your choices):
./build.sh

# Re-run last successful build silently:
./build.sh --non-interactive
```

Both honour `--help`. The mechanical builder downloads upstream tools
into `.tools/` on first run (~30 MB total) and caches them across
re-runs.

## Smoke test the build

```bash
./desktop-connector-x86_64.AppImage --version            # prints "Desktop Connector <ver>"
./desktop-connector-x86_64.AppImage --headless           # enters receiver loop
./desktop-connector-x86_64.AppImage --gtk-window=settings --config-dir=/tmp/dc
./desktop-connector-x86_64.AppImage --gtk-window=find-phone --config-dir=/tmp/dc
```

`--gtk-window=<NAME>` is an AppRun-internal dispatch that runs
`python -m src.windows <NAME>` against the bundled GTK4. NAME is one of
`send-files`, `settings`, `history`, `pairing`, `find-phone`,
`locate-alert`. The `find-phone` and `locate-alert` names retain the
legacy `phone` token for IPC stability — user-facing labels say
"Find my Device" / "Being located".

## Not here

CI workflow (`.github/workflows/desktop-release.yml`) lands in P.5a
and lives at the repo root, not in this folder.
