# Desktop Connector Tresor — 09 Destructive Actions and Threat Model

## Goal

Make clear/delete/purge operations safe enough that they do not become the easiest attack vector.

The dangerous actions are:

```text
delete file
delete folder
clear remote folder
clear whole vault
hard purge retained versions
garbage collect chunks
remove remote folder
remove vault from relay
```

## Main design principle

Destructive actions must be:

```text
explicit
auditable
recoverable by default
delayed before physical purge
permission-gated
protected against accidental sync damage
```

## Attackers to consider

### Malicious relay

Can:

```text
delete blobs
hide latest manifest
return old manifest
refuse service
corrupt ciphertext
```

Cannot:

```text
decrypt data
forge valid encrypted manifest update
create valid chunk plaintext
know filenames
```

Mitigations:

```text
manifest hash chain
client-side AEAD verification
missing-chunk detection
export backups
migration ability
local cached latest revision
```

The app cannot prevent a relay operator from deleting stored ciphertext. It can only detect and recover if user has export/backup elsewhere.

### Attacker with Vault ID only

Must not be able to do anything useful.

Mitigations:

```text
vault access bearer secret
rate limiting
no unauthenticated list/download/delete
Vault ID treated as public
```

### Attacker with device auth only

A registered Desktop Connector device without vault access must not control a vault.

Mitigations:

```text
separate vault auth
separate vault grants
```

### Attacker with unlocked vault device

This is serious.

If a device has write/destructive permissions and is compromised, it can create valid delete operations.

Mitigations:

```text
roles/permissions
fresh local unlock for destructive operations
soft delete by default
retention
delayed purge
operation log
device revocation
exports/backups
```

You cannot fully prevent damage from a fully compromised authorized device. You can limit and make it recoverable.

## Delete vs clear vs purge

Use precise terminology.

### Delete file/folder

```text
creates tombstone
hides from current view
keeps versions during retention
recoverable
```

### Clear remote folder

```text
creates tombstones for all entries in one remote folder
folder remains
recoverable during retention
danger flow required
```

### Clear whole vault

```text
creates tombstones for all remote folders
vault remains
recoverable during retention
stronger danger flow required
```

### Hard purge

```text
physically removes retained chunks/manifests no longer needed
not recoverable from that relay
strongest authorization required
delayed execution recommended
```

## Soft delete as default

All normal browser delete actions should be soft delete.

```text
file deleted
→ manifest tombstone
→ old chunks retained
→ previous versions available
→ restore possible
```

## Clear folder flow

User flow:

```text
Remote folder menu
→ Clear remote folder contents
→ app shows:
   folder name
   file count
   current logical size
   retained history size
   synced devices warning
→ user must type folder name or Vault ID suffix
→ app requires fresh vault unlock
→ app creates clear operation in manifest
→ chunks retained until retention expires
```

Suggested text:

```text
This will remove all current files from the remote folder view.
Synced devices may remove their local copies during sync.
Previous versions remain recoverable until the retention period expires.
```

## Clear whole vault flow

Stronger flow:

```text
Vault settings
→ Clear whole vault
→ show Vault ID
→ show all folders and usage
→ require typing full visible Vault ID
→ require fresh vault unlock
→ require admin role
→ create vault-wide clear operation
→ retain versions until retention expires
```

Do not physically purge immediately.

## Hard purge flow

Hard purge should not be casual.

Requirements:

```text
admin role
fresh vault unlock
type full Vault ID
show exact effect
delay before physical purge
option to export first
```

Recommended:

```text
Purge is scheduled, not instant.
Default delay: 24 hours.
User can cancel during delay from any admin device.
```

## Purge capability

To reduce attack surface, use a separate purge capability.

At vault creation:

```text
purge_secret = random high-entropy value
```

Store it:

```text
inside recovery kit
inside admin device secure storage
not inside read-only/sync-only grants
```

Server accepts hard purge only with:

```text
device auth
vault auth
valid purge authorization token
pending-purge manifest marker
grace period passed
```

This is extra protection against a compromised sync-only device.

## Import as attack vector

Danger:

```text
old export imported into current vault
→ current files disappear or rollback
```

Mitigation:

```text
import never replaces current head by default
import merges
older imported revisions become history
conflicts keep both
user must explicitly choose rollback
```

Rollback should be a separate destructive operation.

## Migration as attack vector

Danger:

```text
target relay has same Vault ID but different vault
```

Mitigation:

```text
compare decrypted genesis identity
refuse if different
```

Danger:

```text
migration partially completed, app switches to incomplete relay
```

Mitigation:

```text
do not switch active relay until verification succeeds
```

## Browser upload as attack vector

Danger:

```text
attacker uploads many files and fills quota
```

Mitigation:

```text
roles
quotas
upload preflight
rate limits
storage usage warning
```

Danger:

```text
upload path traversal
```

Mitigation:

```text
client normalizes paths
manifest parser rejects absolute paths and ../
server treats chunks as blobs only
```

## Browser delete as attack vector

Danger:

```text
malicious/compromised browse device deletes remote files
```

Mitigation:

```text
browse-only role cannot delete by default
delete permission separate from download
fresh unlock for folder/whole-vault clear
soft delete + retention
```

## Sync binding as attack vector

Danger:

```text
user binds remote folder to wrong local folder
sync deletes or overwrites data
```

Mitigation:

```text
preflight plan
no tombstone application before baseline
never delete unknown local files
conflicts keep both
local deletes go to trash/safety folder
```

## Rollback attack

Malicious relay may serve old manifest.

Mitigations:

```text
local device stores latest known revision/hash
client warns if server returns older revision
manifest hash chain
optional external/latest revision witness later
```

Limit:

```text
a brand-new restored device cannot always know the latest revision without another trusted source
```

## Permission recommendations

Default owner/admin device:

```text
all permissions
```

New QR-joined device:

```text
ask role during grant
```

Suggested roles:

```text
Read-only
Browse + Upload
Sync
Admin
```

Do not grant hard purge to non-admin devices.

## Red line

Do not add a server endpoint that can clear a folder by plaintext path.

All semantic deletes must be encrypted manifest operations from an authorized client.
