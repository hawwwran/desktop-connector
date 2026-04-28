# Desktop Connector — Android release signing key

Recovery + operations runbook for the keystore that signs the
Android APKs published as GitHub Releases.

Companion to
[`docs/release/desktop-signing-recovery.md`](desktop-signing-recovery.md)
which covers the parallel desktop AppImage GPG key. Both follow the
same storage pattern (KeePassXC primary, two physically distinct
copies overall).

## Identity

| Field | Value |
|---|---|
| Keystore alias | `desktop-connector` |
| Keystore type | PKCS12 (modern default since JDK 9). Store password and key password are the same value; `keytool -keypasswd` is unsupported. Both gradle properties hold the same string. |
| Owner DN | `CN=Desktop Connector, O=hawwwran` |
| Algorithm | RSA 4096 |
| Created | 2026-04-15 |
| Valid until | 2053-08-31 (~27 years; effectively single-use) |
| File SHA-256 | `dedb3605f31e5fd49de967fa90c2643a39c115ce6a7e6b5fcae54375d1383176` |
| Cert SHA-256 | `42:6B:D5:F1:3D:38:36:8F:3F:60:71:DB:73:2D:5B:C9:02:AB:29:0B:A5:78:C3:E3:2C:6A:21:29:7E:9C:68:40` |
| Cert SHA-1 | `2E:A5:7B:9C:91:BB:19:A0:0F:73:A3:03:26:6E:60:75:3F:73:60:E7` |

The **certificate SHA-256** is what Android compares on update.
Any APK signed by a key with this cert SHA-256 is treated as the
same publisher; sideload `adb install -r` succeeds. Lose the
keystore → no future APK can match this fingerprint → installed
users cannot receive updates without uninstalling first (which
wipes paired-device state and history).

## Where the materials live

| Material | Location | Sensitive? |
|---|---|---|
| `keystore.jks` (binary) | `android/keystore.jks` in the repo (gitignored via `android/.gitignore:10`) | **Yes** — private key. |
| `~/.gradle/gradle.properties` (passwords) | Per-machine, outside the repo (`chmod 600`). Holds `DC_KEYSTORE_PASSWORD`, `DC_KEY_ALIAS`, `DC_KEY_PASSWORD`. | **Yes**, in combination with the keystore. |
| Backup of both | KeePassXC entry `Desktop Connector — Android signing` (group `Desktop Connector`). Keystore + properties file as attachments; passwords in main + protected custom-attribute fields; verification fingerprints in notes. | **Yes**. |

Rule: at least two physically distinct copies of the keystore +
passwords. KeePassXC database (replicated to one cloud / second
device) plus one of: encrypted USB stick in a drawer, encrypted
7z (`7z a -p -mhe=on backup.7z keystore.jks`) on a cloud drive,
`git-crypt`-protected private repo. If you only have one and the
device dies, you've lost signing.

There is **no GitHub Actions signing path** for Android yet —
release APKs are built and signed locally on the developer's
machine. If/when that changes, GitHub Actions secrets
(`secrets.DC_KEYSTORE_*` plus the keystore as a base64-encoded
secret) join the inventory above. See "Anti-checklist" below.

## Verifying a build locally

```bash
# Inspect the live keystore (requires DC_KEYSTORE_PASSWORD)
keytool -list -v \
    -keystore android/keystore.jks \
    -alias desktop-connector \
    -storepass "$(awk -F= '/DC_KEYSTORE_PASSWORD/ {print $2}' ~/.gradle/gradle.properties)"

# Build a release APK
cd android && ./gradlew assembleRelease

# Verify the APK is signed with the canonical key
APK=android/app/build/outputs/apk/release/Desktop-Connector-*-release.apk
apksigner verify --print-certs $APK
```

`apksigner` should print:

```
Signer #1 certificate DN: CN=Desktop Connector, O=hawwwran
Signer #1 certificate SHA-256 digest: 426bd5f13d38368f3f6071db732d5bc902ab290ba578c3e32c6a21297e9c6840
```

The SHA-256 digest must match the **Cert SHA-256** in the Identity
table above (just stripped of colons + lowercased). Any other
value means the build picked up the wrong keystore — stop and
investigate before installing.

## Provisioning a new development machine

Done after a wipe / OS reinstall / new laptop. Reproducible
checklist; runtime ~10 minutes.

1. **Clone the repo**:
   ```bash
   git clone https://github.com/hawwwran/desktop-connector
   cd desktop-connector
   ```
2. **Install Android SDK** (Android Studio sdkmanager, or
   standalone). Set `ANDROID_HOME` env var. First Android Studio
   open auto-creates `android/local.properties` with `sdk.dir=…`.
