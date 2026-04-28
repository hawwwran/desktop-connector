# Android auto-update plan

**Status: LANDED** (Phases 1–7). End-to-end verified on device 2026-04-28:
banner shows under status bar, modal opens, download → system installer
→ in-place upgrade succeeds, post-upgrade state reports current. Real
release cut by user separately.

Notable additions outside the original plan:
- `versionCode` bumped 5 → 8 to match the released APK (a published 0.3.1
  must be `versionCode > 5`; iterating during testing landed at 8).
- Truncation check added to `UpdateDownloader`: if response advertised a
  Content-Length and `bytesRead != total`, fail loudly instead of writing
  a partial APK that the system installer rejects as "package invalid".
- `AppPreferences` round-trip unit test deferred — `AppPreferences` uses
  Android `SharedPreferences` which would need Robolectric; the
  end-to-end on-device verification covers the same property.

Self-contained Android-side auto-updater. Independent of the desktop
updater (`desktop/src/updater/`) — different platform, different
constraints, no shared abstractions. The Android app polls GitHub
Releases for newer `android/v*` tags, surfaces an update banner at the
top of the app, and on confirmation downloads the published APK and
hands it to the system installer.

## Goals

- **Discovery on app open** — at most one network check per 24 h, then
  a top banner if a newer release is available and not skipped.
- **Manual re-check** — "Check for updates" button in Settings always
  fetches (force=true), bypasses the 24 h cache.
- **Modal install flow** — banner tap → modal showing release info +
  `[Install]` / `[Skip this version]` / `[Close]`. Install streams the
  APK to disk and launches the system installer; same-keystore
  signature → in-place update.
- **Skip persists per-version** — skipped versions hide the banner but
  still surface in Settings; force-check still opens the modal. A
  newer release un-suppresses the banner naturally (different version
  string).
- **Debug builds opt out** so dev devices don't try to "upgrade"
  themselves to a release build.
- **Never block UI or other functionality.** The check is fully
  asynchronous, with aggressive timeouts (3 s connect, 5 s read, 8 s
  overall) so a flaky network never freezes anything. Failures fall
  silently back to the cached state.
- **Abort the auto-check on minimize.** If the user backgrounds the
  app while a check is in flight, the request is cancelled. The
  user-initiated APK download is intentional and does **not** cancel
  on minimize (a `WAKE_LOCK` keeps it alive).

## Non-goals

- Sharing code or release infrastructure with the desktop AppImage
  updater. The two implementations stay independent.
- Silent / unattended updates. Android's installer dialog is always
  user-confirmed for sideloaded apps.
- Automating Android release CI. Releases at `android/v0.3.0` are
  already shaped correctly (single `Desktop-Connector-<ver>-release.apk`
  asset published manually); the updater works with that as-is. CI
  remains a separate, optional track.
- Migrating the keystore out of git. Out of scope; called out
  separately at the end.

## High-level flow

```
on app resume                        on Settings "Check for updates" tap
        |                                          |
        v                                          v
maybeCheck() ────── if (now - lastCheckAt < 24h) ──> use cache
        |                                          |
        v                                          v
   GitHub /releases  <──────── force=true ─────────┘
        |
        v
parse → first non-draft non-prerelease where tag startsWith "android/v"
        |
        v
UpdateInfo(latest, apkUrl, releaseNotes, isNewer, stale)
        |
        v
UpdateUiState ∈ { NONE, AVAILABLE, AVAILABLE_SKIPPED }
        |                |                |
   no banner       banner shown      no banner
                          |                |
                          v                v
                       modal ←─── settings button (force-check) ──→ modal
                          |
                          v
                   [Install] → download → system installer
                   [Skip]    → persist + state→AVAILABLE_SKIPPED
                   [Close]   → state unchanged
```

---

## Where today's Android code is (relevant entry points)

- `android/app/build.gradle.kts` — reads `versionName` from repo-root
  `version.json`'s `android` field; `versionCode = 5` is hardcoded
  (out of scope to fix here, but worth a follow-up).
- `android/app/src/main/AndroidManifest.xml`
  - Already declares `REQUEST_INSTALL_PACKAGES` (line 22).
  - Already declares the `FileProvider` at
    `${applicationId}.fileprovider`.
- `android/app/src/main/res/xml/file_paths.xml` — has `cache-path`
  mapping (`name="cache"` → `.`), so cached APKs at
  `cacheDir/updates/*.apk` are FileProvider-shareable as-is.
