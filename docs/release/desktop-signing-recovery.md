# Desktop Connector — AppImage release signing key

Recovery + operations runbook for the GPG key that signs the
AppImage and `SHA256SUMS` published by the
[`desktop-release` workflow](../../.github/workflows/desktop-release.yml).

Companion to [`android-signing-recovery.md`](android-signing-recovery.md),
which covers the Android APK signing key. The rules here are specific to
the desktop signing key, but the storage pattern (password manager primary,
encrypted secondary, two physically distinct copies) is the same.

## Identity

| Field | Value |
|---|---|
| Name | `Desktop Connector Releases` |
| Email | `github@hawwwran.com` |
| Comment | `desktop-connector AppImage signing key` |
| Fingerprint | `FBEFCEC13D7AEC0810812975491C904390F4E03B` |
| Algorithm | Ed25519 (EDDSA), sign-only — no encryption subkey |
| Created | 2026-04-25 |
| Expires | 2029-04-24 (3 years; renew per "Renewing expiry" below) |

The fingerprint is the canonical identifier. Anyone receiving a release
should verify against this fingerprint, not the short key id.

### Supported distros

The released AppImage is built on `ubuntu-24.04` (glibc 2.39). Coverage
floor: **Zorin 17+, Mint 22+, Pop! 24.04+, Ubuntu 24.04+, Debian 13+,
Fedora 40+**. Earlier releases (Ubuntu 22.04 / Mint 21 / Zorin 16)
should use `install-from-source.sh` (apt+pip path) — the AppImage
won't run on glibc < 2.39.

Manual smoke checklist before announcing a release widely: at minimum
one Ubuntu, one Mint or Zorin, and one Fedora install. Sign-off
recorded against the tag in the release notes.

## Where the materials live

| Material | Location | Sensitive? |
|---|---|---|
| Public key (`.asc`) | `docs/release/desktop-signing.pub.asc` in this repo + password manager backup | No — published. |
| Private key (armoured `.asc`) | Password manager only. | **Yes**, in combination with the passphrase. |
| Passphrase | Password manager only — `passphrase` field on the same item. | **Yes**, in combination with the private key. |
| Revocation certificate (`.rev`) | Originally at `~/.gnupg/openpgp-revocs.d/FBEFCEC13D7AEC0810812975491C904390F4E03B.rev` on the machine that generated the key. **Move it to the password manager** as a separate attachment. | **Yes** — anyone with this can revoke the key. |
| `DESKTOP_SIGNING_KEY` (GitHub Actions secret) | `Settings → Secrets and variables → Actions` on `hawwwran/desktop-connector`. Holds the full armoured private key. | **Yes**. Read-only from CI; not viewable from the UI after upload. |
| `DESKTOP_SIGNING_PASS` (GitHub Actions secret) | Same UI, separate secret. Holds the passphrase. | **Yes**. Same visibility model. |

Rule: at least two physically distinct copies of the private key + passphrase + revocation cert. Password manager (online, replicated) plus one of: encrypted USB stick in a drawer, encrypted 7z (`7z a -p -mhe=on backup.7z desktop-signing.priv.asc`) on a cloud drive, `git-crypt`-protected private repo. If you only have the password manager and your account is locked, you've lost signing.

## Verifying a release locally

