# Desktop Connector Vault — Open UX and Implementation Gaps

Status: discussion notes  
Purpose: collect additional items that should be reviewed and resolved before or during Vault implementation.

This document supplements the main Desktop Connector Vault implementation plans. It focuses on missing UX details, safety gaps, failure states, and edge cases that are easy to forget when designing encrypted backup/sync software.

---

## 1. Recovery test / recovery health check

### Problem

Users may create recovery material once and never verify it. In an account-less encrypted Vault, broken or untested recovery material can mean permanent data loss.

### Proposed feature

Add:

```text
Vault settings
→ Recovery
→ Test recovery
```

### Flow

```text
User enters recovery phrase/passphrase or imports recovery file
→ app attempts to decrypt the Vault header
→ app verifies that the Vault Master Key can be recovered
→ app confirms success
→ no data is modified
```

### Suggested UI state

```text
Recovery status:
  OK
  Not tested
  Missing
  Weak passphrase
  Needs update
```

### Suggested warning

```text
Recovery has not been tested yet.
If all devices are lost, untested recovery material may not restore this Vault.
```

### Decision needed

Should recovery testing be mandatory during Vault creation, or only strongly recommended?

---

## 2. Emergency access status

### Problem

The user needs to know whether the Vault can realistically be recovered if all devices are lost.

### Proposed feature

Show a clear emergency access state in Vault settings.

Example:

```text
Emergency recovery: Ready
Recovery method: Recovery file + passphrase
Last tested: 2026-05-02
```

Possible states:

```text
Ready
Not tested
No recovery method configured
Weak passphrase
Recovery material outdated
```

### Decision needed

Should the app show a persistent warning badge if recovery is not tested?

---

## 3. Machine-readable Vault error codes

### Problem

Sync, import, export, recovery, and migration need stable error handling. Human-readable errors are not enough.

### Proposed error codes

```text
vault_auth_failed
vault_not_found
vault_already_exists
vault_access_denied
vault_manifest_conflict
vault_manifest_tampered
vault_header_tampered
vault_quota_exceeded
vault_chunk_missing
vault_chunk_tampered
vault_export_tampered
vault_import_failed
vault_identity_mismatch
vault_import_requires_merge
vault_purge_not_allowed
vault_recovery_failed
vault_recovery_not_configured
vault_protocol_unsupported
vault_client_too_old
vault_server_too_old
vault_storage_unavailable
vault_local_disk_full
vault_sync_paused_suspicious_change
```

### API shape

Prefer:

```json
{
  "ok": false,
  "error": {
    "code": "vault_manifest_conflict",
    "message": "The Vault manifest changed on the server.",
    "details": {
      "current_revision": 43
    }
  }
}
```

### Decision needed

Should all Desktop Connector endpoints migrate to machine-readable errors, or only new Vault endpoints?

---

## 4. Protocol and capability versioning

### Problem

Vault data may live for years. Older clients must not corrupt newer Vault formats.

### Proposed capability response

```json
{
  "capabilities": [
    "vault_v1",
    "vault_manifest_cas_v1",
    "vault_export_v1",
    "vault_folder_usage_v1",
    "vault_soft_delete_v1"
  ]
}
```

### Vault format version

Each Vault header should contain:

```json
{
  "vault_protocol_version": 1,
  "minimum_client_version": "...",
  "created_by_client_version": "..."
}
```

### Required behavior

```text
If client is too old:
  refuse to open Vault for write
  allow read-only only if format is compatible
  clearly show "Update required"
```

### Decision needed

Should an older compatible client be allowed to browse read-only, or should it fully refuse newer Vaults?

---

## 5. Old-client protection

### Problem

Existing Desktop Connector clients know about transfers, not persistent Vault storage.

### Required rules

```text
Old clients ignore Vault endpoints.
Old clients cannot delete Vault chunks through transfer cleanup.
Vault chunks are stored separately from transfer chunks.
Vault manifests are never represented as transfers.
```

### Decision needed

Should the relay physically separate storage directories?

Recommended:

```text
storage/transfers/
storage/vaults/
```

---

## 6. Ransomware / mass-change protection

### Problem

A sync engine can faithfully upload mass damage: ransomware encryption, accidental bulk delete, broken script output, etc.

### Detection examples

```text
1,500 files changed in 20 seconds
80% of files renamed
many files replaced with high-entropy content
large number of deletes
large number of files changed to same extension
folder suddenly shrinks by 90%
```

### Proposed behavior

```text
Suspicious mass change detected.
Vault sync has been paused for this folder.
Review changes before uploading.
```

Actions:

```text
Review changes
Allow once
Resume sync
Rollback local folder from previous Vault version
Keep paused
```

### Decision needed

What thresholds should be used for the first version?

Possible simple defaults:

```text
pause if more than 500 files change within 5 minutes
pause if more than 30% of tracked files are deleted in one scan
pause if more than 30% of tracked files are modified in one scan
```

---

## 7. Ignore rules and exclusions

### Problem

Without exclusions, Vault will upload junk, temporary files, build artifacts, caches, and unstable partial files.

### Default exclusions

