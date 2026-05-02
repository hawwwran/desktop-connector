# Desktop Connector Tresor — 07 Remote Browser, Upload, Delete, and Versions

## Goal

The vault must be usable even when no local folder is bound.

Browse mode should support controlled remote operations:

```text
browse remote folders
download files
download folders
upload files
delete files/folders
download previous versions
restore previous versions
```

All operations happen through encrypted manifest updates.

## Browser mode after import

After importing/restoring a vault on a new device:

```text
remote folders are visible
local bindings are absent
browser mode is available
sync is off
```

This is the expected default.

Browser actions should not require selecting a local sync target.

## Browser UI

Recommended layout:

```text
Tresor
  Vault ID: H9K7-M4Q2-Z8TD
  Used: 18.1 GB / 50 GB

  Remote folders:
    Documents
    Photos
    Projects

  Current path:
    Documents / Invoices / 2026

  Files:
    example.pdf
    invoice-001.pdf
    old-contract.docx

  Side panel:
    selected file
    latest version
    previous versions
    size
    modified time
    actions
```

## Browse operation

To browse:

```text
1. Download encrypted manifest.
2. Decrypt locally.
3. Render remote folder tree.
4. Compute usage locally.
```

The relay must not expose plaintext browsing endpoints.

Bad endpoint:

```http
GET /api/vaults/{vault_id}/folders/Documents/files
```

Good endpoint:

```http
GET /api/vaults/{vault_id}/manifest
```

The client does the rest.

## Download latest file

Flow:

```text
User selects file
→ client identifies latest non-deleted version
→ client downloads referenced encrypted chunks
→ decrypts chunks locally
→ writes selected destination file
```

Destination should be explicit in browse-only mode.

Do not silently write into some old local path.

## Download previous version

The versions panel should show:

```text
version timestamp
size
origin device
whether it is latest/current/deleted
```

Action:

```text
Download this version...
```

This should download the selected version as a file chosen by user.

It should not make it current unless user chooses restore.

## Restore previous version

Separate from download.

Flow:

```text
User selects previous version
→ Restore as current version
→ app creates new manifest revision
→ previous version's chunks are referenced by new current version
→ no chunk re-upload needed
```

This is safer than mutating history.

Example:

```text
version 4 becomes new version 7
```

## Upload file in browser mode

Upload should not require local binding.

Flow:

```text
User navigates remote folder
→ Upload file
→ choose local file
→ app chunks and encrypts
→ uploads missing chunks
→ creates new file version in manifest
→ CAS-updates manifest
```

If a file with same remote path exists:

Options:

```text
Add as new version
Upload as renamed copy
Cancel
```

Default:

```text
Ask.
```

If user explicitly selects "Upload new version" on one file:

```text
add as new version
```

## Upload folder in browser mode

Flow:

```text
User chooses Upload folder
→ app scans selected folder
→ shows number of files and total size
→ chunks/encrypts/uploads
→ updates manifest in one operation group
```

If conflict with existing paths:

Default:

```text
keep both or create new versions, depending on user choice
```

Never overwrite without preview.

## Delete file in browser mode

Delete means soft delete.

Flow:

```text
User selects file
→ Delete
→ app requires confirmation
→ creates tombstone in manifest
→ old versions remain retained
→ bound devices receive tombstone according to sync rules
```

UI copy:

```text
This removes the file from the current remote view. Previous versions are kept for the retention period and can be restored.
```

## Delete folder in browser mode

Deleting a folder means soft-deleting all entries under that folder path.

Flow:

```text
User selects folder
→ Delete folder
→ app shows count and size
→ creates tombstones for contained entries
→ keeps versions/chunks retained
```

Do not physically delete chunks immediately.

## Clear main remote folder

Clear folder is more dangerous than deleting a normal subfolder.

It should have a separate danger flow.

Soft clear:

```text
marks all entries in remote folder deleted
keeps versions for retention
folder itself remains
usage changes from current files to retained history
```

Hard purge:

```text
physically deletes unreferenced retained chunks after extra authorization and grace period
```

## Version list

Each file should expose version history:

```text
Current version
Previous versions
Deleted versions
Conflict versions
```

Actions per version:

```text
Download
Restore as current
Show details
Delete this retained version
```

Deleting a retained version is a purge-like operation and should be restricted.

## Remote file details

Details panel should show:

```text
name
path
logical size
remote stored size
modified time
uploaded/synced by device
current version ID
number of versions
deleted/recoverable status
```

Avoid showing raw internal IDs by default, but allow copy in advanced/debug UI.

## CAS conflicts during browser operations

Browser operations can conflict with sync operations from other devices.

Process:

```text
1. Client prepares manifest update based on revision N.
2. Server current is now N+1.
3. Server returns 409.
4. Client downloads latest manifest.
5. Client merges operation.
6. User may need to confirm if operation changed meaning.
```

For simple upload to new path:

```text
auto-merge
```

For delete of a path that changed:

```text
ask user
```

## Offline browser operations

Do not implement offline remote mutations in v1.

Allowed offline:

```text
view cached manifest if available
open previously downloaded files
```

Not allowed offline in v1:

```text
queue remote deletes
queue remote uploads
```

Reason: it complicates conflict behavior.

## Safety defaults

Remote browser default behavior:

```text
download is safe
upload creates new versions, not silent overwrite
delete is soft delete
clear is separate danger action
hard purge requires stronger authorization
```
