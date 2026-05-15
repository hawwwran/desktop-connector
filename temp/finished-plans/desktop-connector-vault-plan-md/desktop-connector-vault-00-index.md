# Desktop Connector Vault — 00 Index

> **T0 lock takes precedence.** When this plan and [`desktop-connector-vault-T0-decisions.md`](desktop-connector-vault-T0-decisions.md) disagree, the T0 decisions doc wins. Implementation tracking lives in [`VAULT-progress.md`](VAULT-progress.md).

This is an incremental implementation plan for adding a robust **Vault** feature to Desktop Connector.

The feature is designed as a persistent encrypted vault inside the existing Desktop Connector ecosystem:

- account-less,
- relay-compatible,
- end-to-end encrypted,
- recoverable from user-held secrets,
- able to contain multiple remote synced folders,
- safe after import on a new device,
- browsable without local folder bindings,
- able to upload/delete/download previous versions directly from browser mode,
- exportable/importable for relay migration,
- protected against destructive sync and destructive-clear mistakes.

## File order

1. `desktop-connector-vault-01-current-app-fit-and-boundaries.md`
2. `desktop-connector-vault-02-product-model-vault-folders-bindings.md`
3. `desktop-connector-vault-03-crypto-recovery-identity.md`
4. `desktop-connector-vault-04-vault-storage-data-model-usage.md`
5. `desktop-connector-vault-05-server-api-and-relay-migration.md`
6. `desktop-connector-vault-06-export-import-protected-bundles.md`
7. `desktop-connector-vault-07-remote-browser-upload-delete-versions.md`
8. `desktop-connector-vault-08-sync-engine-folder-binding.md`
9. `desktop-connector-vault-09-destructive-actions-threat-model.md`
10. `desktop-connector-vault-10-ui-ux-desktop-android.md`
11. `desktop-connector-vault-11-implementation-roadmap-tests.md`

## Core design position

Vault should not be implemented as a hidden extension of the current transfer lifecycle.

Current transfer behavior is delivery-oriented:

```text
sender encrypts chunks
→ relay stores pending transfer
→ receiver downloads chunks
→ transfer is acknowledged
→ relay may delete transfer/chunks
```

Vault must be storage-oriented:

```text
folder/file changes
→ client encrypts chunks and manifest updates
→ relay stores persistent opaque vault data
→ any authorized device can browse, download, upload, sync, export, or migrate
```

## Most important rule

After importing/restoring a vault on a new device:

```text
No local folder is connected automatically.
No sync starts automatically.
No remote delete is applied to the local filesystem.
The vault opens in browse-only mode.
```

The user can then:

```text
browse remote files
download files/folders manually
upload files into remote folders
delete files remotely as soft-delete/tombstones
download previous versions
connect a local folder to a remote folder
start sync only after explicit preflight
```

## Vocabulary

### Vault

The top-level encrypted storage container.

It has:

```text
visible Vault ID
encrypted header
encrypted manifests
encrypted chunks
device grants
remote folders
usage metadata
```

### Remote folder

A top-level folder inside the vault.

Examples:

```text
Documents
Photos
Project Backup
```

A remote folder exists even if the current device has no local path connected to it.

### Local binding

A per-device mapping from a remote folder to a real filesystem location.

Example:

```text
Remote folder: Documents
Local path: /home/michal/Documents
State: bound / paused / unbound / error
```

### Browse-only mode

The device can browse the decrypted remote tree and manually upload/download/delete, but there is no automatic sync with a local folder.

### Sync mode

The device has a local binding and participates in folder synchronization.

## Security philosophy

The relay is never trusted with plaintext.

The relay may see:

```text
Vault ID
encrypted blob sizes
chunk counts
timestamps
quota/used-byte counters
request IPs
device IDs
```

The relay must not see:

```text
vault master key
recovery passphrase
plaintext filenames
plaintext folder names
plaintext file contents
plaintext file hashes
plaintext folder usage by name
```

## Recommended default feature behavior

Default vault import behavior:

```text
Import/restore vault
→ show visible Vault ID
→ show whole-vault used space
→ decrypt manifest locally
→ show remote folders and per-folder usage
→ all folders are unbound
→ browse/download/upload/delete allowed according to role
→ sync disabled until user connects local folder
```

Default delete behavior:

```text
Delete file/folder in browser
→ create encrypted tombstone
→ keep versions/chunks during retention
→ allow restore
```

Default clear behavior:

```text
Clear folder / clear whole vault
→ danger flow
→ soft clear first
→ hard purge only with stronger authorization
→ delayed garbage collection
```

Default export/import behavior:

```text
Export vault
→ create fully protected encrypted export bundle
→ import on another relay or device
→ if vault exists, verify same vault identity
→ merge revisions/chunks safely
→ never blindly overwrite existing vault
```
