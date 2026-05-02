# Desktop Connector Vault — Critical Implementation Risks and Weaknesses

Status: risk review  
Scope: Desktop Connector Vault  
Target use case: smaller datasets of critical data, not huge archives.

---

## 1. Product intent

Desktop Connector Vault is not intended to compete with large-scale backup systems for huge datasets.

The primary target should be:

```text
KeePassXC databases
password vault exports
SSH keys
certificates
recovery codes
critical PDFs
contracts
identity documents
small project folders
configuration backups
important family or business documents
```

The feature should be optimized for:

```text
confidentiality
integrity
recoverability
safe restore
disaster management
low operational complexity
clear user decisions
```

It does not need to be optimized first for:

```text
terabytes of media
hundreds of thousands of files
advanced deduplication
complex backup scheduling
enterprise sharing
high-throughput distributed sync
```

For this feature, correctness matters more than raw performance.

---

## 2. Most important risk statement

The biggest implementation risk is not choosing the wrong encryption algorithm.

The biggest risk is this:

```text
The Vault appears to protect critical data,
but a sync, recovery, import, delete, or purge edge case silently destroys or makes that data unrecoverable.
```

For critical small datasets, one lost file can matter more than thousands of successfully synced files.

Therefore the implementation should prefer:

```text
safe failure
explicit user confirmation
version retention
tombstones
integrity checks
browse-only recovery
manual restore
clear diagnostics
```

over:

```text
automatic cleanup
aggressive mirroring
silent overwrite
silent conflict resolution
automatic purge
complex optimization
```

---

# 3. Areas that need special care

## 3.1 Vault key generation

### Why this is crucial

If Vault Master Key generation is weak, every encrypted file can be compromised.

### Required behavior

```text
Vault Master Key must be generated with a cryptographically secure random generator.
It must be 256-bit random material.
It must never be derived directly from a normal user password.
```

### Must not happen

```text
do not use timestamps
do not use device IDs
do not use predictable UUIDs
do not use Math.random-like generators
do not derive the master key directly from a weak passphrase
```

### Test requirement

Add tests that verify:

```text
key length
randomness source
no deterministic output across vault creation
no logging or serialization into normal config
```

---

## 3.2 Recovery envelope

### Why this is crucial

Recovery is the difference between secure disaster recovery and permanent loss.

If recovery is broken, the Vault may work for months and then fail exactly when needed.

### Required behavior

The Vault Master Key should be wrapped by recovery material.

Recommended model:

```text
Vault Master Key = random 256-bit key
Recovery kit / recovery secret = high-entropy material
Recovery passphrase = protects recovery material
```

### Required UX

```text
create recovery material during Vault setup
warn clearly that there is no password reset
offer recovery test
show recovery status
require fresh unlock before changing recovery settings
```

### Must not happen

```text
do not allow silent Vault creation without recovery
do not imply the relay can recover the Vault
do not allow weak passphrase-only recovery without strong warning
do not store recovery passphrase on the relay
do not log recovery material
```

### Test requirement

Test:

```text
correct recovery succeeds
wrong passphrase fails
tampered recovery envelope fails
old recovery envelope remains readable after app update
recovery test does not modify vault state
```

---

## 3.3 Device grants and revocation

### Why this is crucial

Vault access is not the same as normal Desktop Connector device pairing.

A device may be valid for file transfer but should not automatically have Vault access.

### Required behavior

Separate:

```text
Desktop Connector device identity
Vault access authorization
Vault decryption key material
Vault role/permissions
```

### Recommended roles

```text
Admin
Sync
Browse + Upload
Read-only
```

### Important UX copy

When revoking a device:

```text
Revoking this device prevents future Vault access.
It cannot erase data already copied to that device.
```

### Must not happen

```text
do not automatically grant Vault access to every paired device
do not grant purge permission to normal sync devices
do not confuse device removal with cryptographic erasure
```

---

## 3.4 QR-assisted device joining

### Why this is crucial

QR flows are convenient but easy to misuse.

A QR intended for short-lived device joining must not become a permanent recovery secret accidentally exposed to a camera.

