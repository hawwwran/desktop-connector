# Desktop Connector Vault — 02 Product Model: Vault, Remote Folders, Local Bindings

## Goal

Vault should support multiple synced folders inside one vault.

A remote folder and a local folder are not the same thing.

This distinction is critical for recovery and safety.

## User-facing model

The app has one or more vaults.

First implementation can support one vault per relay/device, but the data model should not prevent multiple vaults later.

A vault contains top-level remote folders:

```text
Vault Vault: H9K7-M4Q2-Z8TD

Remote folders:
  Documents
  Photos
  Work Backup
  Desktop Connector Project
```

Each remote folder may have a local binding on the current device:

```text
Documents
  Remote folder ID: rf_...
  Local binding: /home/michal/Documents
  Status: Syncing

Photos
  Remote folder ID: rf_...
  Local binding: none
  Status: Browse-only

Work Backup
  Remote folder ID: rf_...
  Local binding: /home/michal/Work
  Status: Paused
```

## Visible Vault ID

The Vault ID must be visible in the app.

Recommended display:

```text
Vault ID: H9K7-M4Q2-Z8TD
```

With actions:

```text
Copy full Vault ID
Show vault QR
Show recovery status
Export vault
Migrate vault
```

## Vault ID is not a secret

The Vault ID should be a random public identifier.

It can be shown in the UI and copied safely.

It must not grant access by itself.

Correct security model:

```text
Vault ID = lookup identifier
Vault access secret = authorization capability
Vault master key = decryption capability
Recovery secret/passphrase = recovery capability
```

An attacker with only the Vault ID must not be able to:

```text
list vault contents
download chunks
upload chunks
delete chunks
modify manifests
learn folder names
learn filenames
```

## Remote folder identity

Each remote folder has:

```json
{
  "remote_folder_id": "rf_8LhJ...",
  "display_name_enc": "...",
  "created_at": 1777650000,
  "created_by_device_id": "desktop_...",
  "folder_key_id": "fk_...",
  "retention_policy": {
    "keep_deleted_days": 30,
    "keep_versions": 10
  },
  "state": "active"
}
```

The plaintext display name exists only inside encrypted manifest data.

The server may store the remote folder ID for indexing if necessary, but should not store plaintext folder names.

## Local binding identity

Local binding is device-local state.

It should not be treated as vault-global truth.

Example local binding record:

```json
{
  "vault_id": "H9K7-M4Q2-Z8TD",
  "remote_folder_id": "rf_8LhJ...",
  "local_path": "/home/michal/Documents",
  "binding_state": "bound",
  "sync_direction": "two_way",
  "initial_baseline_revision": 42,
  "last_applied_remote_revision": 88,
  "last_local_scan_id": "scan_...",
  "created_at": 1777650000
}
```

This should be stored locally, not on the relay.

## Binding states

Recommended states:

```text
unbound
bound
paused
browse_only
error
needs_preflight
```

### unbound

Remote folder exists, but this device has no local folder connected.

Allowed:

```text
browse
download
upload through remote browser
delete through remote browser if role allows
download previous versions
connect local folder
```

Not allowed:

```text
automatic filesystem sync
automatic local deletion
automatic local writes
```

### bound

Remote folder is connected to a local filesystem path.

Allowed:

```text
automatic sync
manual sync now
browse remote
manual upload/download
disconnect binding
pause sync
```

### paused

Binding exists, but no automatic sync is running.

Allowed:

```text
browse
download
manual upload if allowed
resume sync
disconnect binding
```

### needs_preflight

The user selected a local path, but the app has not yet accepted an initial sync plan.

Allowed:

```text
scan local folder
compare remote/local state
show sync plan
cancel
confirm binding
```

Not allowed:

```text
apply remote tombstones
delete local files
start automatic sync
```

## New-device import behavior

After importing/restoring a vault on a new device:

```text
All remote folders appear as unbound.
The app shows their names and usage after decrypting the manifest.
No local path is assigned.
No sync starts.
```

The remote browser is fully available.

The user may choose:

```text
Download this file
Download this folder
Upload files here
Delete remote file
Download older version
Connect local folder
```

## Connecting a local folder

When the user connects a local folder to a remote folder:

```text
1. User selects remote folder.
2. User selects local filesystem folder.
3. App scans local folder.
4. App compares local snapshot to remote manifest.
5. App shows preflight plan.
6. User confirms.
7. App creates local binding checkpoint.
8. Sync starts only after checkpoint.
```

## Safe initial preflight cases

### Remote has files, local folder is empty

Offer:

```text
Restore remote contents into this folder
Start sync after restore
```

### Remote has files, local folder has unrelated files

Offer:

```text
Merge safely
Keep both on conflict
Do not delete anything
```

### Remote has tombstones/deletions

On first binding, remote tombstones must not be applied to local files automatically.

Treat tombstones as history, not as commands, until baseline is created.

### Local has files with same paths but different content

Default behavior:

```text
keep both
create conflict copies
```

Do not overwrite by default.

## Multiple remote folders

The vault should allow adding multiple main folders.

UI example:

```text
Vault
  Vault ID: H9K7-M4Q2-Z8TD
  Used: 18.4 GB encrypted / 17.9 GB logical

  Folders:
    Documents       4.2 GB logical    Bound to /home/michal/Documents
    Photos         13.1 GB logical    Browse-only
    Projects        0.6 GB logical    Paused
```

## Per-folder operations

For each remote folder:

```text
Browse
Connect local folder
Disconnect local folder
Pause sync
Sync now
Download folder
Export folder later
Clear folder
Rename folder
Show usage
Show versions/retention
```

## Folder rename

Remote folder rename should be a metadata operation.

It must not move local folders automatically.

If a bound remote folder is renamed:

```text
local path remains unchanged
remote display name changes
binding remains valid via remote_folder_id
```

## Removing a local binding

Disconnecting local binding should not delete remote data.

Flow:

```text
User chooses "Disconnect local folder"
→ app stops sync
→ local files stay untouched
→ remote folder remains in vault
→ folder becomes browse-only on this device
```

This is different from clearing or deleting remote data.

## Clear folder vs disconnect folder

The UI must distinguish these clearly:

```text
Disconnect from this device
  - stops sync only
  - keeps remote data
  - keeps local files

Clear remote folder
  - deletes/hides remote files using tombstones
  - can affect other synced devices after sync
  - requires danger confirmation

Hard purge remote folder
  - physically removes retained chunks after policy
  - requires stronger authorization
```
