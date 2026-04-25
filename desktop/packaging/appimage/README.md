# desktop/packaging/appimage/

Linux AppImage packaging for the desktop client. See
`docs/plans/desktop-appimage-packaging-plan.md` for the full plan.

**Status:** P.1a (scaffolding only). The build pipeline is stubbed —
running `build-appimage.sh` today only echoes what it would do.

## Layout

| Path | Purpose |
|---|---|
| `AppRun.sh` | AppImage entrypoint. Sets GTK / GI / Python env, execs `python3 -m src.main`. |
| `desktop-connector.desktop` | AppImage-internal `.desktop` entry. Used by AppImageLauncher and the install hook (P.3b). |
| `linuxdeploy.recipe.sh` | Bundling driver invoked by `build-appimage.sh`. Filled in P.1b/P.2a. |
| `build-appimage.sh` | Mechanical builder: `--source=<dir> --output=<dir>` → `desktop-connector-x86_64.AppImage`. |
| `build.sh` | Interactive driver wrapping `build-appimage.sh` with prompts + state. Filled in P.1c. |
| `.tools/` | Vendored linuxdeploy AppImages (gitignored). Auto-downloaded on first run. |

## Brand icons

Sourced at build time from `desktop/assets/brand/desktop-connector-{48,64,128,256}.png`
in the source checkout — not duplicated here. `build-appimage.sh` drops
them into the AppImage's `usr/share/icons/hicolor/<size>/apps/`
hierarchy.

## Usage

```bash
# Direct (mechanical, no prompts):
./build-appimage.sh --source=$PWD --output=/tmp/out

# Interactive (P.1c, not yet wired):
./build.sh
```

Both honour `--help`. `build.sh` also accepts `--non-interactive`
to run with the last-saved state.

## State

`build.sh` persists answers at
`~/.config/desktop-connector-build/state.json`:

```json
{
  "last_source": "github" | "local",
  "last_local_path": "/abs/path",
  "last_output_dir": "/abs/path"
}
```

`.tools/` holds vendored `linuxdeploy*.AppImage` and `appimagetool`.
Each clone of the repo gets its own copy; tools are never committed.

## Not here

CI workflow (`.github/workflows/desktop-release.yml`) lands in P.5a
and lives at the repo root, not in this folder.
