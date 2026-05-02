# Desktop Connector Tresor — 08 Sync Engine and Folder Binding

## Goal

Define how automatic sync works once a remote folder is explicitly connected to a local filesystem folder.

## Important principle

Sync is opt-in per remote folder per device.

A restored/imported vault is browse-only until the user connects folders.

## Local binding flow

```text
Remote folder selected
→ user clicks Connect local folder
→ choose local path
→ scan local path
→ compare with remote manifest
→ show sync plan
→ user confirms
→ create local binding checkpoint
→ start sync
```

## Local binding checkpoint

Store locally:

```json
{
  "vault_id": "H9K7-M4Q2-Z8TD",
  "remote_folder_id": "rf_...",
  "local_path": "/home/michal/Documents",
  "sync_direction": "two_way",
  "binding_created_at": 1777650000,
  "initial_baseline_revision": 42,
  "last_applied_remote_revision": 42,
  "last_uploaded_local_scan_id": "scan_...",
  "initial_sync_completed": true
}
```

## Sync directions

Supported modes:

```text
two_way
upload_only
download_only
paused
```

### two_way

Local changes upload.

Remote changes download.

Deletes propagate as tombstones after baseline.

### upload_only

Local changes upload to remote.

Remote changes do not modify local files automatically.

Useful for backup mode.

### download_only

Remote changes download.

Local changes are ignored or reported.

Useful for restore mirror mode.

### paused

No automatic sync.

Browser operations still available.

## First implementation recommendation

Implement in this order:

```text
1. browse-only
2. manual upload/download
3. upload-only bound folder backup
4. restore remote into local folder
5. two-way sync
```

Do not start with two-way sync.

## Desktop folder watcher

Desktop should use filesystem watcher where possible.

Behavior:

```text
watch bound local paths
debounce bursts
wait for file stability before reading
ignore temporary files
queue changes
survive app restart
fall back to periodic scan
```

File stability check:

```text
same size + same mtime for N seconds
```

or:

```text
open/read succeeds without sharing/permission error
```

## Android folder sync

Android should be more conservative.

Recommended v1 Android:

```text
restore/import vault
browse remote files
download files
manual upload
receive vault grant
```

Recommended later Android:

```text
SAF tree binding
manual sync
WorkManager scheduled sync
network/battery constraints
```

Do not promise reliable real-time folder watching on Android.

## Local index

Use a local SQLite database.

Tables:

```text
tresor_vaults
tresor_remote_folders_cache
tresor_bindings
tresor_local_entries
tresor_pending_operations
tresor_download_cache
tresor_conflicts
```

## Upload sync flow

```text
watcher detects change
→ enqueue path
→ wait for stability
→ scan file
→ chunk/encrypt/upload missing chunks
→ create manifest update
→ CAS update manifest
→ update local index
```

If CAS conflict:

```text
download latest manifest
merge
retry
```

## Download sync flow

```text
poll manifest revision or receive notification
→ download encrypted manifest
→ decrypt
→ compare with local binding checkpoint
→ plan downloads/deletes/conflicts
→ apply safe operations
→ update local index
```

## Applying remote downloads

Safe write process:

```text
download encrypted chunks
→ decrypt into temp file inside target filesystem
→ fsync temp file
→ atomic rename into final path
→ update local index
```

If final path changed locally during download:

```text
do not overwrite
create conflict
```

## Applying remote deletes

Only after initial baseline is complete.

Process:

```text
remote tombstone found
→ check local entry was previously synced
→ move local file to local trash or app safety folder
→ update local index
```

Recommended:

```text
local deletes caused by sync should go to OS trash if possible
```

Do not permanently delete immediately.

## Local delete propagation

If user deletes local synced file:

```text
watcher detects missing file
→ verify it was previously synced
→ create remote tombstone
→ retain remote versions
```

If the file was never synced:

```text
do nothing remote
```

## Conflict cases

### Local and remote changed same file

Default:

```text
keep both
```

Local conflict filename:

```text
example (conflict from This Device 2026-05-02 17-30).pdf
```

### Remote delete vs local modify

Default:

```text
keep local modified copy
keep remote tombstone
create conflict notice
```

Do not delete locally modified data.

### Local delete vs remote modify

Default:

```text
keep remote modified version
record local delete as conflict
```

User can later delete intentionally.

## Rename detection

v1 behavior:

```text
treat rename as delete old path + upload new path
```

Later:

```text
detect move by stable file fingerprint
```

## Browser operations and bound folders

If user uploads/deletes through browser into a folder that is bound on the same device:

```text
manifest revision changes
sync engine treats it as remote change
local folder will receive upload/delete according to normal sync rules
```

UI should warn before browser delete in a bound folder:

```text
This remote folder is synced on this device. Deleting here may remove the local file during sync.
```

## Pause and disconnect

### Pause

```text
stop automatic sync
keep local binding
keep local index
browser still works
```

### Disconnect

```text
stop automatic sync
remove local binding
keep local files
remote folder remains
```

### Clear remote folder

```text
danger action
remote tombstones
affects other devices
```

Keep these actions visually separate.
