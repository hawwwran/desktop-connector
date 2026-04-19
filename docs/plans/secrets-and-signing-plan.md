# Secrets & release-signing hygiene

Two related problems to close:

1. **Committed-source hygiene.** One file (`android/app/build.gradle.kts`) still has signing passwords hardcoded. Move them out so the repo carries zero secrets even if the build config is inspected.
2. **Release-signing continuity.** Define how release APKs get signed, how the keystore + passwords are kept safe, and how the whole setup survives wiping this machine or moving to a new one — without losing the ability to ship updates to the same installed-app identity.

Also covers the parallel credentials the server needs (Firebase service account) and how those travel to deployment targets.

Not in scope: desktop at-rest secrets (`auth_token`, paired-device keys, private key in `~/.config/desktop-connector/`) — those are covered by `docs/plans/hardening-plan.md`, a different axis (runtime files, not committed source or signing artifacts).

## Current sensitive-data inventory

Audited 2026-04-19 across the repo.

| Artifact | Location | Committed? | Secret? | Loss consequence |
|---|---|---|---|---|
| Android release keystore | `android/keystore.jks` | No (gitignored in `android/.gitignore:10`) | **Yes** — private key | Cannot ship signed updates under the same identity. Sideload users must uninstall+reinstall, losing paired device state. |
| Android signing passwords | `android/app/build.gradle.kts:26-28` (`storePassword`/`keyPassword` both `"desktopconnector"`) | **Yes** — plaintext in source | **Yes** (paired with keystore file) | Anyone with the keystore file can sign as you with no further friction. |
| Android Firebase client config | `android/app/google-services.json` | No (gitignored) | Config, not secret (Firebase sender-identifier) | Re-download from Firebase Console. |
| Android SDK path | `android/local.properties` | No (gitignored) | Not secret | Regenerate via Android Studio. |
| Server Firebase service account | `server/firebase-service-account.json` | No (gitignored via root `.gitignore`) | **Yes** — GCP service account JSON with full FCM send privileges | Without it, server can't wake phones via FCM push. Re-download from GCP/Firebase Console works, but requires the Firebase project owner account. |
| Server Firebase client config | `server/google-services.json` | No (gitignored) | Config, not secret | Re-download. |
| Server SQLite DB | `server/data/connector.db` | No (gitignored via `server/data/`) | Contains device `auth_token`s | Devices need to re-pair. Not end-of-world, but annoying. |
| Server encrypted blobs | `server/storage/` | No (gitignored) | Already encrypted end-to-end | No real secret lives here; server is a blind relay. |

Nothing else hits the pattern. Desktop client source is clean. Android source has no baked-in credentials.

## Phase 1 — Move Android signing passwords out of `build.gradle.kts`

**Goal:** make the repo carry zero secrets. A leak of the git history alone should be uninteresting.

**Approach:** read passwords from Gradle properties, with the actual values living in `~/.gradle/gradle.properties` (outside the repo, per-user config that Gradle reads automatically for every build on this machine).

Edit `android/app/build.gradle.kts`:

```kotlin
signingConfigs {
    create("release") {
        val ks = rootProject.file("keystore.jks")
        if (ks.exists()) {
            storeFile = ks
            storePassword = (project.findProperty("DC_KEYSTORE_PASSWORD") as? String) ?: ""
            keyAlias = (project.findProperty("DC_KEY_ALIAS") as? String) ?: "desktop-connector"
            keyPassword = (project.findProperty("DC_KEY_PASSWORD") as? String) ?: ""
        }
    }
}
```

Then create `~/.gradle/gradle.properties` with:

```properties
DC_KEYSTORE_PASSWORD=<the store password>
DC_KEY_ALIAS=desktop-connector
DC_KEY_PASSWORD=<the key password>
```

Gradle reads `~/.gradle/gradle.properties` automatically for every build. No command-line flags, no env-var shell ritual. File permissions: `chmod 600 ~/.gradle/gradle.properties` (just you).

If the properties are missing, `findProperty` returns `null`, the fallback `""` is used, and Gradle's signing step will fail with a clear "invalid keystore password" error rather than silently producing an unsigned APK.

**Password rotation opportunity:** while editing, pick a new, stronger store + key password (different from the current `"desktopconnector"`). Change the keystore passwords with `keytool`:

```bash
keytool -storepasswd -keystore android/keystore.jks
keytool -keypasswd -alias desktop-connector -keystore android/keystore.jks
```

Then update `~/.gradle/gradle.properties` to match.

**Commit plan:**
- Change `build.gradle.kts` to the property-reading form.
- Add `android/app/build/` continues to be gitignored (existing behavior).
- `~/.gradle/gradle.properties` is OUTSIDE the repo — nothing to commit for it.

## Phase 2 — Keystore backup and machine-migration strategy

The keystore is THE long-lived identity of the Android app. Losing it means:
- Sideload updates to an existing install fail with `INSTALL_FAILED_UPDATE_INCOMPATIBLE`. User must uninstall the old app first, losing paired state and history.
- If ever published to Play Store without Play App Signing opt-in, there is literally no recovery — you'd have to ship a new app listing under a new package ID.

So: back up the keystore, redundantly, with the passwords alongside.

### Recommended primary backup: password manager

Both 1Password and Bitwarden support file attachments on vault items plus secure notes. A single "Desktop Connector Android signing" item with:

