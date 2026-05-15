# Desktop Connector Vault — 11 Implementation Roadmap and Tests

## Goal

Break the work into manageable development chunks.

Each phase should produce a useful, testable result.

## Phase T0 — Documentation and protocol skeleton

Deliverables:

```text
docs/plans/vault-00-index.md
docs/protocol/vault-v1.md
docs/security/vault-threat-model.md
docs/ui/vault-wireflow.md
```

Decisions to lock:

```text
API namespace
Vault ID format
export file format
crypto primitives
manifest CAS rules
delete/clear/purge terminology
```

## Phase T1 — Relay persistent vault storage

Implement server-only support:

```text
vault create
vault header get/put
manifest get/put with CAS
chunk put/get/head
batch chunk check
quota counters
capability discovery
```

Do not implement folder semantics on server.

Tests:

```text
create vault succeeds
duplicate Vault ID rejected or handled
wrong vault auth rejected
chunk upload requires vault auth
manifest CAS conflict returns 409
quota enforcement works
old transfer endpoints still pass tests
```

## Phase T2 — Shared crypto and format test vectors

Implement cross-platform test vectors for Python and Kotlin:

```text
vault master key derivation
header encryption/decryption
manifest encryption/decryption
chunk encryption/decryption
recovery envelope open/fail
export envelope open/fail
AAD mismatch fail
wrong passphrase fail
tampered ciphertext fail
```

Output test vector files:

```text
tests/protocol/vault-v1/*.json
```

## Phase T3 — Desktop vault create/open

Implement desktop local vault module:

```text
create vault
generate Vault ID
generate Vault Master Key
create recovery envelope
store device grant securely
upload empty encrypted manifest
show Vault ID in UI
show whole-vault usage from server
```

No folder sync yet.

## Phase T4 — Remote folders and usage

Implement:

```text
add remote folder
rename remote folder
list remote folders
compute per-folder logical usage
compute per-folder stored usage
show whole-vault usage
```

Still no local binding.

## Phase T5 — Remote browser read/download

Implement:

```text
decrypt manifest
render file tree
download latest file
download previous version
show versions panel
download folder to selected destination
```

Tests:

```text
previous version downloads correct bytes
deleted file versions can be downloaded if retained
bad chunk fails cleanly
missing chunk reports recoverable error
```

## Phase T6 — Browser upload

Implement:

```text
upload file into remote folder
upload folder into remote folder
add as new version
keep both conflict behavior
upload progress
chunk retry
CAS merge/retry
```

Tests:

```text
upload file then download same bytes
same path can create new version
CAS conflict during upload handled
upload resume works
quota exceeded handled
```

## Phase T7 — Browser soft delete and restore

Implement:

```text
delete file
delete folder
tombstones
show deleted/recoverable items
restore previous version as current
restore deleted file
```

Tests:

```text
delete hides current file
versions remain recoverable
restore creates new version
delete does not physically remove chunks
bound-folder delete warning appears when relevant
```

## Phase T8 — Protected export/import

Implement:

```text
full protected .dc-vault-export
export passphrase
export verification
import into empty relay
import into existing same vault with merge
refuse different-vault collision
browse-only after import
```

Tests:

```text
stolen export without passphrase reveals no vault content
wrong export passphrase fails
tampered export fails
import preserves existing target data
import of older export does not rollback current vault
same Vault ID different identity refused
```

## Phase T9 — Relay migration

Implement:

```text
direct migration old relay → new relay through client
target capability check
copy encrypted header/manifests/chunks
verify
switch active relay only after success
```

Tests:

```text
interrupted migration does not switch active relay
migration resumes
target missing chunks detected
same vault identity confirmed
wrong target collision blocked
```

## Phase T10 — Local binding and upload-only backup

Implement:

```text
connect local folder
preflight scan
local index
initial baseline
upload-only mode
manual sync now
```

Tests:

```text
new device starts unbound
connecting non-empty folder shows preflight
remote tombstones not applied before baseline
upload-only does not modify local files
```

## Phase T11 — Restore remote into local folder

Implement:

```text
download remote folder into empty local folder
safe merge into non-empty local folder
conflict copies
atomic writes
local trash/safety folder
```

Tests:

```text
restore preserves bytes
merge keeps both on conflict
remote delete does not delete unknown local file during initial binding
partial restore resumes safely
```

## Phase T12 — Two-way sync

Implement:

```text
filesystem watcher
periodic scan fallback
local-to-remote changes
remote-to-local changes
tombstone propagation after baseline
conflicts
CAS merge
pause/resume
disconnect
```

Tests:

```text
local edit uploads new version
remote edit downloads
simultaneous edit creates conflict
local delete creates tombstone
remote delete moves local file to trash/safety folder
disconnect does not delete local or remote data
```

## Phase T13 — QR-assisted vault grants

Implement:

```text
grant vault to paired device
vault join QR
ephemeral key exchange
verification code
role selection
grant expiry
grant revocation
```

Tests:

```text
join QR cannot be reused
expired join rejected
new device receives correct role
read-only device cannot upload/delete
sync device cannot hard purge
```

## Phase T14 — Dangerous clear/purge flows

Implement:

```text
clear remote folder
clear whole vault
fresh unlock
typed confirmation
soft clear
pending hard purge
purge cancellation
GC after retention
```

Tests:

```text
clear creates tombstones only
hard purge requires admin/purge capability
purge cannot run before grace period
purge does not delete referenced chunks
clear can be restored before retention expires
```

## Phase T15 — Android browse/import/upload

Implement Android:

```text
restore/import vault
show Vault ID
show usage
browse remote folders
download latest/previous version
upload file
soft delete if role allows
QR grant receive
```

Tests:

```text
Android decrypts desktop-created vault
desktop decrypts Android-uploaded files
version download works
read-only role enforced
```

## Phase T16 — Android folder sync

Implement later:

```text
SAF folder binding
manual sync
WorkManager scheduled sync
upload-only first
download/merge later
```

## Phase T17 — Diagnostics and hardening

Implement:

```text
activity log
redacted logs
debug bundle without secrets
integrity checker
storage usage repair
manifest repair helper
chunk verification
```

Diagnostic events:

```text
vault.created
vault.opened
vault.restored
vault.export.started
vault.export.completed
vault.import.started
vault.import.completed
vault.migration.started
vault.migration.completed
vault.folder.created
vault.folder.bound
vault.folder.unbound
vault.folder.cleared
vault.browser.upload.started
vault.browser.upload.completed
vault.browser.delete.created
vault.version.downloaded
vault.sync.scan.started
vault.sync.scan.completed
vault.sync.conflict.created
vault.gc.started
vault.gc.completed
```

Never log:

```text
Vault Master Key
Recovery Secret
Recovery Passphrase
Vault Access Secret
Export passphrase
plaintext filenames in relay logs
plaintext paths in normal logs
```

## Recommended first pull requests

PR 1:

```text
docs only:
  protocol/vault-v1.md
  threat model
  product model
```

PR 2:

```text
server capability + empty vault create/open API
```

PR 3:

```text
desktop crypto vectors + vault create/open
```

PR 4:

```text
remote folder model + browser skeleton
```

PR 5:

```text
manual upload/download
```

Do not start with full sync.

## Definition of done for v1

A realistic v1 could be:

```text
desktop creates vault
desktop shows Vault ID and usage
desktop adds multiple remote folders
desktop browser uploads/downloads files
desktop supports previous version download
desktop soft-deletes files/folders
desktop exports protected bundle
desktop imports protected bundle
desktop imports into existing same vault by merge
new device opens browse-only
relay migration works
```

Automatic local folder sync can be v1.5 or v2 if needed.

## Non-negotiable tests

Before release:

```text
wrong passphrase cannot open vault
wrong Vault ID secret cannot access API
same Vault ID different identity cannot merge
import cannot rollback existing vault by default
clear folder does not immediately purge chunks
new device import does not start sync
new local binding does not apply old remote tombstones
browser upload/download preserves bytes
previous version download returns exact old bytes
CAS conflict cannot lose either side's changes
```