Once the public key is in your local keyring, every release on
[GitHub Releases](https://github.com/hawwwran/desktop-connector/releases)
that ships a `.AppImage.sig` file can be verified end-to-end:

```bash
# One-time: import the public key
gpg --import docs/release/desktop-signing.pub.asc

# Per release: download AppImage + .sig + SHA256SUMS, then:
gpg --verify desktop-connector-X.Y.Z-x86_64.AppImage.sig \
            desktop-connector-X.Y.Z-x86_64.AppImage
gpg --verify SHA256SUMS.sig SHA256SUMS
sha256sum -c SHA256SUMS
```

`gpg --verify` should print `Good signature from "Desktop Connector Releases …"`.
The trust warning (`This key is not certified with a trusted signature!`) is harmless —
it means you haven't cross-signed this key from another personal identity.

## Provisioning a new development machine

Almost never needed — CI signs releases. Local installs of the private key
are only required when:

- You want to verify releases from your own checkout against the public key.
- The CI signing path is broken and you want to sign one release manually
  from the dev machine while you fix CI (the "lost CI" recovery).

Steps to restore on a fresh machine:

```bash
# Public key from the repo
gpg --import docs/release/desktop-signing.pub.asc

# Private key from your password manager (download the .asc attachment first)
gpg --import desktop-signing.priv.asc   # asks for the passphrase

# Sanity-check: smoke a sign + verify against the imported keys
echo hi > /tmp/in.txt
gpg --pinentry-mode loopback --passphrase '<from password manager>' \
    --local-user FBEFCEC13D7AEC0810812975491C904390F4E03B \
    --detach-sign /tmp/in.txt
gpg --verify /tmp/in.txt.sig /tmp/in.txt   # expect "Good signature"
```

## Trust model — install vs update

Two different verification paths cover two different attack surfaces.
This section is the explicit map.

### Install (`install.sh` path)

`desktop/install.sh` does end-to-end GPG verification before placing
or running anything:

1. Fetch the public key from `raw.githubusercontent.com/.../main/docs/release/desktop-signing.pub.asc`.
2. Compute its fingerprint, compare against the literal hardcoded
   in `install.sh` (`FBEF CEC1 3D7A EC08 1081 2975 491C 9043 90F4 E03B`).
   Mismatch → refuse to proceed.
3. Import into a throwaway `GNUPGHOME`, fetch the AppImage + the
   detached `.sig`, run `gpg --batch --verify` against it.
4. Only on success: move the AppImage into the canonical location,
   `chmod +x`, launch.

This catches: post-CI tampering of release artefacts on
`releases.github.com` (releases assets can be edited by anyone with
repo write access — but the signing private key is only in CI
secrets, so any post-CI edit invalidates the signature). It does
NOT catch: a repo-level compromise that updates BOTH `install.sh`
and the public key file together — that's the same root of trust as
`curl | bash` itself, called out in the script's header comment.

### In-app update (`AppImageUpdate` / tray "Check for updates")

P.6's in-app updater wraps the bundled `appimageupdatetool`. Its
verification model is **different**: it relies on zsync block-hash
verification anchored in the running AppImage's embedded `.zsync`
URL, not a fresh `gpg --verify`.

Concretely: `zsyncmake` builds the `.zsync` metadata at release
time (in CI, after the AppImage is built and signed). The metadata
contains SHA-1 hashes of every 2 KB block of the published AppImage,
plus a master SHA-256. `appimageupdatetool` fetches the new
`.zsync`, computes which blocks of the local AppImage already match,
HTTP Range-fetches the missing blocks, assembles the new AppImage,
verifies the master SHA-256 + per-block hashes match. The `.zsync`
URL is hardcoded into the AppImage at build time (via
`zsyncmake -u <url>`); changing it requires re-building the AppImage,
which requires re-signing.

Two release-pipeline details are load-bearing here:

1. **`UPDATE_INFORMATION` embedded into the AppImage at pack time**
   (the release workflow's "Pin UPDATE_INFORMATION" step → `appimagetool -u`).
   Without this the bundled `appimageupdatetool` exits 2 with
   "Could not find update information" — the `.zsync` sidecar alone
   isn't enough; AppImageUpdate reads the in-binary update string to
   decide where to fetch the `.zsync` from. The string format is
   `gh-releases-zsync|hawwwran|desktop-connector|desktop-latest|desktop-connector-*-x86_64.AppImage.zsync`.

2. **Rolling `desktop-latest` GitHub Release** (stream isolation).
   AppImageUpdate's GitHub provider only accepts literal tag values
   (`latest`, `latest-pre`, or an exact tag string) — no wildcards.
   Using `latest` would break desktop in-app updates whenever a
   chronologically-newer `android/v*` release shadows the desktop
   one (the asset glob 404s). To dodge that, the release workflow
   force-updates a rolling `desktop-latest` GitHub Release on each
   `desktop/v*` build, hosting only the `.zsync` (+ SHA256SUMS for
   integrity reference). The `.zsync`'s `URL:` header still points
   back at the versioned release's AppImage, so zsync2 fetches the
   actual ~150 MB bytes from there. Storage cost: ~150 KB extra per
   release. Anyone deleting `desktop-latest` after a release breaks
   in-app updates until the next desktop release re-creates it.

A third detail lives on the runtime side: `appimageupdatetool` writes
the new bytes at the `.zsync`'s `Filename:` header (the published
asset name, e.g. `desktop-connector-0.2.2-x86_64.AppImage`), which
differs from our canonical install path (`desktop-connector.AppImage`).
`update_runner.py` parses the tool's "New file created: `<PATH>`"
output line and, when the path differs from `$APPIMAGE`, atomically
relocates the new bytes onto the canonical path (with a `.zs-old`
backup) so the install hook's stable path keeps pointing at the
current bytes.

This catches: corrupted downloads, MITM that modifies blocks
mid-flight, GitHub Releases tampering with just the AppImage (the
zsync hashes won't match). It does NOT catch: an attacker who
replaces both the AppImage and the `.zsync` atomically on
`releases.github.com`. Defending against that requires either:

(a) re-doing GPG signature verification on every update (not what
    AppImageUpdate does today), or
(b) a repo-write-access compromise (which would also let the
    attacker control the next `install.sh` fetch — same root of
    trust as the install path).

We accept the asymmetric model because (a) requires a substantial
patch to `appimageupdatetool` and (b) is already covered by the
install-time root of trust. **If the AppImage update path ever
needs to be hardened to (a), it would be its own dedicated change
captured in a new plan doc.**

### Re-running `install.sh` as a re-verify

A user who wants to manually re-anchor trust can re-run
`install.sh`:

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
```

This pulls a fresh AppImage + `.sig` and verifies before placing.
Equivalent to the original install. Useful as an out-of-band
re-verification after a long sequence of in-app updates, or after
a security advisory recommends it.

## Re-uploading the GitHub Actions secrets

If GitHub somehow loses the `DESKTOP_SIGNING_KEY` / `DESKTOP_SIGNING_PASS`
secrets (revoked, accidentally deleted, transferred ownership), restore from
the password manager:

1. **Settings → Secrets and variables → Actions** on the repo.
2. `DESKTOP_SIGNING_KEY` → **Update** → paste the full content of
   `desktop-signing.priv.asc` (begins with `-----BEGIN PGP PRIVATE KEY BLOCK-----`).
3. `DESKTOP_SIGNING_PASS` → **Update** → the passphrase from the same item.

The key itself is unchanged, so all previously published releases continue
to verify with the same public key.

## Lost-private-key recovery (no password manager backup)

You can no longer sign new releases under this identity. Two paths, neither
ideal:

### a) Revoke + replace

1. Locate the revocation certificate from the password manager attachment
   (`<FP>.rev`).
2. Import + apply it to your keyring:
   ```bash
   gpg --import FBEFCEC13D7AEC0810812975491C904390F4E03B.rev
   ```
3. Export the now-revoked public key and overwrite the repo copy:
   ```bash
   gpg --armor --export FBEFCEC13D7AEC0810812975491C904390F4E03B \
     > docs/release/desktop-signing.pub.asc
   ```
4. Generate a new key with `~/temp-scripts/026-appimage-gpg-signing-key.sh`
   (will prompt to remove the revoked key first).
5. Update GitHub Actions secrets with the new private key + passphrase.
6. Cut a release that ships the new public key, alongside a notice that the
   old fingerprint `FBEF CEC1 3D7A EC08 …` is revoked. Old releases still
   verify with the (now-revoked) old public key — `gpg --verify` will report
   the signature as good *but the key as revoked*.

### b) Stop signing for a release cycle

Less destructive — ship one or two unsigned releases while you regenerate
the key. Document `SHA256SUMS` verification by other means (Actions run's
published SHA in the UI). Resume signing in the next release with a fresh
key + new public-key file. Mention the gap in the changelog.

## Leaked-private-key emergency rotation

Threat: someone has both `desktop-signing.priv.asc` and the passphrase. They
can sign anything as the project, and users who trust the old public key
will accept it.

1. **Revoke immediately** using the revocation cert (see step 1 above).
   Push the revoked public key to the repo so anyone re-cloning gets the
   revocation status. Add a SECURITY advisory in `SECURITY.md` and the next
   release's notes.
2. Generate a new keypair (the keygen script).
3. Update `DESKTOP_SIGNING_KEY` + `DESKTOP_SIGNING_PASS` GitHub secrets.
4. Cut a new release **with the new public key shipped in the AppImage's
   own `docs/release/desktop-signing.pub.asc`** so the in-app updater
   (P.6) can pivot to the new key without trusting any external service.
5. Old releases' AppImage files on `releases.github.com` continue to verify
   with the *revoked* old key — the SHA256 chain in `SHA256SUMS` for those
   releases is unchanged. A user re-downloading an old release won't get a
   different file, but they will get a `revoked` warning on `gpg --verify`.

## Renewing expiry (every ~3 years)

The key expires 2029-04-24. Renewal does not change the fingerprint —
existing releases continue to verify, new releases keep being signed by
the same identity.

```bash
gpg --edit-key FBEFCEC13D7AEC0810812975491C904390F4E03B
> expire
# pick a duration, e.g. 3y
> save
```

Then re-export the public key and commit:

```bash
gpg --armor --export FBEFCEC13D7AEC0810812975491C904390F4E03B \
  > docs/release/desktop-signing.pub.asc
git add docs/release/desktop-signing.pub.asc
git commit -m 'docs(release): renew signing key expiry to YYYY-MM-DD'
```

The CI-stored private key embeds the same expiry timestamp internally;
re-export the private key and update `DESKTOP_SIGNING_KEY` to keep CI in sync:

```bash
gpg --armor --export-secret-keys \
    --pinentry-mode loopback --passphrase '<from password manager>' \
    FBEFCEC13D7AEC0810812975491C904390F4E03B > /tmp/priv.asc
# Settings → Secrets → DESKTOP_SIGNING_KEY → Update → paste contents
shred -u /tmp/priv.asc
```

## Anti-checklist

- **Don't** commit the private key, even encrypted. Repos leak. CI secrets are the only durable home outside the password manager.
- **Don't** put the passphrase in the repo, in `~/.bashrc`, in shell history, in a CI variable named anything but `DESKTOP_SIGNING_PASS`, or in the keygen script's source after running it.
- **Don't** echo the passphrase in scripts (`set -x` would leak it; the keygen script disables that for its sensitive section).
- **Don't** rotate the key proactively. Every rotation forces users to re-trust a new fingerprint and adds a chance for someone to ship a malicious update under a "we rotated, trust this new key!" pretext. Rotate only on expiry (every 3 years) or on a confirmed leak.
- **Don't** delete the revocation cert from the password manager — it's the one tool that still works after a private-key loss. Treat it with the same care as the private key itself.