### Required separation

There should be separate QR concepts:

```text
Device pairing QR
Vault join QR
Vault recovery QR
```

### Vault join QR

Should contain only short-lived joining data:

```text
relay URL
vault ID
join request ID
ephemeral public key
expiry
```

It must not contain:

```text
Vault Master Key
Recovery Secret
Recovery Passphrase
permanent Vault Access Secret
```

### Recovery QR

If implemented, this is backup material.

It must be treated like a printed master key backup.

### Must not happen

```text
do not put raw Vault Master Key in normal join QR
do not allow reusable join QR forever
do not skip human verification code
do not let unauthenticated relay responses grant Vault access
```

---

## 3.5 Authenticated encryption and nonce safety

### Why this is crucial

The current app uses AES-256-GCM for encrypted transfer payloads. That is fine when nonce discipline is correct. For long-lived persistent vault storage, nonce mistakes are especially dangerous.

### Required behavior

Every encrypted object must use:

```text
unique nonce
authenticated encryption
context-specific associated data
strict decrypt failure handling
```

### Associated data should bind

```text
vault ID
object type
manifest revision
folder ID
file version ID
chunk index
schema version
```

### Must not happen

```text
do not reuse nonce/key pairs
do not decrypt without verifying authentication tag
do not accept ciphertext in the wrong context
do not ignore AEAD failure and continue
```

### Recommendation

For Vault, consider a misuse-resistant AEAD if available and practical:

```text
AES-256-GCM-SIV
XChaCha20-Poly1305 with strong nonce generation
```

If AES-256-GCM remains the implementation choice, nonce handling must be heavily tested.

---

## 3.6 Manifest correctness

### Why this is crucial

The encrypted manifest is the truth of the Vault.

If manifest handling is wrong, files may disappear, versions may become unreachable, or chunks may be purged while still needed.

### Required behavior

Every manifest update should include:

```text
revision number
parent revision
author device
operation ID
timestamp
hash
authenticated encryption
```

Server update must use compare-and-swap:

```text
update from revision N to N+1 only if server is still at N
```

### Must not happen

```text
do not allow last-writer-wins manifest overwrite
do not accept revision rollback silently
do not allow missing parent chain without explicit repair mode
do not publish manifest before all referenced chunks are uploaded
```

### Test requirement

Test:

```text
two devices upload at same time
manifest conflict returns 409
client merges safely
no file version is lost
no delete overrides newer upload silently
```

---

## 3.7 Rollback detection

### Why this is crucial

A malicious or broken relay could serve an older manifest.

For critical data, this can hide recent changes or resurrect old state.

### Required behavior

Each client should remember:

```text
latest known manifest revision
latest known manifest hash
latest known header revision
```

If the relay returns older state:

```text
warn clearly
do not silently accept rollback
offer repair/recovery options
```

### Limitation

A brand-new device restored only from recovery material may not know the latest revision unless another trusted source exists.

This limitation should be documented.

### Must not happen

```text
do not silently accept lower revision than locally remembered
do not let import of old export replace current head by default
```

---

## 3.8 Chunk upload integrity

### Why this is crucial

If a manifest references chunks that were not uploaded correctly, restore fails later.

### Required behavior

Upload flow:

```text
encrypt chunk
upload chunk
verify server stored expected ciphertext size/hash
only then publish manifest referencing chunk
```

### Must not happen

```text
do not publish manifest before chunks exist
do not treat partial chunk upload as complete
do not ignore failed chunk verification
```

### Test requirement

Test:

```text
upload interruption before manifest update
upload interruption after chunks but before manifest
upload interruption during chunk
server loses chunk
download detects missing chunk
integrity check reports missing chunk
```

---

## 3.9 Import and merge

### Why this is crucial

Import is a major attack and data-loss surface.

A user may import an old export into a newer Vault. The app must not roll back or erase newer data by default.

### Required behavior

Import should:

```text
open protected export
verify export integrity
compare Vault identity
detect same Vault vs different Vault
merge by default
preserve both sides
never delete target-only data by default
```

