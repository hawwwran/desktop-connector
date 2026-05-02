# Desktop Connector Vault — 01 Current App Fit and Boundaries

## Purpose

This document defines how Vault should fit into the existing Desktop Connector architecture.

The goal is to reuse what is already good in the app while avoiding a design that fights the current protocol.

## Existing strengths to reuse

Desktop Connector already has the right base concepts for Vault:

```text
Android app
Linux desktop app
PHP relay
device registration
QR-assisted pairing
X25519 identity keys
HKDF-derived symmetric keys
AES-256-GCM encrypted payloads
chunked upload/download
blind relay model
transfer queueing
long polling / catch-up behavior
multi-device thinking
desktop tray and GTK windows
Android share integration
```

This means Vault does not need to invent a separate ecosystem.

It should look like another major capability inside Desktop Connector:

```text
Send
Clipboard
Find device
History
Vault
```

## Existing model that must not be reused directly

The current transfer lifecycle is not suitable for persistent vault storage.

Current transfer:

```text
file selected
→ chunks encrypted
→ uploaded to relay as one pending transfer
→ receiver downloads
→ sender/receiver status changes
→ relay can clean it up
```

Vault:

```text
file/folder state changes
→ chunks encrypted
→ manifest revision updated
→ relay stores persistent encrypted state
→ other devices may browse/sync later
→ versions and deleted files remain recoverable during retention
```

Do not implement Vault as "very long-lived transfers".

That would create problems:

```text
transfer cleanup would fight retention
delivery state does not equal vault state
current recipient-specific model does not fit multi-device browse mode
ACK/delete semantics are dangerous for backups
existing pairwise keys do not fit vault recovery
```

## Protocol boundary

Keep current protocol behavior stable.

Add new capability:

```json
{
  "capabilities": [
    "transfer_v1",
    "fasttrack_v1",
    "vault_v1"
  ]
}
```

Do not change:

```text
existing pairing flow
existing transfer endpoints
existing file receive behavior
existing clipboard conventions
existing fasttrack command format
```

Add a new namespace:

```text
/api/vaults/...
```

or:

```text
/api/vault/...
```

Recommended naming:

```text
server API: /api/vaults/*
product/UI name: Vault
internal protocol term: vault
```

Reason: `vault` is clearer in API/data structures; `Vault` is the user-facing feature name.

## Key architectural separation

There should be three separate layers:

```text
Device identity layer
  - current Desktop Connector device registration
  - device_id
  - auth_token
  - public key

Pairing / device communication layer
  - existing QR pairing
  - pairwise keys
  - transfers
  - fasttrack

Vault layer
  - visible Vault ID
  - vault access secret
  - vault master key
  - encrypted manifests
  - persistent chunks
  - recovery
  - folder bindings
```

Vault should use device identity for API authentication, but vault access must be separate.

Why:

```text
A device can be valid for Desktop Connector
but not authorized for a specific vault.
```

## Existing relay trust model

Vault should preserve the existing blind relay idea.

The relay should be able to:

```text
store encrypted blobs
serve encrypted blobs
count storage usage
enforce quotas
validate bearer tokens
perform compare-and-swap on manifest revision numbers
delete unreachable chunks during authorized garbage collection
```

The relay should not be able to:

```text
decrypt file contents
decrypt filenames
decrypt folder names
recover a lost vault
decide file-level conflicts
inspect per-folder usage by folder name
merge manifests semantically
```

## Why vault-level keys are needed

Current pairwise keys are good for device-to-device transfer:

```text
Device A <-> Device B
```

Vault needs something different:

```text
Device A
Device B
Device C
future restored Device D
        ↓
same persistent vault
```

A vault-level key hierarchy is needed because a restored future device must decrypt old vault content even if it was not part of the original pairwise relationship.

Recommended:

```text
Vault Master Key
  → metadata encryption key
  → chunk key derivation key
  → manifest signing/authentication key
  → device grant wrapping key
```

## Compatibility requirement

Vault must be optional.

Older clients should continue to work with file transfer and clipboard features even if the relay supports Vault.

New clients should detect whether the relay supports Vault and show:

```text
Vault is not supported by this relay version.
Update the relay to use encrypted vaults.
```

## Development boundary

Do not implement full sync first.

The first useful Vault milestone should be:

```text
create vault
add remote folder
upload encrypted snapshot
restore vault on another device
browse remote tree
download files manually
```

Then add automatic sync.

Reason:

```text
browse-only restore validates the vault model without risking destructive filesystem behavior
```
