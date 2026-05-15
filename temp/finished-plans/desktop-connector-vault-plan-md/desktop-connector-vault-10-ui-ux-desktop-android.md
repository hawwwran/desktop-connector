# Desktop Connector Vault — 10 UI/UX: Desktop and Android

## Goal

Define how the feature should appear in the app.

## Desktop entry points

Add tray menu item:

```text
Vault
```

Possible submenu:

```text
Open Vault
Sync now
Pause all Vault sync
Export vault
Import vault
Settings
```

Launch GTK window:

```text
desktop-connector --gtk-window=vault
```

## Desktop main Vault window

Recommended sections:

```text
Header
Remote folders
Remote browser
Activity / sync status
Settings
```

Header:

```text
Vault Vault
Vault ID: H9K7-M4Q2-Z8TD   [Copy] [Show QR]
Remote storage used: 18.1 GB / 50 GB
Current files: 16.9 GB
Retained history: 1.2 GB
Relay: https://relay.example.com
```

## Remote folder list

Columns:

```text
Name
Binding
Current files
Stored
History
Status
Last sync
Actions
```

Example:

```text
Documents
  Bound: /home/michal/Documents
  Current: 4.1 GB
  Stored: 4.3 GB
  History: 300 MB
  Status: Syncing

Photos
  Browse-only
  Current: 12.8 GB
  Stored: 13.2 GB
  History: 900 MB
  Status: Not connected on this device
```

## Remote folder actions

```text
Browse
Connect local folder
Pause sync
Disconnect local folder
Sync now
Upload here
Download folder
Clear contents
Folder settings
```

## Remote browser

Toolbar:

```text
Back
Forward
Breadcrumb path
Upload files
Upload folder
New folder
Download
Delete
Versions
Refresh
```

File list:

```text
Name
Size
Modified
Versions
Status
```

Context menu:

```text
Download latest
Download previous version...
Upload new version...
Restore previous version...
Delete
Rename
Show details
```

## Version dialog

For a file:

```text
example.pdf

Current version:
  2026-05-02 17:30
  123 KB
  from Laptop

Previous versions:
  2026-05-01 21:10
  121 KB
  from Phone
  [Download] [Restore as current]
```

Important distinction:

```text
Download version = saves a copy outside sync
Restore version = creates a new current remote version
```

## Upload dialog

When uploading in browser mode:

```text
Upload to:
Vault / Documents / Invoices / 2026

Files:
  example.pdf
  invoice.pdf

If file exists:
  Add as new version
  Keep both with renamed copy
  Skip
```

Default:

```text
Ask.
```

If user explicitly selects "Upload new version" on one file:

```text
add as new version
```

## Delete confirmation

For file delete:

```text
Delete "example.pdf" from Vault?

This removes it from the current remote view.
Previous versions remain recoverable until 2026-06-01.
```

For folder delete:

```text
Delete folder "Invoices/2026"?

This will remove 128 files from the current remote view.
Previous versions remain recoverable for 30 days.
```

For clear main remote folder:

```text
Clear remote folder "Documents"?

This affects all devices syncing this folder.
Files may be removed from connected local folders during sync.
Previous versions remain recoverable for 30 days.

Type "Documents" to continue.
```

For clear whole vault:

```text
Clear whole vault H9K7-M4Q2-Z8TD?

This affects all remote folders and all connected devices.
Type the full Vault ID to continue.
```

## Import UX

Flow:

```text
Open Vault
→ Import vault
→ choose export file or enter relay/recovery
→ unlock
→ app shows summary
```

Summary example:

```text
Vault ID: H9K7-M4Q2-Z8TD
Export created: 2026-05-02 17:30
Remote folders: 3
Stored data: 18.1 GB
Current files: 16.9 GB
Versions/deleted retained: 1.2 GB
```

If target has same vault:

```text
A vault with this ID already exists on this relay.
It appears to be the same vault.
Import will merge missing data and preserve existing changes.
```

If target has different vault:

```text
A different vault with this ID already exists.
Automatic merge is blocked for safety.
```

## After import

Show:

```text
Import complete.
Remote folders are browse-only on this device.
Connect a local folder if you want to sync.
```

## Connect local folder UX

Flow:

```text
Remote folder → Connect local folder
→ choose local path
→ scan
→ preview
```

Preview examples:

### Empty local folder

```text
Remote has 4.1 GB in 2,430 files.
Local folder is empty.
Recommended: Restore remote files into this folder.
```

### Non-empty local folder

```text
Remote: 2,430 files
Local: 1,200 files
Conflicts: 12 paths

Recommended: Merge safely and keep both versions on conflict.
No local files will be deleted during initial binding.
```

## Export UX

```text
Vault settings
→ Export vault
→ choose destination
→ enter export passphrase
→ export
→ verify
```

After export:

```text
Export verified.
Store this file and passphrase safely.
```

## Migration UX

```text
Vault settings
→ Migrate to another relay
→ enter new relay URL
→ check capabilities
→ copy encrypted vault
→ verify
→ switch active relay
```

Do not switch until verified.

## Android UX

Android should support first:

```text
restore/import vault
view Vault ID
browse folders
download files
download previous versions
upload files
delete files if role allows
receive vault grant by QR
```

Android folder sync should come later.

Reason:

```text
Android background folder watching is not as reliable as desktop filesystem watching.
```

## Settings

Vault settings:

```text
Vault
  Vault ID
  Relay URL
  Storage usage
  Export
  Import
  Migrate relay

Recovery
  Recovery status
  Change recovery passphrase
  Export recovery kit
  Test recovery kit

Folders
  Add remote folder
  Retention policy
  Connected local folders

Devices
  Devices with vault access
  Roles
  Revoke device
  Grant device by QR

Danger zone
  Clear folder
  Clear whole vault
  Schedule hard purge
```

## Plain language rules

Use clear distinctions:

```text
Disconnect = stop syncing on this device
Delete = hide current file, keep recovery versions
Clear = delete all contents from remote view
Purge = permanently remove retained data from relay
Export = encrypted backup/migration file
Import = add/merge encrypted vault data
```

Do not use these interchangeably.