- Attached file: `keystore.jks`
- Field: `storePassword`
- Field: `keyPassword`
- Field: `keyAlias` = `desktop-connector`
- Note: "Android release signing keystore. Losing this = cannot update installed app under same identity. Re-create via `keytool -genkey -v -keystore keystore.jks -keyalg RSA -keysize 4096 -validity 10000 -alias desktop-connector` only as a last resort (forces user uninstall+reinstall)."

Vault is already encrypted at rest and replicated across your devices. This is the cleanest home.

### Secondary backup: offline encrypted copy

One of:

- USB stick in a drawer with the keystore + a plain-text `passwords.txt` inside a Cryptomator/VeraCrypt volume.
- Encrypted 7z (`7z a -p -mhe=on backup.7z keystore.jks passwords.txt`) uploaded to a cloud drive you control.
- `git-crypt`-protected private repo containing the same bundle.

Rule: at least two physically distinct copies (vault + cloud, or vault + USB). If you only have one and it dies, you're back to the loss scenario.

### Re-creation last resort

If both backups are gone:

```bash
keytool -genkey -v -keystore android/keystore.jks -keyalg RSA -keysize 4096 \
  -validity 10000 -alias desktop-connector
```

Pick new passwords. Update `~/.gradle/gradle.properties`. But note: anyone with the OLD app installed cannot receive updates signed by the NEW key. Document that this is acceptable for this app's audience (you, plus maybe a handful of testers) before doing it.

## Phase 3 — Provisioning a new development machine

After wiping / switching OS / new laptop — the reproducible checklist to get back to signed release builds.

1. **Clone the repo**: `git clone https://github.com/hawwwran/desktop-connector`.
2. **Install Android SDK**: Android Studio or `sdkmanager`. `ANDROID_HOME` env var points at it. Create `android/local.properties` with `sdk.dir=<path>` (auto-generated by Android Studio on first open).
3. **Restore the keystore**:
   - From password manager: download the attached `keystore.jks`, place at `android/keystore.jks`, `chmod 600`.
4. **Restore signing passwords**: create `~/.gradle/gradle.properties` (make sure `~/.gradle/` exists first):
   ```properties
   DC_KEYSTORE_PASSWORD=<from password manager>
   DC_KEY_ALIAS=desktop-connector
   DC_KEY_PASSWORD=<from password manager>
   ```
   `chmod 600 ~/.gradle/gradle.properties`.
5. **Optional — Firebase client config**: if you want FCM-enabled builds, download `google-services.json` from the Firebase Console → Android app → place at `android/app/google-services.json`. Without it, FCM is dynamically pulled from the server's `/api/fcm/config` at runtime (current code path), so this step is only for embedded fallback if you later add one.
6. **Test**: `cd android && ./gradlew assembleRelease`. If signing passwords are correct, APK lands at `app/build/outputs/apk/release/*.apk` and is installable with `adb install -r`.
7. **Install-on-device sanity**: if the device already has the app installed from the previous machine's build, `adb install -r` should succeed (same signature). If it fails with `INSTALL_FAILED_UPDATE_INCOMPATIBLE`, the keystore didn't restore correctly — stop and fix before uninstalling the old app.

Keep this checklist updated when anything about the build environment changes.

## Phase 4 — Server Firebase service account hygiene

Separate but related credential: `server/firebase-service-account.json` is a GCP service account JSON giving full FCM-send authority for the Firebase project. Already correctly gitignored, but worth writing down:

- Back it up the same way as the keystore (password manager attachment is ideal).
- It is deployed onto the production server via `scp` / deploy pipeline, NOT regenerated there.
- If leaked, rotate via Firebase Console (delete the service account, create a new one, redeploy the new JSON). FCM-send keys have no kill switch other than deleting the account.
- Document in the password manager item which Firebase project it belongs to (Firebase Console → Project Settings → project ID).

## Phase 5 — One-time repo-history hygiene (optional, low-priority)

After Phase 1 lands, `android/app/build.gradle.kts` no longer contains secrets in its current state — but git history still contains the old `"desktopconnector"` password in every commit up to and including that change. Threat model check: this password is only meaningful when paired with the `keystore.jks` file, which has never been committed. So an attacker with only git-history access cannot do anything with that password.

Therefore: **do not rewrite history** for this. The cost (force-push, collaborator disruption) outweighs the marginal benefit. If the keystore itself had ever been committed, that would be a different call and would require `git filter-repo` plus credential rotation.

Note this decision in the commit message when landing Phase 1, so it's clear why the history was left alone.

## Anti-checklist — things this plan is NOT doing

- **CI signing.** No GitHub Actions release workflow yet. When that lands, passwords move to repository secrets (`secrets.DC_KEYSTORE_PASSWORD`) and the keystore file becomes a base64-encoded secret decoded into place during the workflow. Not needed until there's an actual CI target.
- **Play Store / Play App Signing.** If the app ever goes to Play Store, Google's Play App Signing lets them hold the upload key and lets you rotate your own. Opt in on first upload for an escape hatch. Not relevant while distribution is sideload-only.
- **Desktop secrets.** Covered by `hardening-plan.md`. Different problem: runtime files, not committed source.
- **Server secrets beyond Firebase.** The SQLite DB contains per-device `auth_token`s, but those are already gitignored (via `server/data/`) and are recoverable by re-pairing devices. Not worth a dedicated ritual.