- `android/app/src/main/kotlin/com/desktopconnector/ui/transfer/TransferViewModel.kt:425-465`
  — existing APK install block: `canRequestPackageInstalls()` gate +
  redirect to settings, FileProvider URI, `Intent.ACTION_VIEW` with
  `application/vnd.android.package-archive`. Phase 3 lifts this into
  a reusable helper.
- `android/app/src/main/kotlin/com/desktopconnector/data/AppPreferences.kt`
  — `dc_prefs` SharedPreferences. Add update fields here.
- `android/app/src/main/kotlin/com/desktopconnector/network/ApiClient.kt`
  — OkHttp instance pattern; mirror its style for `UpdateChecker` /
  `UpdateDownloader`.
- `android/app/src/main/kotlin/com/desktopconnector/MainActivity.kt`
  — wire `UpdateViewModel.onAppOpen()` from `onResume`; wrap the
  `NavHost` in a `Column { UpdateBanner(...); NavHost(...) }` for
  app-global banner placement.
- `android/app/src/main/kotlin/com/desktopconnector/ui/SettingsScreen.kt`
  — version footer at lines 530-549 is where the "Check for updates"
  button + skipped-state subtitle land.

GitHub Releases shape (verified against live data 2026-04-27):
- Tag pattern: `android/v0.3.0`.
- Single asset: `Desktop-Connector-0.3.0-release.apk` (~36 MB,
  `application/vnd.android.package-archive`).
- Body: markdown changelog generated by GitHub.

---

## Phase chunks

Each phase is independently landable and reviewable. Phases 1–4 add
plumbing without changing user-visible behaviour; Phase 5 is the first
phase that turns on a feature; Phase 6 finishes the discovery story;
Phase 7 hardens.

### Phase 1 — Storage + data model

**Goal:** preferences and the `UpdateInfo` data class. No HTTP, no UI.

**Files:**
- `android/app/src/main/kotlin/com/desktopconnector/data/AppPreferences.kt`
  - `var lastUpdateCheckAt: Long` (epoch millis; 0 means never).
  - `var dismissedUpdateVersions: Set<String>` with `dismissVersion(v)`
    / `isDismissed(v)`.
  - `var cachedLatestVersion: String?` (so Settings can render the
    "skipped" subtitle without a re-fetch on every Settings open).
- `android/app/src/main/kotlin/com/desktopconnector/network/UpdateInfo.kt`
  - `data class UpdateInfo(currentVersion, latestVersion, releaseUrl,
    apkUrl, releaseNotes, isNewer, stale)`. Frozen / immutable.

**Acceptance:**
- Compiles. No behaviour change.
- Round-trip test: dismiss `"0.3.1"` → reload prefs → `isDismissed("0.3.1") == true`.

---

### Phase 2 — Version check engine

**Goal:** `UpdateChecker.check()` returns a populated `UpdateInfo` (or
null) given the network state. Pure-ish — no UI dependencies, no
permissions, no Compose imports. JVM-unit-testable with a mock OkHttp
client.