```text
node_modules/
.git/
.cache/
tmp/
temp/
*.tmp
*.part
*.crdownload
*.swp
*.lock
.DS_Store
Thumbs.db
desktop.ini
```

### User-defined exclusions

Per Vault folder:

```text
Vault folder settings
→ Excluded files
```

Support patterns like:

```text
*.log
dist/
build/
coverage/
```

### Decision needed

Should default exclusions be visible/editable, or hidden with an “advanced” toggle?

---

## 8. Symlink and special-file policy

### Problem

Linux folders can contain symlinks, device files, sockets, FIFOs, hardlinks, and permission-denied files.

Following symlinks blindly can upload data outside the selected folder.

### Recommended v1 policy

```text
regular files: supported
directories: supported
hidden files: supported
symlinks: skip by default, show warning
hardlinks: treat as separate files
sockets: skip
FIFOs: skip
device files: skip
broken symlinks: skip
permission-denied files: show sync problem
```

### Possible later enhancement

Store symlinks as symlink metadata, not followed target contents.

### Decision needed

Should symlinks be skipped by default, or stored as metadata?

Recommended: skip by default in v1.

---

## 9. Case-sensitivity conflicts

### Problem

Linux allows both:

```text
Report.docx
report.docx
```

Other platforms or storage abstractions may not safely support that distinction.

### Recommended behavior

```text
Vault manifest preserves exact case.
Client detects case-collision risk on case-insensitive targets.
Restore/binding creates conflict copies instead of overwriting.
```

### Example conflict

```text
Report.docx
report (case conflict).docx
```

### Decision needed

Should the Vault path model be case-sensitive always?

Recommended: yes, preserve exact case internally and handle target limitations during restore/sync.

---

## 10. File locking and open-file behavior

### Problem

The sync engine may detect a file while it is still being written.

### Recommended behavior

```text
wait until file is stable
skip if still changing
retry later
show "waiting for file to become stable" if persistent
```

Stability check:

```text
same size and mtime for N seconds
```

or:

```text
read succeeds and file does not change during hashing/upload
```

### Large file behavior

If file changes during upload:

```text
cancel current upload
discard partial version
restart after file becomes stable
```

### Decision needed

What should the default stability delay be?

Possible default:

```text
3 seconds for normal files
longer retry loop for large files
```

---

## 11. Local disk-space preflight

### Problem

Restore, download, export, and import can fail halfway if there is not enough local disk space.

### Required checks

Before restore/download:

```text
needed decrypted size
temporary space needed
available local free space
```

Before export:

```text
estimated export size
available destination space
```

Before import:

```text
temporary import cache size
target relay free quota
local temporary space
```

### Suggested UX

```text
Not enough local disk space.
Needed: 18.4 GB
Available: 9.2 GB
```

### Decision needed

Should downloads use a temp file in the target folder or app cache?

Recommended: target folder when possible, because atomic rename then stays on same filesystem.

---

## 12. Partial restore UX

### Problem

After damage or new-device recovery, users may not want to restore the whole Vault.

### Proposed restore actions

```text
Download selected file
Download selected folder
Restore this remote folder to local folder
Restore selected subfolder
Restore files from previous revision
Restore files changed before/after date
```

### Useful scenarios

```text
recover one accidentally deleted file
recover one project folder
recover a folder state from before ransomware
restore only photos, not documents
```

### Decision needed

Which partial restore actions belong in v1?

Recommended v1:

```text
download selected file
download selected folder
download previous version
restore remote folder into selected local folder
```

---

## 13. Vault lock / unlock model

### Problem

The app needs clear rules for how long the Vault stays unlocked and which actions require fresh authorization.

### Proposed unlock modes

```text
unlock until app quits
unlock for 15 minutes
unlock until system locks
always require unlock for sensitive actions
```

### Fresh unlock required for

```text
export Vault
change recovery settings
show/export recovery kit
grant device access
clear folder
clear whole Vault
hard purge
rotate Vault access secret
```

### Decision needed

What is the default unlock timeout?

Possible default:

```text
15 minutes for sensitive actions
normal browsing remains unlocked until app quits
```

---

## 14. Device revocation UX

### Problem

Revocation is easily misunderstood. It cannot erase data that a device already copied.

### Required copy

```text
Revoking this device prevents it from receiving future Vault updates.
It cannot remove data already copied to that device.
```

### Related actions

```text
Revoke device
Rotate Vault access secret
Require remaining devices to refresh access
Review active devices
```

### Decision needed

Should device revocation automatically rotate the Vault access secret?

Recommended: offer it, but explain that remaining devices must refresh access.

---

## 15. Relay backup warning

### Problem

The relay cannot read encrypted Vault data, but it can still lose or delete encrypted blobs.

### Required user education

```text
The relay cannot read your Vault, but it can lose or delete encrypted data.
Keep protected Vault exports or server backups if the data matters.
```

### UX location

```text
Vault creation
Vault settings
Export screen
Migration screen
```

### Decision needed

Should the app encourage scheduled protected exports?

Recommended: yes.

---

## 16. Export scheduling

### Problem