3. **Restore `keystore.jks` from KeePassXC**:
   - Open KeePassXC → entry `Desktop Connector — Android signing`.
   - Right-click the `keystore.jks` attachment → **Save Attachment**
     → save to `android/keystore.jks`.
   - `chmod 600 android/keystore.jks`.
   - Verify integrity:
     ```bash
     sha256sum android/keystore.jks
     # Must match: a49a15daeab30e50779e6951de23e334f7786286ab0ca805ba7360b2f734071e
     ```
     If the hash mismatches, the attachment is wrong — stop, do
     not proceed, re-fetch.
4. **Restore `~/.gradle/gradle.properties` from KeePassXC**:
   - From the same entry, save the `gradle.properties` attachment
     to `~/.gradle/gradle.properties` (create `~/.gradle/` first
     if missing).
   - `chmod 600 ~/.gradle/gradle.properties`.
   - The file content matches the entry's main password +
     protected attributes, so manual recreation also works:
     ```properties
     DC_KEYSTORE_PASSWORD=<entry's main Password field>
     DC_KEY_ALIAS=desktop-connector
     DC_KEY_PASSWORD=<entry's protected keyPassword attribute>
     ```
5. **(Optional) Firebase client config** — if you want
   embedded-FCM builds, download `google-services.json` from
   the Firebase Console and place at
   `android/app/google-services.json`. Without it, FCM is pulled
   dynamically from the server's `/api/fcm/config` at runtime
   (current code path), so this step is only useful as a fallback.
6. **Build a signed release**:
   ```bash
   cd android && ./gradlew assembleRelease
   ```
   APK lands at `app/build/outputs/apk/release/Desktop-Connector-*-release.apk`.
7. **Sanity check on a real device**:
   ```bash
   adb install -r app/build/outputs/apk/release/Desktop-Connector-*-release.apk
   ```
   - **Success** = the keystore restored correctly. Same signing
     identity → existing install accepts the update.
   - **Failure with `INSTALL_FAILED_UPDATE_INCOMPATIBLE`** =
     the keystore did NOT restore correctly. Stop. Re-fetch from
     KeePassXC; verify SHA-256 again. Do not uninstall the old
     app to "fix" the install error — that loses paired-device
     state and history, and doesn't help if the keystore is
     genuinely wrong.

## Recovery if the keystore is lost

If both KeePassXC and the secondary backup are gone:

1. Generate a new keystore (note: this **breaks updates for all
   currently-installed users** — they must uninstall + reinstall):
   ```bash
   keytool -genkey -v \
       -keystore android/keystore.jks \
       -keyalg RSA -keysize 4096 -validity 10000 \
       -alias desktop-connector
   ```
2. Pick new passwords. Update `~/.gradle/gradle.properties`.
3. Compute fresh fingerprints (`sha256sum android/keystore.jks` +
   `keytool -list -v …`) and update **this document's Identity
   table** + the KeePassXC entry's notes.
4. Update the password-manager item with the new keystore +
   passwords.
5. Document in the next release notes that existing installs
   must uninstall + reinstall to get updates. This is acceptable
   only because the project's audience is small (sideload-only,
   personal use). If/when the project goes to Play Store, opt
   into [Play App Signing](https://developer.android.com/studio/publish/app-signing#app-signing-google-play)
   on first upload — Google holds the upload key and lets you
   rotate; loss of the local keystore stops being terminal.

## Renewing the keystore validity

The keystore is valid until 2053-08-31. No action needed before
then. When/if validity needs extending, generate a new keystore
(see "Recovery if the keystore is lost" — the user impact is the
same as a lost-key recovery, since the certificate identity
changes).

## Anti-checklist — things this runbook does NOT cover

- **CI signing.** No GitHub Actions release workflow yet for
  Android. When that lands, passwords move to repository secrets
  (`secrets.DC_KEYSTORE_PASSWORD`, etc.) and the keystore file
  becomes a base64-encoded secret decoded into place during the
  workflow. Update this doc's "Where the materials live" table
  at that point.
- **Play Store / Play App Signing.** If the app ever goes to
  Play Store, opt into Play App Signing on first upload — Google
  holds the upload key and lets you rotate your own. Not
  relevant while distribution is sideload-only.
- **Desktop AppImage signing.** Different key, different
  runbook: [`desktop-signing-recovery.md`](desktop-signing-recovery.md).
- **Server Firebase service account.** Different credential
  (`server/firebase-service-account.json`); back it up the same
  way (KeePassXC entry with attachment) but it lives on the
  production server, not this laptop.
