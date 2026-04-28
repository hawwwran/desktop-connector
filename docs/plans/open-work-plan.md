# Open work — consolidated plan

**Status: DRAFT** — created 2026-04-28 by consolidating the two
remaining open tails of larger plans that have otherwise landed.

The wider plans are archived under
`temp/finished-plans/` (gitignored — historical reference only):
- `secrets-and-signing-plan.md` — Phase 1 never landed; rest is
  process documentation.
- `desktop-appimage-packaging-plan.md` — P.1a, P.1b, P.2–P.8 all
  landed. P.1c (the interactive build driver) was deferred while
  the mechanical `build-appimage.sh` carried the load.

This plan is the to-do list. Two independent tracks, **A** (Android
signing hygiene — has a real security tail) and **B** (AppImage
interactive build driver — UX polish). Track A is higher priority
because of the committed-secret issue.

---

## Track A — Android signing secrets cleanup

**Why:** `android/app/build.gradle.kts` lines 41 + 43 still have
`storePassword = "<retired-password>"` and
`keyPassword = "<retired-password>"` in plaintext, and the file is
git-tracked. The keystore itself (`android/keystore.jks`) is
properly gitignored, so the password alone is meaningless to a
repo-history attacker — but the project's stated goal ("repo
carries zero secrets") is violated as long as that string lives in
source.

**Scope:** stop committing the passwords; strengthen the long-term
backup story so the keystore + passwords survive a wiped laptop.

**Out of scope:** rewriting git history. The keystore was never
committed, so historical leaks of the password string can't sign
anything. Force-push cost outweighs benefit.

### A.1 — De-hardcode signing passwords ⏱ ~30 min

**Status: DONE 2026-04-28.** `build.gradle.kts:41,43` now read
from `DC_KEYSTORE_PASSWORD` / `DC_KEY_PASSWORD` Gradle
properties (with `DC_KEY_ALIAS` defaulting to
`desktop-connector`). Values live in
`~/.gradle/gradle.properties` (`chmod 600`, outside repo).
Verified: `assembleRelease` produces APK with cert SHA-256
`426bd5f13d38368f3f6071db732d5bc902ab290ba578c3e32c6a21297e9c6840`
— matches the keystore identity, so installed apps continue to
update. Negative test: removing the properties file causes
`BUILD FAILED`. Not committed yet — awaiting user go-ahead.

**What changes:**

1. Edit `android/app/build.gradle.kts` lines 36–46. Replace:
   ```kotlin
   storePassword = "<retired-password>"
   keyAlias = "desktop-connector"
   keyPassword = "<retired-password>"
   ```
   with:
   ```kotlin
   storePassword = (project.findProperty("DC_KEYSTORE_PASSWORD") as? String) ?: ""
   keyAlias = (project.findProperty("DC_KEY_ALIAS") as? String) ?: "desktop-connector"
   keyPassword = (project.findProperty("DC_KEY_PASSWORD") as? String) ?: ""
   ```
2. Create `~/.gradle/gradle.properties` (outside the repo) with
   the actual values:
   ```properties
   DC_KEYSTORE_PASSWORD=<retired-password>
   DC_KEY_ALIAS=desktop-connector
   DC_KEY_PASSWORD=<retired-password>
   ```
   `chmod 600 ~/.gradle/gradle.properties`.
3. Verify `cd android && ./gradlew assembleRelease` still produces
   a signed APK in `app/build/outputs/apk/release/`.
4. Verify `adb install -r` against the existing dev-installed app
   succeeds (signature unchanged → updates work).

**Acceptance:**
- `git grep -n <retired-password> android/app/build.gradle.kts`
  returns nothing.
- Release build still produces a signed APK with the same
  signature fingerprint as before.
- `findProperty` returning `null` (i.e. missing
  `~/.gradle/gradle.properties`) makes the build fail with a
  clear "invalid keystore password" error rather than silently
  producing an unsigned APK — verify by temporarily renaming
  the file.

### A.2 — (Optional) Rotate to a stronger password ⏱ ~20 min, user-run

