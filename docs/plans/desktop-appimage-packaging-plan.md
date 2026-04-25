# Desktop AppImage packaging — primary distribution artefact

**Status: DRAFT** — not started. Implementation plan for shipping the
Linux desktop client as a self-contained, self-updating AppImage.

Companion to:
- `docs/plans/appimage-distro-support-plan.md` — strategy: which
  distros are supported, what "support" means, build cadence. This
  document is the **how**; that document is the **what** and **why**.
- `docs/plans/secrets-and-signing-plan.md` — covers Android signing
  + Firebase service account hygiene. Desktop AppImage signing
  (GPG) lives here; the underlying "where do release secrets
  live" hygiene mirrors the model laid out in the secrets plan.
- `docs/plans/brand-rollout.md` — desktop brand work is still
  pending. AppImage cutover is a natural moment to land the
  desktop-side brand pass alongside packaging polish.

Correctness over speed. Each phase is independently landable. The
existing `desktop/install.sh` apt-pip flow keeps working end-to-end
until P.7 cuts it over to the AppImage path. Users on the old
install layout migrate on first AppImage launch (P.4).

---

## Why this exists

Today's desktop distribution model is `curl ... install.sh | bash`.
That works but has four structural weaknesses:

1. **No update mechanism.** Users discover updates manually, or
   never. The script is idempotent so re-running it updates, but
   there is no in-app prompt, no version awareness, no delta
   transfer.
2. **Distro-coupled.** The installer assumes `apt`, assumes
   `python3-gi` / `gir1.2-gtk-4.0` / `gir1.2-adw-1` are available
   from the system package manager at versions the app actually
   wants. On Ubuntu 22.04 the system libadwaita is 1.0; the app
   uses 1.5+ paint paths (`brand.py:118`). Today this is patched
   over by users running newer distros. Tomorrow it breaks.
3. **Trust model is bare.** `curl | bash` over HTTPS is fine for
   power users but reads as user-hostile to mainstream desktop
   audiences. No signature verification, no provenance.
4. **Not portable.** Each install drags a couple hundred MB of
   apt + pip dependencies into the user's system. AppImage is one
   self-contained file the user can rm to fully uninstall.

AppImage solves all four cleanly without a toolkit migration. It
matches the support strategy already locked in
(`appimage-distro-support-plan.md`): one artefact per architecture,
broad distro coverage via building on an old base.

---

## Goals

After this plan lands the desktop release looks like:

- **One artefact per architecture** — `desktop-connector-x86_64.AppImage`.
  ARM64 deferred until demand exists.
- **Self-contained** — no system-level deps required beyond a
  modern glibc + a tray-capable desktop environment. GTK4,
  libadwaita, Python interpreter, all native deps bundled inside.
- **Self-updating** — tray menu shows "Update available → 0.3.1"
  when a new release publishes; one click triggers an
  AppImageUpdate zsync delta pull (~few hundred KB, not the full
  120+ MB).
- **Signed** — GPG-signed AppImage + zsync metadata; public key
  published in the repo and on the project README.
- **Reproducible** — built by GitHub Actions from a tagged
  commit. A clean checkout reproduces byte-identical artefacts.
- **Migrates existing installs** — first launch detects an old
  `~/.local/share/desktop-connector/` layout and absorbs its
  config/keys/history without user action.

Non-goals (out of scope for this plan):
- Flatpak. Worth doing later for Flathub presence; tracked
  separately. AppImage stays primary either way.
- `.deb` / `.rpm` per-distro packages. Users on Arch/Fedora can
  package via AUR/COPR if they want; the project ships AppImage.
- Windows / macOS. Stays Linux-only. The packaging shape here
  doesn't preclude future Qt-based cross-platform work
  (`docs/plans/desktop-client-migration-plan.md`).
- ARM64. Add when demand exists; the GitHub Actions workflow
  can be parameterised to a matrix later.

---

## Build base decision — local on host, releases on Ubuntu 22.04 CI

Two build environments, two purposes — kept deliberately separate
so neither has to compromise.

### Local builds (your laptop) — host-native

Run directly on the host, no container. The AppImage produced is
whatever-the-host-can-bundle: it picks up the host's glibc, so it
runs on the host's distro family and newer. On Zorin OS 18 /
Ubuntu 24.04 derivatives the local AppImage targets glibc 2.39+
(Zorin 17+, Mint 22+, Pop! 24.04+, Debian 13+, Fedora 40+).

This is **fine for development, demos, and your own machine** —
it isn't the published release artefact. The local build exists
so you can iterate fast without waiting on CI.

### Release builds (GitHub Actions) — Ubuntu 22.04 runner

CI runs on `ubuntu-22.04` (glibc 2.35) to produce the canonical
wide-compatibility AppImage published to GitHub Releases. This is
what users download. Coverage floor: Zorin 16+, Mint 21+,
Pop! 22.04+, Debian 12+, Fedora 36+. Same one binary as
`appimage-distro-support-plan.md` calls for.

| Build | Where | glibc | Coverage |
|---|---|---|---|
| Local (you) | Host (Zorin 18 / Ubuntu 24.04+) | 2.39 | 24.04+ family |
| Release | GitHub Actions `ubuntu-22.04` | 2.35 | 22.04+ family |

### What's bundled either way

GTK4, libadwaita, Python, all native deps — all bundled inside the
AppImage. The host's GTK/Python is just where `linuxdeploy` copies
from; the finished AppImage doesn't depend on host runtime.