### If target Vault exists

```text
same Vault identity:
  allow merge

different Vault identity:
  refuse automatic merge
```

### Must not happen

```text
do not replace current Vault head by default
do not delete target-only chunks
do not treat same visible Vault ID as proof of same Vault
do not import unverified bundle
```

### Test requirement

Test:

```text
old export into newer vault
newer export into older vault
same Vault ID but different identity
conflicting same-path files
tampered export
missing chunks in export
```

---

## 3.10 Export protection

### Why this is crucial

The export may be stored on USB drives, copied to cloud storage, emailed, or archived for disaster recovery.

A stolen export must not reveal Vault contents.

### Required behavior

Even though Vault data is internally encrypted, the export file should have an outer protection layer.

Recommended:

```text
protected .dc-vault-export
export passphrase or export key
authenticated encryption
tamper detection
strict parser
```

### Must not happen

```text
do not export plaintext filenames
do not export plaintext manifest
do not use weak ZIP password encryption
do not allow unauthenticated export metadata if it leaks sensitive structure
```

### Test requirement

Test:

```text
wrong export passphrase fails
tampered export fails
truncated export fails
export verifies after creation
import from export restores exact files
```

---

## 3.11 Delete, clear, and purge

### Why this is crucial

Destructive actions are the easiest way to turn a secure Vault into a data-loss tool.

### Required distinction

```text
Delete:
  hides selected file/folder from current view
  creates tombstone
  previous versions retained

Clear folder:
  tombstones all current entries in one remote folder
  recoverable during retention

Clear whole Vault:
  tombstones all current entries in all folders
  recoverable during retention

Hard purge:
  physically removes retained encrypted data
  not recoverable from that relay
```

### Required UX

```text
delete file: normal confirmation
clear folder: type folder name
clear whole Vault: type full Vault ID
hard purge: fresh unlock + admin role + delay + typed Vault ID
```

### Must not happen

```text
do not physically purge on normal delete
do not make clear and disconnect look similar
do not let sync-only devices hard purge
do not garbage collect chunks still referenced by any retained version
```

---

## 3.12 Local binding after restore

### Why this is crucial

This is one of the strongest parts of the design, but only if implemented strictly.

After import/restore, the device has no local filesystem targets. That is correct and safe.

### Required behavior

After restore:

```text
remote folders visible
browser mode enabled
download/upload allowed if role permits
no local path selected
no sync starts
no remote tombstones applied to local files
```

When connecting a local folder:

```text
scan local folder
compare with remote manifest
show preflight
create baseline only after confirmation
```

### Must not happen

```text
do not guess local paths
do not auto-bind by folder name
do not start sync immediately after restore
do not apply old remote deletes before initial baseline
```

---

## 3.13 Sync defaults

### Why this is crucial

Two-way sync is dangerous for critical files if implemented too early or too aggressively.

### Recommended default

For first practical release, prefer:

```text
Browse only
Backup only
Manual restore
Manual upload/download
Version history
```

Only later add:

```text
Two-way sync
```

### Recommended connected-folder default

```text
Backup only
```

not:

```text
Two-way sync
```

### Must not happen

```text
do not make bidirectional mirror the default
do not delete local files during first binding
do not overwrite local files without conflict copy
```

---

## 3.14 File stability and critical single-file databases

### Why this is crucial

The target use case includes files like:

```text
KeePassXC databases
password vault files
SQLite files
certificate stores
small encrypted containers
```

These files may be rewritten atomically or temporarily locked.

### Required behavior

```text
wait for file stability before upload
detect file changed during read/upload
retry after stable
keep previous valid version
never replace last good remote version with partial read
```

### Special care for KeePassXC-like files

KeePassXC database files are usually single critical files.

For these files, the Vault should be very conservative:

```text
every successful upload creates a new version
failed upload does not affect previous version
remote restore can download previous versions easily
```

### Must not happen

```text
do not upload half-written database file
do not remove previous version immediately
do not collapse versions too aggressively for critical files
```

---

## 3.15 Ransomware and mass-change protection

### Why this is crucial