**Status: DONE 2026-04-28.** Rotated to a 40-char random password
via `~/temp-scripts/038-rotate-android-keystore-password.sh`.
Surprise during execution: the keystore is PKCS12 (modern default
since JDK 9), so `keytool -keypasswd` is unsupported and store +
key passwords share one value. Script + recovery doc adjusted
to reflect this. `gradle.properties` now holds the same value in
both `DC_KEYSTORE_PASSWORD` and `DC_KEY_PASSWORD`. Verified:
`./gradlew clean assembleRelease` produces an APK with cert
SHA-256 unchanged (`426b…6840`). New file SHA-256 recorded in
`docs/release/android-signing-recovery.md`. KeePassXC entry
updated; backups deleted.

The current password `"<retired-password>"` is weak and was visible
in git history for years. Rotation is independent of A.1 — it can
happen later, or never. Do it if you want a clean break.

**Approach:** because rotation runs `keytool -storepasswd` /
`-keypasswd` on the live keystore (irreversible on failure), I
write a script with placeholders and you run it yourself. Per the
saved feedback rule, credential-generating commands are user-run.

**What I'll do:**
1. Write `~/temp-scripts/NNN-rotate-android-keystore-password.sh`
   following the home-CLAUDE template (numbered prefix, `.log`
   sibling, `chmod +x`). Script body uses placeholder env vars
   `DC_NEW_KEYSTORE_PASSWORD` / `DC_NEW_KEY_PASSWORD` that the
   user fills in before running.
2. Script runs `keytool -storepasswd` then `keytool -keypasswd`
   with the new values; captures both old and new password hashes
   into the log for verification.

**What you do:**
1. Pick the new passwords (use a password manager generator).
2. Edit the script header to fill in the placeholders.
3. Run it; report the exit code.
4. Update `~/.gradle/gradle.properties` to match.
5. Update the password-manager backup item (A.3).

**Acceptance:**
- `cd android && ./gradlew assembleRelease` succeeds with the
  new passwords.
- `adb install -r` against the dev-installed app still succeeds
  (the keystore identity doesn't change; only the protection
  password changed).

### A.3 — Document keystore backup ⏱ ~20 min, user-run

**Status: DONE 2026-04-28.** Backed up into KeePassXC vault.
Entry holds the keystore as attachment, store password in the
main password field, key password as protected custom attribute,
and verification fingerprints in the notes (file SHA-256 +
certificate SHA-256/SHA-1). User-side SHA-256 round-trip
verified the attachment matches the live keystore byte-for-byte.

**Why:** if `android/keystore.jks` is lost, sideload updates to
the existing installed app fail with
`INSTALL_FAILED_UPDATE_INCOMPATIBLE`. Users would have to
uninstall + reinstall, losing paired-device state. The plan calls
for **two physically distinct copies**.

**What you do** (no code change):

1. **Primary backup — password manager.** Create one item titled
   "Desktop Connector — Android signing":
   - Attached file: `keystore.jks`
   - Field `storePassword`: current password
   - Field `keyAlias`: `desktop-connector`
   - Field `keyPassword`: current password
   - Note: "Loss = cannot sideload updates under same identity.
     Re-create only as last resort:
     `keytool -genkey -v -keystore keystore.jks -keyalg RSA
     -keysize 4096 -validity 10000 -alias desktop-connector`."
2. **Secondary backup — pick one:**
   - Encrypted 7z (`7z a -p -mhe=on backup.7z keystore.jks
     passwords.txt`) on a cloud drive you control.
   - USB stick in a drawer with a Cryptomator/VeraCrypt volume
     containing the same bundle.
   - `git-crypt`-protected private repo.
3. Verify: download from the password manager onto a different
   machine, run `keytool -list -keystore keystore.jks`, check
   the SHA-256 fingerprint matches the live build's signature.

**Acceptance:**
- Two backup locations exist; you've test-restored at least the
  primary onto a second device.

### A.4 — New-machine provisioning checklist ⏱ ~10 min docs

**Status: DONE 2026-04-28.** Runbook landed at
`docs/release/android-signing-recovery.md`. Mirrors the existing
desktop-signing-recovery.md shape: identity table, where
materials live, verification commands, new-machine restoration
steps, lost-keystore recovery, anti-checklist.

**Why:** when (not if) you migrate machines, the recovery path
needs to be reproducible from notes, not memory.

**What changes:**

Create `docs/release/android-signing-recovery.md` mirroring the
shape of the existing `docs/release/desktop-signing-recovery.md`.
Sections:

1. Clone repo + install Android SDK (`ANDROID_HOME`, `local.properties`).
2. Restore `android/keystore.jks` from the password manager
   attachment; `chmod 600`.
3. Create `~/.gradle/gradle.properties` from the password
   manager fields; `chmod 600`.
4. Verify: `cd android && ./gradlew assembleRelease` produces a
   signed APK; `adb install -r` against an existing dev install
   succeeds.
5. Sanity check: if `adb install -r` fails with
   `INSTALL_FAILED_UPDATE_INCOMPATIBLE`, the keystore didn't
   restore correctly — stop and re-fetch from backup before
   uninstalling the old app.

**Acceptance:**
- `docs/release/android-signing-recovery.md` exists and walks
  someone with the password-manager backup from clean machine to
  signed APK.

### A.5 — Server Firebase service account hygiene ⏱ ~10 min docs

**Status: DONE 2026-04-28.** Service-account JSON backed up to
KeePassXC alongside the keystore + AppImage signing key, same
shape (entry-with-attachment + project-ID note).

**Why:** `server/firebase-service-account.json` is a separate
credential giving full FCM-send authority. Already correctly
gitignored. Only the backup story needs writing down.

**What changes:**

Add a short subsection (or separate
`docs/release/server-firebase-recovery.md`) covering:

- Back up the JSON to the password manager (same item layout as
  the keystore — attachment + project-ID note).
- Deployed onto production server via `scp` / pipeline; never
  regenerated on the server.
- If leaked, rotate via Firebase Console (delete service account,
  create new one, redeploy JSON). FCM-send keys have no kill
  switch other than account deletion.

**Acceptance:**
- Doc exists; password-manager backup item is created.

---

## Track B — AppImage interactive build driver (P.1c)

**Why:** the mechanical `build-appimage.sh` is production-ready
and CI-driven, but invoking it from a local laptop currently means
typing `./desktop/packaging/appimage/build-appimage.sh
--source=$PWD --output=/tmp/dc-out` each time and remembering
which path is which. The plan called for `build.sh` as a
single-command interactive driver that wraps it: pick source
(github/local), pick output dir, confirm, run, remember choices
for next time.

**Scope:** new `desktop/packaging/appimage/build.sh` (~200–300
lines bash). Wraps `build-appimage.sh`. State persisted at
`~/.config/desktop-connector-build/state.json`. Hardcoded
`REMOTE_REPO=https://github.com/hawwwran/desktop-connector`. The
mechanical builder doesn't change.

**Out of scope:** anything CI does (`build-appimage.sh` is what CI
calls directly). `build.sh` is laptop-only convenience.

The original P.1c spec (now archived) had the full UX walkthrough.
Steps below are the implementation chunks.

### B.1 — Skeleton + `--help` + `--non-interactive` ⏱ ~30 min

**What changes:**

1. Replace the existing P.1a stub at
   `desktop/packaging/appimage/build.sh` with:
   - `set -euo pipefail` from line 1.
   - `--help` printing a one-screen usage summary.
   - `--non-interactive` flag (parsed; behaviour wired in B.5).
   - Hardcoded `REMOTE_REPO` constant at the top of the file.
   - `trap` cleanup hook (no-op for now; tmp dirs land in B.3).
   - All later prompts wrapped in a `prompt()` helper that
     respects `$NON_INTERACTIVE` (empty-input or fail-loud — wired
     in B.5).

**Acceptance:**
- `./build.sh --help` prints usage, exits 0.
- `./build.sh --non-interactive` exits non-zero (no state file
  yet) with a clear error.
- ShellCheck clean.

### B.2 — Pre-flight checks ⏱ ~30 min

**What changes:**