If your host can't satisfy a dep `linuxdeploy` wants to copy
(libadwaita 1.5+ on Ubuntu 22.04, say), the local build fails fast
and points at what's missing. Solution is `apt install` on the
host — not a container. CI runner provisions its own deps via the
workflow file (P.5).

### Why no Docker

Docker was on the table for "reproducible local builds = reproducible
release builds." Dropped because: the only environment that needs
to be canonical is the release one (CI), and the GitHub Actions
runner already pins that. Locally you trade some reproducibility
for fast iteration — fair trade. If a local build behaves differently
from the released AppImage, that's a CI-vs-local debugging issue,
not an everyday problem.

---

## Tooling choice

Local development is **direct on host** — no Docker, no chroot, no
virtualenv layering. Tools come from one of three sources:

- **System apt packages** for things the host already installs cleanly.
- **Vendored AppImages** under `desktop/packaging/appimage/.tools/`
  (gitignored), auto-downloaded on first run by the interactive driver.
- **GitHub Actions actions** for CI-only steps (P.5).

The vendored-AppImage trick avoids polluting `/usr/local/` and lets
two separate clones build independently without stepping on each other.

| Tool | Source | Used by |
|---|---|---|
| `linuxdeploy` | Vendored AppImage | Local + CI builder |
| `linuxdeploy-plugin-gtk` | Vendored AppImage | Local + CI builder |
| `linuxdeploy-plugin-python` | Vendored AppImage | Local + CI builder |
| `appimagetool` | Vendored AppImage | Local + CI builder |
| Python 3.11+ | apt (host) / runner (CI) | Source for relocatable interpreter |
| GTK4 + libadwaita 1.5+ | apt (host, possibly via PPA) / runner (CI) | Source for `linuxdeploy-plugin-gtk` to copy |
| `zsync` | apt | Build (delta metadata) |
| `AppImageUpdate` | Vendored AppImage | **Runtime** inside the released app, not the build |
| GPG | apt | Release signing only (P.5) |
| GitHub Actions | — | Release CI on `ubuntu-22.04` runner |

**Not** chosen and why:
- **Docker / podman** — adds a multi-GB image layer for what amounts
  to running a few binaries; slows iteration; the user explicitly
  doesn't want it. Reproducibility win belongs to CI (P.5), not
  local Docker.
- **`appimage-builder`** (declarative recipe) — more opinionated,
  smaller community, less Python+GTK precedent. Reconsider only if
  `linuxdeploy` becomes painful.
- **`pyinstaller`** / **`nuitka`** — adds a layer without buying
  anything; `linuxdeploy-plugin-python` already handles the
  relocatable interpreter problem.
- **`flatpak-builder`** — different distribution model, tracked
  separately as a v2 path.

---

## What stays unchanged

- **`desktop/src/`** — all Python source. AppImage runs the same
  `python3 -m src.main` entrypoint, same arg parsing, same
  pairing/poller/tray code paths.
- **Subprocess windows** — the GTK4-as-subprocess pattern stays.
  Each window subprocess uses the **same bundled Python +
  GTK4** out of the AppImage, so the GTK3-vs-GTK4 isolation
  reason for the split is preserved.
- **Config layout** — `~/.config/desktop-connector/` keeps its
  schema. AppImage reads/writes the same files an apt-pip install
  did.
- **`desktop/install.sh`** as a fallback path — kept available
  but rebranded to "install AppImage" in P.7. Power users who
  want the source-tree install get a separate
  `install-from-source.sh`.
- **`version.json`** — single source of truth for desktop
  version, bumped per release. AppImage embeds it as
  `usr/share/desktop-connector/version.json`.
- **Brand assets** at `desktop/assets/brand/` — bundled into the
  AppImage's hicolor icon hierarchy.
- **Server-URL prompt** — moves from terminal `read -p` (in
  install.sh) to a GTK4 first-launch dialog (P.4).

---

## What changes

- **Build pipeline** — new directory `desktop/packaging/appimage/`
  holds the linuxdeploy recipe, AppRun script, .desktop
  template, GTK runtime bootstrap, and CI workflow.
- **Update mechanism** — new module `desktop/src/updater/` polls
  GitHub releases (24h cache), surfaces in tray menu, triggers
  `AppImageUpdate` subprocess on user click.
- **Distribution channel** — primary is GitHub Releases (signed
  AppImage + zsync + .sig). Secondary is `install.sh` rewritten
  to download + place the AppImage (~30 lines instead of 330).
- **Autostart + .desktop entries** — point at the AppImage path
  (`~/.local/share/desktop-connector/desktop-connector.AppImage`)
  rather than the `~/.local/bin/desktop-connector` shell wrapper.
  AppImageUpdate replaces in place; no version suffix in the path.
- **File-manager scripts** — Nautilus/Nemo/Dolphin "Send to
  Phone" entries call the AppImage with `--send=` rather than
  `python3 -m src.main`. Path resolution survives the AppImage
  being moved (AppImage exposes `$APPIMAGE` env var to children).

---

## Bouncing between AppImage and `install-from-source.sh`

For dev work the two install paths must remain interchangeable:
running one then the other then the first again must always leave
a working app. The plan supports this through a deliberately
simple invariant — **last install wins, shared user data is
preserved** — rather than a clever side-by-side coexistence model.

The mechanics:

- **Shared state** — `~/.config/desktop-connector/` (config,
  keys, history, pairings) is owned by neither install path.
  Both modes read/write the same files in the same schema. Bouncing
  never loses data.