**Files:**
- `android/app/src/main/kotlin/com/desktopconnector/network/UpdateChecker.kt`
  - `class UpdateChecker(context: Context, http: OkHttpClient, prefs: AppPreferences)`.
  - `suspend fun check(force: Boolean = false): UpdateInfo?`.
  - **Dedicated OkHttp client** (do **not** reuse `ApiClient`'s
    instance — different timeout profile): `connectTimeout = 3 s`,
    `readTimeout = 5 s`, `writeTimeout = 5 s`, `callTimeout = 8 s`.
    The 8 s overall ceiling guarantees the suspend never blocks the
    coroutine for longer than that, even on a network blackhole.
  - All network work runs on `withContext(Dispatchers.IO)`. Coroutine
    cancellation cooperates: `ensureActive()` between the cache read,
    HTTP call, and JSON parse so a cancellation on minimize aborts
    promptly. Wrap the OkHttp `execute()` so `Call.cancel()` fires
    when the coroutine is cancelled (use the
    `kotlinx-coroutines-okhttp` `await()` extension or a manual
    `suspendCancellableCoroutine` that calls `call.cancel()` in
    `invokeOnCancellation`).
  - On `CancellationException` re-throw immediately (don't swallow,
    don't replay cache, don't log warn) — the caller deliberately
    cancelled.
  - Endpoint: `https://api.github.com/repos/hawwwran/desktop-connector/releases?per_page=30`.
  - Headers: `Accept: application/vnd.github+json`, `User-Agent:
    desktop-connector-android-updater`, `If-Modified-Since` from
    cache when present.
  - Cache file: `context.cacheDir/update-check.json` —
    `{fetched_at, last_modified, release: {tag, html_url, apk_url, body}}`.
  - 24 h freshness check skipped on `force=true`.
  - 304 response: refresh `fetched_at`, replay cached release.
  - Parser: first non-draft non-prerelease where
    `tag_name.startsWith("android/v")`; first asset whose `name`
    ends `.apk`. Return null if either is missing.
  - Version compare: split on `.`, `Int.toIntOrNull()` each part;
    any non-int → `isNewer = false` (don't crash on `0.3.0-rc.1`).
  - Network failure path: log warn, replay cached release with
    `stale = true`. No cache → return null.
  - **Gate on `BuildConfig.DEBUG`**: `if (BuildConfig.DEBUG) return null`
    at the top.
- `android/app/src/test/kotlin/com/desktopconnector/network/UpdateCheckerTest.kt`
  - JVM unit tests with `MockWebServer`:
    - newer / equal / older / unparseable version strings.
    - prerelease and draft entries skipped.
    - missing `.apk` asset → null.
    - 24 h cache: second call within window skips network.
    - 304 path: cache replay, `stale = false`.
    - Network failure with stale cache: returns cached `UpdateInfo`
      with `stale = true`.
    - **Slow server (`MockWebServer.dispatcher` with `bodyDelay`)**:
      `check()` returns within `callTimeout` budget; `stale = true`.
    - **Coroutine cancellation**: launch `check()`, cancel the job
      mid-flight, verify `CancellationException` propagates and the
      underlying `okhttp3.Call` was cancelled (no late socket
      activity).
    - Dismissed-version filter is **NOT** in `UpdateChecker` — that's
      the ViewModel's concern; checker reports raw `isNewer`.

**Acceptance:**
- All unit tests pass.
- Manual smoke: from a JUnit test pointing at the live endpoint,
  `check()` returns the current `android/v0.3.0` release.

---

### Phase 3 — APK installer helper (refactor)

**Goal:** lift the existing APK install logic from `TransferViewModel`
into a shared helper so Phase 6 can reuse it without duplication. No
behaviour change.

**Files:**
- `android/app/src/main/kotlin/com/desktopconnector/util/Installer.kt`
  - `object Installer { fun installApk(context: Context, apk: File): InstallStartOutcome }`.
  - `enum class InstallStartOutcome { LAUNCHED, MISSING_PERMISSION, FILE_GONE, ERROR }`.
  - Body lifted verbatim from `TransferViewModel.kt:425-465`:
    `canRequestPackageInstalls()` gate (returns
    `MISSING_PERMISSION` + redirects to
    `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES`), FileProvider URI
    via `${context.packageName}.fileprovider`, `Intent.ACTION_VIEW`
    with `application/vnd.android.package-archive`, flags
    `FLAG_GRANT_READ_URI_PERMISSION | FLAG_ACTIVITY_NEW_TASK`.
- `android/app/src/main/kotlin/com/desktopconnector/ui/transfer/TransferViewModel.kt`
  - Replace the 30-line block at 425-465 with
    `Installer.installApk(app, file)` + a short outcome→toast switch.

**Acceptance:**
- Manually: send an APK from the desktop → tap it in history →
  installer dialog appears, behaviour identical to before.
- No new lint warnings; no AppLog tag changes.

---

### Phase 4 — Update download

**Goal:** APK download with progress, cancellable, no UI yet.

**Files:**
- `android/app/src/main/kotlin/com/desktopconnector/network/UpdateDownloader.kt`
  - `class UpdateDownloader(context: Context, http: OkHttpClient)`.
  - `fun download(url: String, version: String): Flow<DownloadProgress>`
    that emits `DownloadProgress.Started`, `.Progress(bytesRead, total)`,
    `.Done(file: File)`, `.Failed(reason)`.
  - Streams to `context.cacheDir/updates/Desktop-Connector-<version>.apk`
    via OkHttp `BufferedSource`.
  - Holds a partial `WAKE_LOCK` for the duration (releases on
    completion, failure, or cancellation — `try/finally`).
  - Cancellation honoured via Kotlin coroutine cancellation
    (`ensureActive()` between buffer reads).
- `android/app/src/main/kotlin/com/desktopconnector/util/UpdateCacheCleanup.kt`
  - `fun pruneOldUpdates(context: Context, maxAgeDays: Int = 7)` —
    deletes `cacheDir/updates/*.apk` older than the threshold. Called
    from `MainActivity.onCreate` once per process.

**Acceptance:**
- Instrumented test (or quick manual run with a small fixture URL):
  download a known artefact, verify file exists at expected path with
  expected length.
- Cancellation test: collect 1 progress event, cancel the coroutine,
  verify `WAKE_LOCK` released and partial file deleted.

---

### Phase 5 — UI: Settings entry + modal (no banner yet)

**Goal:** end-to-end update flow accessible from Settings. Banner is
absent; users discover updates via the Settings button. After this
phase the feature is functionally complete; Phase 6 only adds
discovery polish.

**Files:**
- `android/app/src/main/kotlin/com/desktopconnector/ui/update/UpdateViewModel.kt`
  - Activity-scoped (instantiated at `MainActivity` and passed to both
    `UpdateBanner` later and `SettingsScreen`).
  - Holds `StateFlow<UpdateUiState>` where `UpdateUiState` is one of:
    - `Idle` — no info yet.
    - `NoUpdate(currentVersion)`.
    - `Available(info: UpdateInfo, dismissed: Boolean)`.
    - `Checking` — overlays previous state during a force-check.
    - `Downloading(progress: Float)`.
    - `Launching` — APK ready, intent fired.
    - `Error(message: String)`.
  - **Two separate `Job` slots, with different cancellation
    semantics**:
    - `private var checkJob: Job? = null` — auto-check + force-check.
      Cancelled when the activity backgrounds (`MainActivity.onStop`
      calls `cancelInFlightCheck()`). Re-launching while one is in
      flight: cancel the prior job first (debounce).
    - `private var downloadJob: Job? = null` — APK download.
      **NOT** cancelled on minimize. Only the user's explicit
      `[Cancel]` button or modal dismissal stops it; a `WAKE_LOCK`
      held by `UpdateDownloader` keeps the CPU alive.
  - `fun onAppOpen()` — `checkJob = viewModelScope.launch { checker.check(force=false) }`.
    No-op (cache hit) when called within 24 h.
  - `fun onForceCheck()` — `checkJob = viewModelScope.launch { checker.check(force=true) }`.
    Always opens the modal afterwards via `_modalOpen.value = true`
    regardless of `dismissed`.
  - `fun cancelInFlightCheck()` — `checkJob?.cancel()`. If state was
    `Checking`, revert to the prior state without surfacing an
    error (the user backgrounded the app, they're not waiting).
  - `fun onInstall()` — `downloadJob = viewModelScope.launch { ... }`,
    runs `UpdateDownloader.download(...)`, on `.Done(file)` calls
    `Installer.installApk(...)`, transitions to `Launching` then back
    to `Idle` after a short delay.
  - `fun onSkipVersion()` — `prefs.dismissVersion(latestVersion)`,
    state → `Available(info, dismissed=true)`.
  - `fun onDismissModal()` — close modal, state untouched.
  - `fun onCancelDownload()` — `downloadJob?.cancel()`; revert state
    to `Available(info, dismissed)`.
- `android/app/src/main/kotlin/com/desktopconnector/ui/update/UpdateModal.kt`
  - Compose `AlertDialog` (or `ModalBottomSheet` — pick whichever
    matches existing app style; existing dialogs in `LogsDialog` use
    `AlertDialog`, so go with that).
  - Three internal phases driven by `UpdateUiState`:
    1. **Info** — title "Update available", current → latest line,
       collapsible release-notes excerpt (first 800 chars of
       `UpdateInfo.releaseNotes`, "View on GitHub" link to
       `releaseUrl`), buttons `[Install]` `[Skip this version]`
       `[Close]`. If `dismissed=true`, `[Skip this version]` becomes
       `[Skipped ✓]` (visual confirmation only — already persisted).
    2. **Downloading** — `LinearProgressIndicator` with percent,
       `[Cancel]`.
    3. **Launching** — brief spinner with caption "Opening installer…"
       — modal closes shortly after intent fires.
  - Error states render an inline error band at the top of the modal
    in `Info` phase ("Couldn't reach update server — try again later")
    and revert there on download failure too.
- `android/app/src/main/kotlin/com/desktopconnector/ui/SettingsScreen.kt`
  - New `SettingsItem` row above the version footer at lines 530-549:
    - Primary label: "Check for updates".
    - Secondary label (computed from `UpdateUiState`):
      - `Available(_, dismissed=false)` → "Update available — vX.Y.Z".
      - `Available(_, dismissed=true)` → "vX.Y.Z available (skipped)".
      - `NoUpdate` → "You're on the latest version (vA.B.C)".
      - else → "Tap to check now".
    - Tap → `viewModel.onForceCheck()`. Modal opens regardless of
      whether the version was previously skipped (this is the
      "force-check still opens popup" requirement).

**Acceptance:**
- Manual on a real device against a real release:
  - Open Settings, tap "Check for updates" — modal appears with the
    correct latest version + notes.
  - Tap `[Install]` — progress bar advances, system installer dialog
    appears, app updates in place, data preserved.
  - Tap `[Skip this version]` — modal closes, settings subtitle now
    reads "vX.Y.Z available (skipped)". Reopen Settings → tap "Check
    for updates" → modal opens again with `[Skipped ✓]` rendered.
- No banner anywhere yet.

---

### Phase 6 — UI: Top banner

**Goal:** discovery on app open. Exactly the behaviour the user asked
for: banner appears when newer + not skipped; vanishes when skipped.

**Files:**
- `android/app/src/main/kotlin/com/desktopconnector/ui/update/UpdateBanner.kt`
  - Compose `Surface` (color = `BrandColors.dcYellow600` light /
    `dcYellow500` dark per visual identity guide).
  - Body: "Update available — vX.Y.Z" with a small chevron / arrow
    icon. Single-line, max 56 dp tall.
  - Visible iff `state is Available && !state.dismissed`.
  - Tap → `viewModel.openModal()`.
- `android/app/src/main/kotlin/com/desktopconnector/MainActivity.kt`
  - Instantiate `UpdateViewModel` at activity scope.
  - `onResume()`: `viewModel.onAppOpen()` (rate-limited internally to
    24 h via the cache check).
  - `onStop()`: `viewModel.cancelInFlightCheck()` — aborts the
    auto-check / force-check if it's still running. Use `onStop`
    rather than `onPause` so transient interruptions (a permission
    prompt overlay, a partial dismissal of a system dialog) don't
    spuriously cancel the request — `onStop` only fires when the
    activity is genuinely no longer visible. The download is
    intentionally untouched here.
  - Wrap the existing `NavHost` content in
    `Column { UpdateBanner(state, onClick); NavHost(...) }`.
  - `UpdateModal` is hosted by `MainActivity` (not by individual
    screens) so it surfaces from any tab when the banner / Settings
    button opens it.

**Acceptance:**
- Open the app on a device that's behind on releases → banner appears
  at the top within ~1 s of resume.
- Tap banner → modal opens with same behaviour as Phase 5's force-check.
- Tap `[Skip this version]` → banner disappears, modal closes. Re-open
  app → no banner. Open Settings → "vX.Y.Z available (skipped)". Tap
  "Check for updates" → modal still opens.
- Wait for next release → new tag pushed → relaunch app → banner
  reappears (different version string ⇒ skip doesn't apply).
- **Minimize-during-check**: enable airplane mode (forces the
  request to hang on connect) → cold-launch the app → the moment
  it foregrounds, immediately press Home. With logging on, observe
  `update_check.cancelled` (no `update_check.network_error` and no
  late `update_check.network_ok` after returning to the app).

---

### Phase 7 — Polish + tests

**Goal:** edge cases, cleanup, manual test playbook.

**Files & tasks:**
- `android/app/src/main/kotlin/com/desktopconnector/MainActivity.kt`
  - Call `UpdateCacheCleanup.pruneOldUpdates(this)` from `onCreate`
    so cached APKs from prior installs don't accumulate.
- `android/app/src/main/kotlin/com/desktopconnector/network/UpdateChecker.kt`
  - Confirm `BuildConfig.DEBUG` early-return is in place (added in
    Phase 2 but worth re-verifying).
- `android/app/src/main/AndroidManifest.xml`
  - **No new permissions required.** `INTERNET`, `WAKE_LOCK`,
    `REQUEST_INSTALL_PACKAGES` already declared.
- `android/app/src/test/kotlin/com/desktopconnector/data/AppPreferencesUpdateTest.kt`
  - Round-trip: dismiss multiple versions, persist, reload, verify
    set membership.
  - `cachedLatestVersion` survives prefs round-trip.
- `docs/plans/android-autoupdate-plan.md` (this file)
  - Flip status to **LANDED** with commit references after Phase 6
    merges.

**Manual end-to-end playbook (real device, real release):**
1. Install the app from a `android/v0.3.0` APK manually.
2. Confirm: open app → no banner (you are current).
3. Push a new tag `android/v0.3.1`, publish a release with the
   matching APK asset.
4. Re-open app on the device → banner appears within seconds.
5. Tap banner → modal shows v0.3.0 → v0.3.1 with release notes.
6. Tap `[Install]` → progress bar to 100% → system installer →
   confirm → app relaunches as v0.3.1.
7. Verify history, paired devices, settings all preserved.
8. Repeat with `[Skip this version]` instead — banner vanishes,
   Settings shows skipped indicator, force-check re-opens modal.
9. Toggle airplane mode mid-download → modal returns to Info phase
   with "Couldn't download update".
10. Revoke "Install unknown apps" permission → tap `[Install]` →
    redirect to system Settings.
11. **Cancellation under flaky network**: enable airplane mode,
    cold-launch the app, immediately press Home before the 8 s
    `callTimeout` elapses. Reopen the app → banner state is whatever
    the cache held (no error toast, no stuck spinner).
12. **Download survives minimize**: tap `[Install]`, press Home
    after the progress bar starts moving. Re-foreground the app —
    the modal is still in `Downloading` phase (or already on
    `Launching` if the download finished while backgrounded).

**Acceptance:**
- Playbook passes end-to-end on at least one Android 12+ device.
- No new lint errors. Existing `assembleRelease` succeeds.
- Update check fires at most once per 24 h (verified by log scrubbing
  `update_check.network` events in `AppLog`).

---

## Open questions

1. **Banner placement: app-global or HomeScreen-only?**
   - Plan defaults to **app-global** (above `NavHost` in `MainActivity`)
     so a user mid-pairing or mid-folder-browse still sees the prompt.
   - Alternative: HomeScreen-only — less intrusive but worse discovery.
   - Decide before Phase 6 starts. Switching is a small refactor.
2. **Modal style: `AlertDialog` vs `ModalBottomSheet`?**
   - Plan defaults to `AlertDialog` (matches existing `LogsDialog`).
   - Bottom sheet might suit longer release notes better. Cosmetic.
3. **Should the banner pulse / animate on first appearance?**
   - Default: no — static surface, brand-yellow background already
     stands out per the visual identity guide.

---

## Risks & mitigations

- **GitHub API rate limit (60/h unauthenticated per IP).** With the
  24 h cache + If-Modified-Since path most checks cost zero requests
  after the first; force-checks from Settings are user-initiated and
  rare. No mitigation needed unless we see real-world rate limiting.
- **Slow / unreachable network freezing the app.** Mitigated by the
  3 s connect / 5 s read / 8 s overall OkHttp timeouts on the
  dedicated update client, plus `onStop`-driven cancellation. The
  worst-case cost of an unreachable network is one 8 s coroutine in
  the background — never blocks the UI.
- **APK download cancelled mid-stream leaves partial file.** Handled
  in Phase 4 — `try/finally` deletes the partial on cancellation.
- **System installer denied (user cancels confirmation dialog).** App
  state stays at `Available`; user can retry. Cached APK pruned by
  Phase 7's startup cleanup after 7 days regardless.
- **Same-keystore drift.** As long as `android/keystore.jks` stays the
  signing key for every published `android/v*` release, in-place
  updates work. If the key ever rotates, users on old versions
  cannot auto-update — they'd have to uninstall + reinstall manually.
  Out of scope; flag for release runbook when keystore rotation is
  considered.
- **Forked-repo confusion.** The updater hardcodes
  `https://api.github.com/repos/hawwwran/desktop-connector/releases`.
  A fork's releases will not be fetched.

---

## Out of scope (separate tracks)

- **Android release CI workflow.** The updater works against manually
  published releases as-is (verified against `android/v0.3.0`'s
  shape). Adding `.github/workflows/android-release.yml` would
  formalise version-code monotonicity + asset naming, but the
  autoupdate feature does not depend on it.
- **`versionCode` source-of-truth.** Currently hardcoded to `5` in
  `android/app/build.gradle.kts`. Should derive from
  `version.json.android` (e.g. `0.3.1` → `301`) so monotonic ordering
  is structural. Independent refactor.
- **Keystore migration to GitHub Secrets.** `android/keystore.jks` is
  committed with passwords in-tree. Trust model for the autoupdater
  is "trust the repo owner's release process"; the URL is hardcoded,
  so a fork can't shadow updates. But anyone with the keystore can
  sign APKs that the system would accept as a same-signature update
  if they could host them at the canonical URL — they can't, but the
  release-engineering hygiene is still worth fixing. Independent track.