1. Before any prompt, run pre-flight (one-line error per
   failure, fail fast):
   - Vendored tools present in `.tools/`, or offer
     `Download missing tools now? [Y/n]`.
   - ≥2 GB free in `$TMPDIR` (and in chosen output dir, after
     B.4 runs).
   - Host has Python 3.11+ on `$PATH`.
   - GTK4 + libadwaita 1.5+ headers present (warn-only — actual
     bundling is the mechanical builder's concern).
   - `git` lazy-checked when github source is chosen.
   - Network reachable for github lazy-checked via
     `git ls-remote $REMOTE_REPO HEAD` only when github is
     chosen.

**Acceptance:**
- Missing tool: prompt to download fires; declining exits non-zero.
- All checks pass on the dev laptop without intervention.

### B.3 — Source-choice + path validation ⏱ ~45 min

**What changes:**

1. Source-choice prompt (`g`/`l`, default = saved or `g`).
2. If `local`: prompt for repo path; validate dir exists, has
   `version.json` + `desktop/`, git remote matches `$REMOTE_REPO`
   (or user explicitly confirms a non-canonical remote).
   Validation failure re-prompts — does not exit.
3. If `github`: `git ls-remote $REMOTE_REPO HEAD` prints SHA;
   prompt `Pull latest from main of <REMOTE_REPO> @ <sha>?
   [Y/n]`. Shallow clone (`--depth 1`) into a `mktemp -d` dir;
   `trap` adds it to cleanup list.

**Acceptance:**
- Local path with mismatching remote: prompt fires; both
  branches behave correctly.
- Github path: clones happen, SHA shown matches HEAD on
  remote.
- Cancelling at github confirm re-prompts source choice.

### B.4 — Output dir + confirmation summary + run ⏱ ~30 min

**What changes:**

1. Output-dir prompt (default = saved or `$PWD`); resolve, expand
   `~`, `mkdir -p`, validate writable.
2. Confirmation summary printed before invoking the builder:
   ```
   About to build:
     source:  <local path or github SHA>
     output:  <output dir>
     tools:   .tools/linuxdeploy-x86_64.AppImage (etc.)
   Proceed? [Y/n]:
   ```
3. Invoke `build-appimage.sh --source=<resolved> --output=<dir>`.
   Stream output live; on non-zero exit, leave partial output for
   inspection; do not update state file.
4. On success: print AppImage path + size + SHA-256.

**Acceptance:**
- Successful run prints all three pieces of summary info.
- Mid-run kill (Ctrl-C): tmp clones cleaned by trap; state file
  unchanged.

### B.5 — State persistence + `--non-interactive` wiring ⏱ ~30 min

**What changes:**

1. State file `~/.config/desktop-connector-build/state.json`:
   ```json
   {
     "last_source": "github" | "local",
     "last_local_path": "/abs/path",
     "last_output_dir": "/abs/path"
   }
   ```
   Created on first successful run; updated after each success.
   Parse failure → treat as empty (never crash).
2. `--non-interactive` resolves every prompt to "use saved
   default or fail loudly with clear error"; never reads from
   tty.

**Acceptance:**
- Run twice — second run accepts all defaults with Enter and
  produces a byte-identical AppImage modulo `SOURCE_DATE_EPOCH`.
- `./build.sh --non-interactive` with no state file exits
  non-zero with a clear message ("no saved build state — run
  interactively once first").
- `./build.sh --non-interactive` with state present runs to
  completion silently.

### B.6 — Idempotence + cleanup verification ⏱ ~15 min

**What changes:**

Final pass — no new code, just verification:

- Re-run safety: identical answers → identical AppImage (modulo
  `SOURCE_DATE_EPOCH` until pinned in CI).
- Mid-run failure: tmp clones / partial outputs cleaned;
  state file untouched.
- First-ever run with no state: defaults to `github`, prompts
  for output dir with `$PWD` default — never crashes on
  missing state.
- `--help`, `--non-interactive`, success path, failure path —
  each exercised once.

**Acceptance:**
- All four scenarios pass manually.
- Doc one-liner added to `desktop/packaging/appimage/README.md`:
  "Use `./build.sh` for interactive local builds. CI uses
  `build-appimage.sh` directly."

---

## Suggested order

1. **A.1** first — flips the security gap closed in 30 minutes.
   Land alone; commit when ready (per the always-ask-before-commit
   rule).
2. **A.3 + A.4 + A.5** — backup / docs sweep. ~40 minutes
   together. Independent of A.1 but most useful right after it.
3. **A.2** — optional rotation. Only worth it if you want a clean
   break from the historically-leaked weak password. Can land any
   time after A.1.
4. **B.1 → B.6** — AppImage build driver. Pure UX polish; no time
   pressure. Land sub-step at a time as appetite allows.

After both tracks land, this file can be archived to
`temp/finished-plans/` like its predecessors.