- **System integration** — `.desktop` entries, autostart,
  file-manager scripts get fully overwritten by whichever install
  path ran last. There's no merge / no preservation: last writer
  decides what the menu launches.
- **Each path is idempotent** — `install-from-source.sh` cleanly
  re-creates the apt-pip layout on every run; AppImage's P.4b
  cleanly absorbs whatever apt-pip layout it finds on every
  launch. Either action is safe to repeat.

Concrete bounce sequence, end-to-end working at every step:

1. `install-from-source.sh` → apt-pip install active, menu
   launches it.
2. Run AppImage from anywhere → P.4b deletes `src/` +
   `~/.local/bin/desktop-connector`, rewrites
   `.desktop`/autostart to point at AppImage. Menu now launches
   AppImage.
3. `install-from-source.sh` again → recreates `src/` +
   `~/.local/bin/desktop-connector`, rewrites
   `.desktop`/autostart back to the apt-pip wrapper. AppImage
   file at `~/.local/share/desktop-connector/desktop-connector.AppImage`
   is now orphaned but harmless.
4. Run AppImage again → P.4b absorbs the new apt-pip install.

App always works after each step. No portable mode, no
side-by-side coexistence, no surprise dialogs.

---

## Phases

Each phase is split into **sub-steps** sized to fit one focused
sitting (~1–3 hours each). Each sub-step is independently landable
— it ends on a single commit, has its own acceptance criteria, and
leaves the repo in a working state. You don't have to do an entire
phase in one go.

The phase boundaries themselves are where the bigger validation
happens: green `test_loop.sh` from inside an AppImage build (where
applicable) plus a user-side smoke check on the priority distros
from `appimage-distro-support-plan.md` (Ubuntu LTS, Mint, Zorin).

### P.1 — Build foundation

Three sub-steps. P.1a is pure scaffolding (no build); P.1b is the
mechanical builder (the actually-hard one); P.1c is the interactive
driver wrapping P.1b.

#### P.1a — Repo scaffolding

**Estimated effort:** ~1 hour.

**What changes:**

1. New folder `desktop/packaging/appimage/` containing:
   - `AppRun.sh` — stub entrypoint that sets the runtime env
     (`LD_LIBRARY_PATH`, `GI_TYPELIB_PATH`,
     `GSETTINGS_SCHEMA_DIR`, `GDK_PIXBUF_MODULE_FILE`,
     `XDG_DATA_DIRS`, `PYTHONPATH`, `PYTHONHOME`) relative to
     `$APPDIR` and execs Python with `-m src.main`.
   - `desktop-connector.desktop` — AppImage-internal desktop
     entry (used by AppImageLauncher and the install hook in P.3b).
   - `linuxdeploy.recipe.sh` — placeholder shell driver.
   - `build-appimage.sh` — stub with `--source` / `--output` arg
     parser + `--help`. No actual build yet.
   - `build.sh` — stub with `--help`. No prompts yet.
   - `.tools/.gitignore` — `*` (vendored linuxdeploy AppImages
     live here, never committed).
   - `README.md` — what's in this folder, how to use it, where
     state lives.
2. Vendor brand icon PNGs (48/64/128/256) into a build-time
   `icons/` subfolder so P.1b can drop them into the AppImage's
   hicolor structure.
3. No build is attempted at this step.

**Acceptance:**
- Folder `desktop/packaging/appimage/` exists with all files above.
- `./desktop/packaging/appimage/build.sh --help` and
  `./desktop/packaging/appimage/build-appimage.sh --help` each
  print a one-screen usage summary.
- README explains the folder layout in <60 lines.

#### P.1b — Mechanical builder (`build-appimage.sh`)

**Estimated effort:** ~2–3 hours including debugging.

**What changes:**

1. `build-appimage.sh --source=<dir> --output=<dir>` runs end-to-end:
   - Auto-downloads vendored AppImage tools into `.tools/` if
     missing: `linuxdeploy-x86_64.AppImage`,
     `linuxdeploy-plugin-python-x86_64.AppImage`, `appimagetool`.
     (`linuxdeploy-plugin-gtk` is added in P.2a; not needed yet.)
   - Stages an `AppDir/` skeleton.
   - Copies `desktop/src/` into `AppDir/usr/lib/desktop-connector/`.
   - Bundles a relocatable Python via `linuxdeploy-plugin-python`.
   - `pip install`s pure-Python deps (`pystray`, `qrcode`,
     `PyNaCl`, `cryptography`, `requests`, `Pillow`) into the
     bundled Python.
   - Drops brand icons into `AppDir/usr/share/icons/hicolor/.../`.
   - Calls `appimagetool` to pack `AppDir/` into
     `desktop-connector-x86_64.AppImage` in `<output>/`.
2. **No GTK bundling yet** — that's P.2a. The minimal AppImage
   has Python + pure-Python deps + `src/` only. Anything that
   imports `gi` will fail at runtime; that's expected at this step.
3. `set -euo pipefail`. `trap` cleans tmp dirs on exit (success
   or failure). Re-running with same args overwrites prior output
   cleanly (idempotent).

**Acceptance:**
- `./build-appimage.sh --source=$PWD --output=/tmp/out` produces
  `desktop-connector-x86_64.AppImage` (~30–60 MB; GTK arrives in P.2).
- `./desktop-connector-x86_64.AppImage --version` prints the
  desktop version from `version.json`.