Manual export is useful, but users forget.

### Possible feature

```text
Vault settings
→ Export reminders
```

Options:

```text
Remind monthly
Remind every 3 months
Never remind
```

Later enhancement:

```text
Automatic protected Vault export to selected local folder
```

### Decision needed

Reminder only, or automatic export?

Recommended v1: reminder only.

---

## 17. Import preview detail

### Problem

“Will merge into existing Vault” is too vague.

### Import preview should show

```text
new files
new versions
deleted files retained
conflicts
chunks missing from target
remote storage needed
whether active head changes
whether any rollback would happen
```

### Default behavior

```text
merge without rollback
preserve both sides
do not delete target-only data
keep conflicts
```

### Decision needed

Should “replace current Vault with import” exist at all?

Recommended: not in v1. Only merge.

---

## 18. Manifest size and scaling limits

### Problem

One encrypted manifest is simple, but may not scale to large Vaults.

### Add guardrails

```text
max manifest size
max file count warning
max versions per file
max operation-log tail
manifest compaction
future folder-manifest migration path
```

### Suggested v1 warnings

```text
Vault contains more than 100,000 files.
This version may become slower. Folder-manifest storage is recommended for future versions.
```

### Decision needed

Set hard limits or soft warnings?

Recommended v1:

```text
soft warning first
hard safety limit for manifest size
```

---

## 19. Vault integrity check and repair

### Problem

Users need a way to verify whether remote encrypted data is complete and consistent.

### Proposed feature

```text
Vault settings
→ Check Vault integrity
```

### Checks

```text
manifest decrypts
manifest hash chain is valid
chunk references exist
chunk AEAD decrypts
usage counters match
version chains are valid
tombstones are valid
local binding index matches manifest
```

### Repair actions

```text
repair usage counters
mark missing chunks
remove invalid cache records
export recoverable data
generate diagnostic report without secrets
```

### Decision needed

Should full chunk decrypt verification be default or optional?

Recommended:

```text
quick check by default
full verification as explicit action
```

---

## 20. Clear wording around sync vs backup

### Problem

Users interpret “sync” as bidirectional mirroring and “backup” as safe history. Vault should be explicit.

### Proposed modes

```text
Browse only
Backup only
Two-way sync
Download only
Paused
```

### UI descriptions

```text
Browse only:
  View, upload, and download remote Vault files. No local folder is connected.

Backup only:
  Upload local changes to Vault. Remote changes do not modify local files automatically.

Two-way sync:
  Local and remote changes are synchronized. Conflicts keep both versions.

Download only:
  Remote changes are downloaded to the local folder. Local changes are not uploaded.

Paused:
  Local folder is connected, but automatic sync is stopped.
```

### Decision needed

Should “Backup only” be the default when connecting a local folder?

Recommended: yes, unless user explicitly chooses two-way sync.

---

## 21. Activity timeline

### Problem

Users need to trust what Vault did.

### Proposed feature

```text
Vault activity
```

Examples:

```text
uploaded 12 files
created 3 versions
deleted 1 file, recoverable until 2026-06-01
skipped 2 files due to permissions
sync paused due to suspicious mass change
export completed
import merged 128 files
```

### Privacy rule

Activity shown in the app can include decrypted names.

Relay logs must not contain plaintext filenames or paths.

### Decision needed

Should activity be stored encrypted in the Vault manifest, locally, or both?

Recommended:

```text
encrypted operation log in Vault
local detailed sync log per device
```

---

## 22. Clear wording for local effects

### Problem

Users can confuse disconnecting, deleting, clearing, and purging.

### Required action descriptions

#### Disconnect local folder

```text
Local files stay where they are.
Remote Vault data stays in Vault.
Only automatic sync from this device stops.
```

#### Delete file

```text
This removes the file from the current Vault view.
Previous versions remain recoverable until the retention period expires.
```

#### Clear Vault folder

```text
This affects the remote Vault folder and may later remove files from connected local folders.
Previous versions remain recoverable until the retention period expires.
```

#### Hard purge

```text
This permanently removes retained encrypted data from the relay.
It cannot be restored from this relay after purge completes.
```

### Decision needed

Should destructive dialogs require typing the folder name or Vault ID?

Recommended:

```text
file delete: normal confirmation
folder clear: type folder name
whole Vault clear/purge: type full Vault ID
```

---

## 23. Highest-priority additions

If only a few of these are implemented early, prioritize:

```text
1. Recovery test UX
2. Machine-readable Vault error codes
3. Protocol/capability versioning
4. Ransomware/mass-change protection
5. Ignore/exclusion rules
6. Symlink/special-file policy
7. Case-sensitivity conflict handling
8. Vault integrity check
9. Local disk-space preflight
10. Activity timeline
```

---

## 24. Final note

The current Vault plan covers the main architecture well.

The remaining risk is mostly not encryption. The real risk is failure UX:

```text
untested recovery
dangerous sync defaults
unclear destructive actions
bad imports
storage shortages
large manifests
old clients
confusing local-vs-remote effects
```

These items should be resolved before the Vault becomes an automatic sync feature.
