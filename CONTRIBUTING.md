# Contributing

Thanks for considering a contribution. This is a small project — there's no formal review SLA, but PRs that follow the conventions below land faster.

## Repo layout

- `server/` — PHP blind relay (no framework, SQLite). Front controller at `server/public/index.php`.
- `desktop/` — Python tray + GTK4 subprocess windows. Released as a signed AppImage; sources runnable from the tree.
- `android/` — Kotlin + Jetpack Compose app, Foreground service for receiving, WorkManager for uploads.
- `docs/plans/` — local working notes, refactor + bugfix plans.
- `tests/protocol/` — cross-runtime contract tests (server, desktop, platform interfaces).

[`CLAUDE.md`](CLAUDE.md) has the deep-dive architecture; read it before changing how transfers, pairing, or fasttrack work.

## Hacking on the desktop client

For local source-tree work, install the dev-tree variant — apt + pip + a thin shell wrapper, no AppImage build cycle on every change:

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install-from-source.sh | bash
```

(or run `bash desktop/install-from-source.sh` from a checkout.)

Run from your checkout while iterating:

```bash
cd desktop && python3 -m src.main          # tray
cd desktop && python3 -m src.main --pair   # pairing flow
cd desktop && python3 -m src.main --headless --send=/tmp/foo.txt
```

GTK4 windows live in `src/windows.py` and run as subprocesses — pystray loads GTK 3 in the main process, so the GTK 4 windows have to be isolated. To launch one directly while developing:

```bash
python3 -m src.windows settings --config-dir=~/.config/desktop-connector
```

Bouncing between this and the AppImage install is safe — `~/.config/desktop-connector/` is shared between both paths; whichever install ran last owns the system integration files (`.desktop`, autostart, file-manager scripts).

## Building the AppImage locally

The packaging plan lives in [`docs/plans/desktop-appimage-packaging-plan.md`](docs/plans/desktop-appimage-packaging-plan.md). To build a local AppImage from the source tree:

```bash
./desktop/packaging/appimage/build-appimage.sh --source=$PWD --output=/tmp/dc-out
```

Vendored linuxdeploy / appimagetool / appimageupdatetool tools are auto-downloaded into `desktop/packaging/appimage/.tools/` on first run and cached. Host needs GTK 4.10+, libadwaita 1.5+, girepository-2.0 2.80+ (on Ubuntu 22.04 you need a backport PPA; on 24.04 / Zorin 18 / Mint 22+ default apt is enough).

Releases are produced by [`.github/workflows/desktop-release.yml`](.github/workflows/desktop-release.yml) on `desktop/v*` tag push — pin `version.json`'s `desktop` field to match the tag before pushing.

## Hacking on the server

```bash
php -S 0.0.0.0:4441 -t server/public/
```

SQLite DB + storage are auto-created on first request at `server/data/connector.db` and `server/storage/`. Routes live in `server/public/index.php`; controllers, services, repos under `server/src/`. Architecture notes (request pipeline, persistence, logging conventions) in `CLAUDE.md`.

## Hacking on the Android client

```bash
export ANDROID_HOME=/opt/android-sdk
cd android && ./gradlew assembleDebug
```

APK lands at `android/app/build/outputs/apk/debug/app-debug.apk`. Release builds need the keystore — see [`docs/plans/secrets-and-signing-plan.md`](docs/plans/secrets-and-signing-plan.md).

## Tests

```bash
python3 -m unittest discover -s tests/protocol     # cross-runtime contract tests
./test_loop.sh                                     # full server+desktop integration round-trip (needs PHP)
```

## Conventions

- **Commit messages**: `feat(scope): subject`, `fix(scope): subject`, `docs(scope): subject`, `ci(scope): subject`. Body wraps at ~72 cols and explains the *why*. Use `Co-Authored-By:` for AI-assisted commits.
- **No secrets in repo**: no API tokens, no signing keys, no passwords. The Android signing keystore is `.gitignored`; CI signing keys live in GitHub Actions secrets — see `docs/plans/secrets-and-signing-plan.md` and `docs/release/desktop-signing-recovery.md`.
- **Diagnostics events**: dot-notation event vocabulary (`transfer.init.accepted`, `clipboard.write_text.succeeded`); catalog at [`docs/diagnostics.events.md`](docs/diagnostics.events.md). Never log keys, tokens, decrypted clipboard/file/GPS content, public keys, or encrypted payloads.
- **Plans before invasive changes**: large refactors get a working note in `docs/plans/`. Small fixes can go straight to a PR.