- `./desktop-connector-x86_64.AppImage --headless` enters the
  receiver loop and connects to a relay server (no GUI is
  expected — that's P.2b).
- Re-running with the same args reproduces byte-identically
  modulo `SOURCE_DATE_EPOCH` (P.5b pins this; until then, allow
  small timestamp drift).
- Tmp dirs cleaned on both success and failure (verify by
  killing mid-run and inspecting `/tmp`).

#### P.1c — Interactive driver (`build.sh`)

**Estimated effort:** ~2–3 hours.

**What changes:**

1. `build.sh` implements the full interactive UX spelled out in
   the next section. Wraps `build-appimage.sh`.
2. State at `~/.config/desktop-connector-build/state.json`.
3. Pre-flight checks for vendored tools, disk space, host deps,
   git presence (lazy, only when github is chosen).
4. `--non-interactive` flag accepted for scripting / smoke
   tests — every prompt resolves to "use the saved default or
   fail loudly."
5. `set -euo pipefail` from line 1; `trap` cleanup.

**Acceptance:**
- Fresh run with no state file walks through all prompts and
  produces the same AppImage as a direct `build-appimage.sh`
  invocation.
- Re-running and pressing Enter at every prompt reproduces the
  prior build (source, output dir, AppImage byte-identical
  modulo `SOURCE_DATE_EPOCH`).
- `build.sh --non-interactive` runs with last-saved state; if
  state is missing it exits non-zero with a clear error.
- Pre-flight failure (vendored tool download fails, disk full,
  git missing for github mode) surfaces a single-line error and
  exits non-zero before any prompt.

---

## Interactive build driver (`build.sh`) — UX spec

Referenced by P.1c. The driver is the only thing the human ever
has to remember. **Simple to use**, **idempotent**, **defensive**:
every input gets validated, every prerequisite gets checked before
any work starts, every failure leaves the workspace clean.

State file: `~/.config/desktop-connector-build/state.json`. Holds:
```json
{
  "last_source": "github" | "local",
  "last_local_path": "/absolute/path/to/repo",
  "last_output_dir": "/absolute/path/to/out"
}
```
Created on first successful run; updated after every successful
run. Never read partially — if JSON parse fails, treat as empty
and proceed with first-run defaults.

Hardcoded constant inside `build.sh`:
```
REMOTE_REPO=https://github.com/hawwwran/desktop-connector
```
The user is never asked for the remote URL. If the project ever
moves repos, edit this one constant.

**Flow:**

1. **Pre-flight checks** (run before any prompt; fail fast with a
   one-line error per failure):
   - Vendored tools either present in `.tools/` or downloadable
     (offer "Download missing tools now? [Y/n]" on first run).
   - At least 2 GB free in `$TMPDIR` and in the chosen output dir
     (output dir checked after the prompt).
   - Host has Python 3.11+, GTK4 + libadwaita 1.5+ headers (only
     warned about pre-build; the actual GTK bundling is P.2a's
     concern). On Ubuntu 22.04 hosts that may need the GNOME 46
     PPA — print the apt one-liner and exit non-zero rather than
     trying to fix it for the user.
   - `git` is installed (only required for the github path —
     check lazily after the source choice).
   - Network reachable for the github option (lazy check via
     `git ls-remote`, only when github is chosen).
2. **Source choice prompt**:
   ```
   Build from:
     [g] github (pulls latest from main of REMOTE_REPO)
     [l] local repo
   Choice [g/l] (default: <last_source or "g">):
   ```
   Single-key answer; default shown in the prompt; Enter accepts
   the default. Invalid input re-prompts (no exit).
3. **If `local`**:
   ```
   Path to local desktop-connector repo
   [<last_local_path or "(none — type a path)">]:
   ```
   - Enter on a populated default → use `last_local_path`.
   - Typed path → resolve to absolute, expand `~`.
   - Validate: directory exists, contains `version.json` AND a
     `desktop/` subdirectory, AND the repo's git remote (if any)
     matches `REMOTE_REPO` OR the user explicitly confirms a
     non-matching remote ("This repo's remote is <X>, not the
     canonical <REMOTE_REPO>. Build anyway? [y/N]").
   - Validation failure → re-prompt (do not exit).
4. **If `github`**:
   - `git ls-remote $REMOTE_REPO HEAD` → confirms reachability +
     surfaces the commit SHA that's about to be built.
   - Prompt: `Pull latest from main of <REMOTE_REPO> @ <sha>? [Y/n]`.
     Enter → yes. `n` → re-enter source-choice prompt.
   - Shallow clone (`--depth 1`) into a tmp dir; tmp dir is
     `trap`'d for cleanup on exit (success or failure).
5. **Output dir prompt**:
   ```
   Output directory [<last_output_dir or "$PWD">]:
   ```
   Enter accepts default; typed path resolved + created if
   missing (`mkdir -p`); writability validated.
6. **Confirmation summary** before running:
   ```
   About to build:
     source:  <local path or github SHA>
     output:  <output dir>
     tools:   .tools/linuxdeploy-x86_64.AppImage (etc.)
   Proceed? [Y/n]:
   ```
7. **Run** `build-appimage.sh --source=<resolved> --output=<dir>`.
   Stream output live; on non-zero exit, leave the partial output
   dir for inspection but don't update the state file.
8. **On success**:
   - Update `~/.config/desktop-connector-build/state.json` with
     all three fields.
   - Print the produced AppImage path + size + SHA-256.
   - If a previous AppImage existed at the same output path, the
     new one overwrites it (idempotent re-run); print a one-line
     diff ("replaces previous build from <date>").

**Idempotence + safety properties:**

- Re-running with identical answers produces the same AppImage
  (modulo `SOURCE_DATE_EPOCH`).
- Mid-run failure: tmp clones / partial outputs cleaned by
  `trap`; state file untouched.
- First-ever run with no state file: defaults to `github`,
  prompts for output dir with `$PWD` default — never crashes on
  missing state.
- `--non-interactive` flag accepted for scripting (every prompt
  becomes "use the default or fail loudly").
- The whole script is `set -euo pipefail` from line 1.

### P.2 — GTK4 + libadwaita bundling

Two sub-steps. P.2a is "GTK is in the AppImage and one window
opens"; P.2b extends to all four subprocess windows + correct GI
typelib coverage.

#### P.2a — Bundle GTK4 + libadwaita libs

**Estimated effort:** ~2–4 hours (high debug risk — first time
linuxdeploy-plugin-gtk is wired up).

**What changes:**

1. Add `linuxdeploy-plugin-gtk-x86_64.AppImage` to the vendored
   `.tools/` set; `build-appimage.sh` invokes it.
2. The plugin bundles GTK4, libadwaita, gdk-pixbuf loaders,
   `loaders.cache` (regenerated to AppImage-internal paths), GIO
   modules, and `gschemas.compiled`.
3. If host's GTK/libadwaita is too old (Ubuntu 22.04 hosts:
   libadwaita 1.0 vs needed 1.5+), pre-install newer GTK on the
   host via the GNOME 46 PPA — document the apt one-liner in the
   driver's pre-flight error message. Don't try to build GTK from
   source.
4. `AppRun.sh` updated so `LD_LIBRARY_PATH`, `GI_TYPELIB_PATH`,
   `GSETTINGS_SCHEMA_DIR`, `GDK_PIXBUF_MODULE_FILE`,
   `XDG_DATA_DIRS` all point at the bundled GTK runtime under
   `$APPDIR`.

**Acceptance:**
- AppImage size jumps to ~120–180 MB (GTK + libadwaita present).
- One window opens manually:
  `./desktop-connector-x86_64.AppImage --pair` runs the pairing
  flow with full GTK4/libadwaita rendering.
- The AppImage runs on a host that has *no* system GTK4 installed
  (verifies bundling, not borrowing from host).

#### P.2b — Subprocess windows + `$APPIMAGE` re-entry

**Estimated effort:** ~1–2 hours.

**What changes:**

1. Bundle GI typelibs explicitly (verify via `ls
   $APPDIR/usr/lib/girepository-1.0/`): `Gtk-4.0`, `Adw-1`,
   `Gdk-4.0`, `GLib-2.0`, `GObject-2.0`, `Gio-2.0`, `Pango-1.0`,
   `cairo-1.0`, `GdkPixbuf-2.0`, `AyatanaAppIndicator3-0.1`.
2. Subprocess invocations in `desktop/src/tray.py` and
   `desktop/src/windows.py` use `$APPIMAGE` to re-enter the same
   AppImage (rather than calling system `python3`). Falls back
   to `python3 -m src.windows ...` when not running inside an
   AppImage (preserves dev-mode invocation).
3. Verify all four subprocess windows render correctly.

**Acceptance:**
- All four subprocess windows (send-files, settings, history,
  find-phone) open from the tray, render brand-correct
  (libadwaita 1.5 accent paths working), and close cleanly.
- `test_loop.sh` passes against the AppImage as the desktop
  client (replace `python3 -m src.main` invocations with
  `$APPIMAGE`).
- Running outside an AppImage (dev mode, `python3 -m src.main`)
  still works — the `$APPIMAGE` re-entry is a fallback, not a
  hard requirement.

### P.3 — Desktop integration from the AppImage

Three sub-steps. P.3a is the tray; P.3b is the autostart/`.desktop`
install hook; P.3c is the file-manager scripts.

#### P.3a — Tray + `.desktop` from inside AppImage

**Estimated effort:** ~1–2 hours.

**What changes:**

1. Tray (`pystray` + `AyatanaAppIndicator3`) works from inside
   the AppImage on KDE / Cinnamon / Mate / XFCE / GNOME-with-
   extension. No regression vs the apt-pip install.
2. `.desktop` entry inside the AppImage carries the same
   `StartupWMClass=com.desktopconnector.Desktop` and `Categories`
   as today's installer-written entry.
3. Manual placement OK at this step — the install hook lands in P.3b.

**Acceptance:**
- Tray icon appears on all priority desktops; menu items invoke
  the subprocess windows correctly.
- AppImage's internal `.desktop` extracted by AppImageLauncher
  (or manual copy to `~/.local/share/applications/`) shows the
  app in the menu.

#### P.3b — Autostart + `.desktop` install hook

**Estimated effort:** ~1–2 hours.

**What changes:**

1. AppImage's first-launch behaviour writes a `.desktop` entry
   to `~/.local/share/applications/desktop-connector.desktop` and
   an autostart entry to `~/.config/autostart/desktop-connector.desktop`,
   both pointing at the AppImage's current path (`$APPIMAGE`).
2. On subsequent launches, if `$APPIMAGE` differs from what those
   entries point to (user moved the AppImage), they get silently
   rewritten.
3. Autostart respect: if the user removed the autostart entry
   (or dropped a `.no-autostart` marker like today's installer
   honours), don't re-create it.

**Acceptance:**
- First launch writes both entries; menu shows the app; reboot
  auto-launches.
- Moving the AppImage between `~/Downloads/` and `~/Applications/`
  doesn't break autostart after one launch from the new location.
- Removing `~/.config/autostart/desktop-connector.desktop` keeps
  it gone across launches.

#### P.3c — File-manager integration

**Estimated effort:** ~1 hour.

**What changes:**

1. The first-launch install hook (P.3b) also drops the
   Nautilus/Nemo "Send to Phone" scripts and the Dolphin service
   menu, all calling `$APPIMAGE --headless --send=%f` instead of
   the old `~/.local/bin/desktop-connector` shell wrapper.
2. Same idempotent rewrite-on-move behaviour as P.3b.

**Acceptance:**
- Right-click "Send to Phone" in Nautilus/Nemo/Dolphin sends a
  file via the AppImage.
- After moving the AppImage, the next launch updates the
  file-manager scripts to point at the new path.

### P.4 — First-launch UX + config migration

Two sub-steps. P.4a is the GTK4 onboarding dialog for fresh
machines; P.4b handles migration from an existing apt-pip install.

#### P.4a — GTK4 onboarding dialog

**Estimated effort:** ~2 hours.

**What changes:**

1. AppImage detects missing `~/.config/desktop-connector/config.json`
   on launch and runs a GTK4 onboarding dialog: server URL prompt
   (with `/api/health` probe like install.sh does today), autostart
   toggle, optional "Place AppImage in
   `~/.local/share/desktop-connector/`" convenience.
2. Server URL setup avoids the terminal `read -p` interaction —
   the AppImage user may never see a terminal.
3. Cancelling the dialog leaves the user in tray-only mode (the
   Settings window can configure later).

**Acceptance:**
- Fresh launch on a machine with no prior install: dialog →
  server URL → tray live, paired flow available.
- Cancelling the dialog leaves a usable (unconfigured) tray app.
- Re-launching after cancel re-opens the dialog (since config is
  still missing).

#### P.4b — Migration from apt-pip install

**Estimated effort:** ~2–3 hours (lots of safety checks).

**What changes:**

1. AppImage detects an old `~/.local/share/desktop-connector/src/`
   layout from a prior `install.sh` run.
2. Migration is **copy-then-verify-then-delete**: confirms config
   readable + key fingerprint matches before removing the old
   files. On verification failure, leaves both in place and
   surfaces a warning notification.
3. Replaces autostart entry to point at the AppImage. Removes
   `~/.local/bin/desktop-connector` shell wrapper.
4. User sees one notification: "Migrated from classic install —
   your pairings and history are preserved."

**Acceptance:**
- Launch on a machine with a working apt-pip install: migration
  completes silently (no key loss), old install files gone,
  autostart now points at the AppImage.
- Launch with stale config (orphaned keys but no `src/`): uses
  existing config, no migration prompt.
- Migration verification failure (key fingerprint mismatch, e.g.
  user manually edited keys): both installs remain, warning
  notification surfaced.

### P.5 — Release pipeline (GitHub Actions)

Two sub-steps. P.5a builds + publishes unsigned; P.5b adds GPG
signing + reproducibility pinning.

#### P.5a — GitHub Actions build + publish (unsigned)

**Estimated effort:** ~1–2 hours.

**What changes:**

1. New workflow `.github/workflows/desktop-release.yml` triggers
   on `desktop/v*` tag push.
2. Runs on `ubuntu-22.04` runner (this is what gives the
   wide-compat glibc 2.35 floor — no Docker layer needed).
3. Workflow:
   - Installs host build deps via apt (GTK4, libadwaita 1.5+ via
     PPA, Python 3.11+, zsync).
   - Calls `desktop/packaging/appimage/build-appimage.sh
     --source=$GITHUB_WORKSPACE --output=artefacts/`.
   - Runs the P.1b acceptance smoke (`--version` + `--headless`
     for 5s).
   - Generates `.zsync` via `zsyncmake`.
   - Generates `SHA256SUMS`.
   - Publishes AppImage + zsync + SHA256SUMS to GitHub Releases.
4. **No signing yet** — that's P.5b. AppImage is unsigned at
   this step.

**Acceptance:**
- `git tag desktop/v0.2.0 && git push origin desktop/v0.2.0`
  produces a published GitHub Release with AppImage + zsync +
  SHA256SUMS within ~10 minutes.
- Released AppImage runs on Ubuntu 22.04, 24.04, Mint 21,
  Zorin 17 (manual smoke).
- Workflow re-run on the same tag produces identical SHA256s
  (caveat: `SOURCE_DATE_EPOCH` not pinned until P.5b — small
  drift OK at this step).

#### P.5b — GPG signing + reproducibility pin

**Estimated effort:** ~1–2 hours (key generation + secrets setup).

**What changes:**

1. Release signing key generated on the user's machine. Public
   key committed (`docs/release/desktop-signing.pub.asc`).
   Private key + passphrase stored per the model in
   `secrets-and-signing-plan.md` (encrypted backup +
   continuity-across-machines plan).
2. CI uses GitHub Actions secrets `DESKTOP_SIGNING_KEY`
   (armoured private key) + `DESKTOP_SIGNING_PASS`.
3. Workflow signs the AppImage and the zsync file (detached
   `.sig` files); signs `SHA256SUMS` too.
4. `SOURCE_DATE_EPOCH` exported from the tag's commit timestamp
   before invoking `build-appimage.sh`. Pins SquashFS timestamps
   so byte-identical reproducibility actually works.
5. Published artefact set is now:
   - `desktop-connector-{version}-x86_64.AppImage`
   - `desktop-connector-{version}-x86_64.AppImage.zsync`
   - `desktop-connector-{version}-x86_64.AppImage.sig`
   - `SHA256SUMS`
   - `SHA256SUMS.sig`

**Acceptance:**
- `gpg --verify` on the released AppImage against the published
  public key passes.
- Re-tagging and re-running the workflow on the same commit
  produces a byte-identical AppImage.
- Lost-key recovery procedure (per `secrets-and-signing-plan.md`)
  documented in `docs/release/`.

### P.6 — In-app update check + AppImageUpdate

Two sub-steps. P.6a is the version checker (no UI changes); P.6b
adds tray menu wiring + AppImageUpdate.

#### P.6a — Version checker module

**Estimated effort:** ~2 hours.

**What changes:**

1. New module `desktop/src/updater/version_check.py` polls
   `https://api.github.com/repos/hawwwran/desktop-connector/releases/latest`
   with `If-Modified-Since` + 24h on-disk cache (cached in
   `~/.cache/desktop-connector/update-check.json`).
2. Filters to `desktop/v*` tags (Android `android/v*` tags are
   ignored). Compares to local `version.json`.
3. Surfaces a `(current_version, latest_version, release_url,
   asset_url)` tuple to callers.
4. **Gated on `$APPIMAGE` env var.** Module's public entrypoint
   short-circuits and returns "no update info available" when
   `$APPIMAGE` is unset — i.e. when running from an apt-pip
   install or from `python3 -m src.main` in a dev tree. The
   updater simply doesn't exist for those code paths. This keeps
   the existing apt-pip install completely silent during the
   AppImage transition (it can't act on an update via
   AppImageUpdate anyway, so surfacing one would be misleading).
5. Unit-tested against a fixture for the GitHub releases JSON,
   plus a test that verifies the gate (no API call when
   `$APPIMAGE` is unset).

**Acceptance:**
- Module returns the correct tuple when given a fixture
  containing newer / equal / older / non-desktop tags **and**
  `$APPIMAGE` is set.
- Module short-circuits to "no info" without any HTTP request
  when `$APPIMAGE` is unset (verified by mock).
- 24h cache prevents repeated API hits (tested via fixture
  timestamps).
- No-internet failure mode: returns last cached value with a
  flag; no exceptions surface to caller.

#### P.6b — Tray menu + AppImageUpdate runner

**Estimated effort:** ~2 hours.

**What changes:**

1. New module `desktop/src/updater/update_runner.py` invokes
   `AppImageUpdate` (vendored inside the AppImage) against
   `$APPIMAGE`. Streams progress to a tray callback.
2. Tray menu items: "Check for updates" (manual) and a
   conditional "Update available → 0.2.1" item that appears
   when `version_check` returns a newer tag. Dismissable
   per-version (writes the dismissed version to the cache).
3. **Gated on `$APPIMAGE` env var.** Both menu items are hidden
   entirely when `$APPIMAGE` is unset — apt-pip and dev-tree
   installs see no Update menu at all. Mirrors the P.6a gate:
   if the underlying mechanism can't run, the UI doesn't
   advertise it.
4. Background check runs once per launch + once every 24h while
   the app is running (only when `$APPIMAGE` is set). Surfaces
   silently in the tray menu; never pops a dialog. Click →
   confirm dialog → AppImageUpdate runs → app restarts itself.

**Acceptance:**
- AppImage install: new release published → installs see
  "Update available" within 24h or immediately on manual "Check
  for updates".
- Apt-pip / dev-tree install: no Update-related menu items
  appear; no background check fires; tray menu looks exactly
  like it does today.
- Click → AppImageUpdate pulls only the delta (~few hundred KB
  to a few MB), not the full ~150 MB AppImage.
- After update, app restarts on the new version. Config /
  pairings / history preserved (they live in
  `~/.config/desktop-connector/`, untouched).
- Dismissed updates stay dismissed for that version; next
  release re-surfaces the prompt.

### P.7 — Cutover + documentation

Two sub-steps. P.7a rewrites `install.sh` / `uninstall.sh` /
`install-from-source.sh`; P.7b updates README + CLAUDE.md.

#### P.7a — Rewrite install.sh + uninstall.sh

**Estimated effort:** ~1–2 hours.

**What changes:**

1. `desktop/install.sh` rewritten to ~30 lines: download the
   latest AppImage from GitHub Releases, GPG-verify it (against
   the public key shipped in the repo), place at
   `~/.local/share/desktop-connector/desktop-connector.AppImage`,
   chmod +x, run it once (triggers P.4a first-launch). Remove
   all apt + pip + `src/` copy logic.
2. `desktop/install-from-source.sh` — new file containing the
   old apt-pip install logic, for contributors / power users.
3. `desktop/uninstall.sh` rewritten — remove the AppImage +
   `.desktop` entries + autostart + file-manager scripts.
   Optionally remove `~/.config/desktop-connector/` (existing
   prompt).

**Acceptance:**
- `curl ... install.sh | bash` on a clean Ubuntu 22.04 / Mint
  21 / Zorin 17 install ends with the app running and a
  configured server URL.
- `curl ... install.sh | bash` on a machine with the old apt-pip
  install replaces it cleanly (uses P.4b migration path).
- Documented uninstall leaves nothing behind except optionally-
  kept config.
- `install-from-source.sh` works on a clean Ubuntu 22.04 host.

#### P.7b — Documentation

**Estimated effort:** ~1 hour.

**What changes:**

1. README updated: primary install becomes the same one-liner
   with different internals; manual download path documented:
   "Download the AppImage from [Releases](…), `chmod +x`,
   double-click."
2. CLAUDE.md gets a single bullet under Building noting the
   AppImage as the release shape and `install-from-source.sh`
   as the dev shape.
3. CONTRIBUTING (or equivalent) mentions `install-from-source.sh`
   for contributors.
4. Old release tarballs stay in GitHub Releases (don't delete
   history). New releases stop producing them.

**Acceptance:**
- README + CLAUDE.md describe the new shape; old curl|bash
  apt-pip language is gone.
- A first-time visitor reading the README knows how to install
  in <1 minute of reading.

---

## Versioning + release process

- `version.json` `desktop` field is the source of truth.
- Release tag format: `desktop/v{semver}` (matches existing
  convention from `install.sh:107`).
- Bump → tag → push triggers P.5 workflow → AppImage published
  → in-app updater (P.6) picks it up within 24h.
- **No release branches.** Tag `main` directly. AppImage is
  built from the tagged commit, period.
- **No pre-release channel** initially. If beta channel becomes
  needed, tag prefix `desktop/v{semver}-beta.N`; updater
  filters them out of `latest` by default with an opt-in
  setting. Out of scope for this plan.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| GTK4 + libadwaita 1.5+ bundling on Ubuntu 22.04 CI runner is intractable. | P.2a acceptance includes the bundling smoke on `ubuntu-22.04`. If it fails, fall back to Ubuntu 24.04 runner and accept narrower distro floor. Document the decision explicitly in the workflow file. |
| Local build behaves differently from the released AppImage. | Acceptable trade for fast iteration. CI is the canonical reference; if a CI artefact breaks on a distro the local build said worked, debug against CI logs. Don't try to make local match CI bit-for-bit. |
| Host can't satisfy GTK 4.14+ / libadwaita 1.5+ for local builds. | `build.sh` pre-flight prints the exact apt one-liner (with the GNOME 46 PPA). User runs it; build proceeds. Don't auto-install — host package management is the user's call. |
| AppImage size > 200 MB after GTK + WebKit bundling (find-phone map). | Audit at end of P.2a. If WebKit is a major contributor, evaluate switching find-phone map to a static / pure-Cairo render before P.5. |
| pystray under AppImage misses tray on some desktops it works on today. | P.3a acceptance tests on each priority desktop. If GNOME degrades, fall back to documenting the AppIndicator extension requirement (already true today). |
| AppImageUpdate fragility — known historic bugs around `--no-confirm` and zsync server compatibility. | Vendor AppImageUpdate at a known-good version inside the AppImage. Don't rely on system `AppImageUpdate`. P.6b acceptance includes a real release-to-update round-trip. |
| GPG key loss → can't sign new releases. | Mirror the keystore-continuity model from `secrets-and-signing-plan.md`. Generate, back up encrypted, document recovery before P.5b ships. |
| First-launch migration corrupts user config. | P.4b migration is a copy-then-verify-then-delete with explicit checks (config readable + key fingerprint matches before old install gets removed). On verification failure, leave both in place and surface a warning. |
| Reproducible builds drift as upstream GTK/libadwaita moves. | Pin GTK + libadwaita versions via apt-pin in the CI workflow. Bump deliberately, test the bump like any other change. |
| GitHub API rate limits hit the version checker for users behind shared NAT. | 24h cache + `If-Modified-Since` keeps the request count negligible. Unauthenticated rate limit is 60/hour per IP — well above any realistic load. |

---

## What this plan does NOT do

- **No Flatpak.** Track separately; AppImage stays primary
  regardless.
- **No `.deb` / `.rpm`.** Out of scope. Community packagers
  welcome.
- **No Windows / macOS.** Stays Linux-only. Cross-platform
  ambitions are tracked under
  `docs/plans/desktop-client-migration-plan.md` (Path B,
  Rust+Qt, long-term).
- **No toolkit migration.** Python + pystray + GTK4-subprocess
  pattern stays exactly as-is. This plan only changes how the
  app gets to the user.
- **No protocol changes.** Server, Android, on-the-wire format
  unchanged.
- **No new features.** First-launch dialog (P.4) is a
  packaging requirement, not a UX feature pass. Brand rollout
  for desktop (`brand-rollout.md`) is recommended alongside but
  tracked separately.

---

## Suggested order of operations

The 7 phases (P.1 → P.7) split into 16 sub-steps. Each sub-step
is one focused sitting; you don't have to hold the whole plan in
your head at any point.

**Land P.1a first** as a quick win — it's just folder + file
scaffolding, no build, ~1 hour, ends on a tidy commit. Gives you
something tangible and validates the directory layout before any
real work.

**P.1b → P.2a are the structural risk.** If either fails (the
mechanical builder doesn't produce a runnable AppImage; GTK
bundling won't work on the chosen base) the rest doesn't matter.
Land them with care, validate on the priority distros, accept
slow progress.

**P.1c, P.2b, P.3a–c, P.4a–b are integration polish.** Can
interleave with each other or with brand rollout
(`brand-rollout.md`) work as desired.

**P.5a → P.6b are release infrastructure.** Land in order; P.5a
must work before P.5b makes sense, P.6a must work before P.6b
needs it. Neither has user-facing visibility until P.7.

**P.7a → P.7b are the cutover.** After P.7b the old install.sh
path is dead and the project's primary distribution shape is the
AppImage.

Each sub-step ends on a landable commit. Don't merge a sub-step
until its acceptance criteria pass; don't merge a phase boundary
(P.N → P.N+1) until the priority distros (Ubuntu LTS / Mint /
Zorin) all round-trip successfully.