Even for small datasets, mass modification can destroy critical data quickly.

### Required detection

Pause sync if suspicious:

```text
large percentage of tracked files deleted
large percentage of tracked files modified
many files renamed quickly
many extensions changed
many files become unreadable or high-entropy unexpectedly
```

### Suggested behavior

```text
Suspicious mass change detected.
Vault sync has been paused for this folder.
Review changes before uploading.
```

### Must not happen

```text
do not blindly upload mass damage
do not turn ransomware output into the newest trusted version without warning
```

---

## 3.16 Ignore rules and special files

### Why this is crucial

The selected folder may contain temporary files, lock files, symlinks, sockets, caches, or build outputs.

For critical data, accidental inclusion can leak irrelevant data or break restore behavior.

### Required default exclusions

```text
*.tmp
*.part
*.crdownload
*.swp
*.lock
.DS_Store
Thumbs.db
desktop.ini
.git/
node_modules/
.cache/
```

### Required special-file policy

Recommended v1:

```text
regular files: supported
directories: supported
symlinks: skip by default
hardlinks: treat as separate files
sockets/FIFOs/device files: skip
permission denied: show sync problem
```

### Must not happen

```text
do not follow symlinks outside selected folder by default
do not silently ignore permission errors without showing user
```

---

## 3.17 Case sensitivity and path normalization

### Why this is crucial

Path handling bugs can overwrite files during restore.

### Required behavior

```text
manifest stores normalized relative paths
no absolute paths
no ../ path traversal
preserve case
detect case conflicts on incompatible targets
```

### Must not happen

```text
do not allow export/import path traversal
do not overwrite Report.pdf with report.pdf on case-insensitive target
do not allow reserved names to break restore on future platforms
```

---

## 3.18 Local disk-space preflight

### Why this is crucial

Disaster recovery often happens under pressure. A restore that fails halfway due to insufficient space creates confusion and risk.

### Required checks

Before download/restore/export/import:

```text
available local space
temporary space needed
final size estimate
remote quota if uploading/importing
```

### Must not happen

```text
do not start large restore if clearly insufficient space
do not leave temp files without clear cleanup
do not overwrite existing files before full download succeeds
```

---

## 3.19 Integrity check

### Why this is crucial

Users need to know whether their remote Vault is actually restorable.

### Required feature

```text
Vault settings
→ Check Vault integrity
```

### Checks

```text
manifest decrypts
manifest revision chain valid
chunk references exist
sample or full chunk decrypt works
usage counters match
versions reachable
tombstones valid
```

### Recommended modes

```text
Quick check
Full verification
```

### Must not happen

```text
do not show "Vault OK" based only on server responding
do not ignore missing chunks
```

---

## 3.20 Activity timeline and diagnostics

### Why this is crucial

For critical data, users need to know what happened.

### Required UI

```text
Vault activity
```

Examples:

```text
uploaded KeePass.kdbx
created new version of contracts.pdf
downloaded previous version
skipped file due to permission error
sync paused because of suspicious mass change
export completed and verified
import merged 12 files
```

### Privacy rule

```text
client UI may show decrypted names
relay logs must not contain plaintext filenames
debug bundle must not include secrets
```

### Must not happen

```text
do not log Vault Master Key
do not log recovery passphrase
do not log Vault Access Secret
do not log plaintext filenames on relay
```

---

# 4. Biggest weaknesses

## 4.1 New sync engine risk

The largest weakness is implementing a new sync engine.

Mature tools have years of edge cases behind them. Vault will need to handle:

```text
conflicts
file locks
partial writes
atomic save patterns
case conflicts
renames
local deletes
remote deletes
permission errors
Android storage limitations
```

Mitigation:

```text
do not start with two-way sync
release browse/download/upload/versioned backup first
make Backup only the default connected-folder mode
```

---

## 4.2 Less efficient than dedicated backup tools

Vault is not meant for huge datasets, but even small critical datasets can have many versions.

Dedicated backup tools are better at:

```text
deduplication
compression
snapshot pruning
repository compaction
large-scale verification
```

Mitigation:

```text
be honest about target use case
keep retention understandable
optimize for correctness
add compaction later
```

---

## 4.3 Recovery burden is on the user

Account-less recovery means there is no server-side reset.

Weakness:

```text
if user loses all devices and recovery material, data is gone
```

Mitigation:

```text
clear setup flow
recovery test
recovery status
export reminders
plain warnings
```

---

## 4.4 Relay protects confidentiality, not availability

The relay cannot read Vault data, but it can lose, corrupt, or delete encrypted blobs.

Weakness:

```text
self-hosted relay failure can still cause data loss
```

Mitigation:

```text
protected exports
relay migration
server backups
integrity check
missing chunk detection
```

---

## 4.5 Import/export complexity

Import/export is valuable but dangerous.

Weakness:

```text
old exports can conflict with newer Vault state
different Vaults can share visible ID by accident or attack
tampered exports can crash bad parsers
```

Mitigation:

```text
protected export envelope
genesis Vault identity
merge-only default
strict import parser
no rollback by default
```

---

## 4.6 New cryptographic format

Even with good primitives, a custom persistent encrypted format has risk.

Weakness areas:

```text
nonce handling
AAD mistakes
recovery envelope design
manifest rollback
key storage
test vector mismatch between Python and Android
```

Mitigation:

```text
small format surface
documented test vectors
cross-platform tests
avoid clever crypto
prefer established libraries
external review if possible
```

---

## 4.7 Mobile limitations

Android is not a reliable always-on filesystem sync environment.

Weakness:

```text
background sync may be delayed
folder access can be revoked
battery/network restrictions can interrupt work
```

Mitigation:

```text
Android browse/download/upload first
manual sync first
scheduled sync later
clear status messages
do not promise desktop-grade watching on Android
```

---

## 4.8 No broad cloud ecosystem

Commercial encrypted clouds already offer polished sharing, accounts, and storage.

Vault will be weaker at:

```text
public links
team sharing
web access
account management
business admin features
large hosted storage
```

Mitigation:

```text
do not position Vault as another cloud drive
position it as account-less encrypted disaster-recovery storage for Desktop Connector users
```

---

# 5. Strengthened product positioning

The intended positioning should be:

```text
Desktop Connector Vault is an account-less encrypted backup and recovery feature for small critical datasets.

It stores encrypted, versioned copies of selected files/folders on your Desktop Connector relay, lets you browse and restore them from another device, and supports protected export/import for disaster recovery and relay migration.
```

Avoid positioning it as:

```text
Dropbox replacement
Syncthing replacement
Borg/Kopia/restic replacement
large media backup system
enterprise cloud storage
```

Better wording:

```text
Secure remote recovery for critical files.
```

or:

```text
Encrypted disaster-recovery Vault for your most important files.
```

---

# 6. Recommended implementation priority for this target

Because the target is small critical data, the best order is:

```text
1. Vault create/open/recovery
2. Recovery test
3. Remote browser
4. Manual upload/download
5. Version history
6. Protected export/import
7. Integrity check
8. Backup-only folder binding
9. Suspicious change detection
10. Restore to local folder
11. Two-way sync only after the above is stable
```

This order matches the actual value proposition:

```text
critical data survives disaster
user can recover it safely
sync convenience comes later
```

---

# 7. Non-negotiable release gates

Before any release that users might trust with critical data:

```text
wrong passphrase cannot open Vault
recovery test works
export/import roundtrip works
previous versions restore correctly
missing chunks are detected
tampered manifest fails
tampered export fails
new device opens browse-only
local folder binding requires preflight
delete does not purge immediately
import does not rollback by default
sync does not overwrite conflicts silently
integrity check exists
```

If these are not done, Vault should be marked experimental.

---

# 8. Final risk summary

The feature is most valuable if it is conservative.

For small critical datasets, users need:

```text
I can recover my KeePass database and documents after disaster.
I can verify my recovery setup.
I can browse the remote Vault from a new device.
I can download previous versions.
I can export the Vault securely.
I will not lose files because sync guessed wrong.
```

That should drive every implementation decision.
