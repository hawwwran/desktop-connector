# desktop/packaging/appimage/

Linux AppImage packaging for the desktop client. See
`docs/plans/desktop-appimage-packaging-plan.md` for the full plan.

**Status:** P.2a. `build-appimage.sh` produces a runnable ~107 MB
AppImage with bundled Python + pure-Python deps + GTK4 + libadwaita +
WebKitGTK 6.0. All four subprocess windows (send-files, settings,
history, find-phone) render with bundled libs. Subprocess
re-entry via `$APPIMAGE` lands in P.2b — until then `tray.py` /
`windows.py` still spawn `python3 -m src.windows`, which only works in
the dev tree.

## Layout

| Path | Purpose |
|---|---|
| `AppRun.sh` | AppImage entrypoint. Sets `PYTHONHOME`/`PATH`/GI/GTK env, execs `python3.11 -m src.main`. |
| `desktop-connector.desktop` | AppImage-internal `.desktop` entry. Used by AppImageLauncher and the install hook (P.3b). |
| `linuxdeploy.recipe.sh` | Bundling driver for native libs + GTK4. Wired up in P.2a. |
| `build-appimage.sh` | Mechanical builder: `--source=<dir> --output=<dir>` → `desktop-connector-x86_64.AppImage`. |
| `build.sh` | Interactive driver wrapping `build-appimage.sh`. Wired up in P.1c. |
| `.tools/` | Vendored upstream AppImages (gitignored). Auto-downloaded on first run. |

## Tooling choice

`build-appimage.sh` uses `niess/python-appimage` to source the relocatable
Python interpreter, not `niess/linuxdeploy-plugin-python` (which is
deprecated upstream in favour of python-appimage). The plan
(`desktop-appimage-packaging-plan.md`) names the plugin; this is the same
upstream wheel layout, just delivered as a self-contained AppImage instead
of a linuxdeploy plugin.

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

# Interactive (P.1c, not yet wired):
./build.sh
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
`send-files`, `settings`, `history`, `pairing`, `find-phone`.

## Not here

CI workflow (`.github/workflows/desktop-release.yml`) lands in P.5a
and lives at the repo root, not in this folder.
