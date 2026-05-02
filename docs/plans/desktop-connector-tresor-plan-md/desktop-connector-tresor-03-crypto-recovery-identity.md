# Desktop Connector Tresor — 03 Crypto, Recovery, and Identity

## Goal

Tresor must be account-less but recoverable.

This means the app cannot rely on email/password reset.

Recovery must be based on user-held secrets.

## Key roles

Tresor needs separate concepts:

```text
Vault ID
  public identifier, visible in app

Vault Access Secret
  authorizes API access to opaque vault blobs

Vault Master Key
  decrypts vault metadata and chunk keys

Recovery Secret / Recovery Kit
  allows restoring Vault Master Key

Recovery Passphrase
  protects recovery material from theft

Device Grant
  allows a specific device to use the vault without re-entering recovery every time
```

## Recommended key hierarchy

Generate a random 256-bit Vault Master Key.

Derive subkeys:

```text
Vault Master Key
  ↓ HKDF-SHA256 or BLAKE3 keyed derivation

metadata_key
chunk_key_derivation_key
manifest_auth_key
folder_key_wrapping_key
device_grant_key
export_auth_key
```

Recommended labels:

```text
dc-tresor-v1/metadata
dc-tresor-v1/chunk
dc-tresor-v1/manifest-auth
dc-tresor-v1/folder-wrap
dc-tresor-v1/device-grant
dc-tresor-v1/export
```

Do not use the existing pairwise transfer key as the vault key.

## Encryption primitives

Recommended:

```text
metadata/manifests: XChaCha20-Poly1305 or AES-256-GCM-SIV
chunks: XChaCha20-Poly1305 or AES-256-GCM-SIV
key wrapping: XChaCha20-Poly1305 or AES-KW + AEAD metadata
KDF from passphrase: Argon2id
```

If staying consistent with current app is more important for early implementation, AES-256-GCM can be used, but only with strict nonce uniqueness and test vectors.

For a long-lived vault, misuse-resistant AEAD is preferable.

## Associated data

Every encrypted object should authenticate context using AAD.

Examples:

### Manifest AAD

```text
dc-tresor-manifest-v1
vault_id
manifest_revision
parent_revision
author_device_id
```

### Chunk AAD

```text
dc-tresor-chunk-v1
vault_id
remote_folder_id
file_id
file_version_id
chunk_index
chunk_plaintext_size
```

### Header AAD

```text
dc-tresor-header-v1
vault_id
schema_version
```

This prevents ciphertext from being moved between contexts undetected.

## Vault header

The vault header is stored on the relay and in exports.

It contains only encrypted or non-secret values.

Conceptual structure:

```json
{
  "schema": "dc-tresor-header-v1",
  "vault_id": "H9K7-M4Q2-Z8TD",
  "created_at": 1777650000,
  "kdf_profiles": {
    "recovery": "argon2id-v1"
  },
  "encrypted_recovery_envelopes": [
    {
      "type": "recovery-kit-passphrase",
      "id": "rk_...",
      "kdf": "...",
      "ciphertext": "..."
    }
  ],
  "encrypted_device_grants": [
    {
      "device_id": "...",
      "grant_id": "...",
      "role": "admin",
      "ciphertext": "..."
    }
  ],
  "genesis_vault_fingerprint": "..."
}
```

## Vault fingerprint

To safely detect whether two vaults are the same during import/merge, each vault should have a stable internal identity.

Recommended:

```text
genesis_vault_secret = random 256-bit value
genesis_vault_fingerprint = BLAKE3/HMAC value derived from it
```

The fingerprint is stored encrypted in the manifest or header and compared client-side after decrypting.

Purpose:

```text
same visible Vault ID + same genesis identity = same vault
same visible Vault ID + different genesis identity = collision or wrong import target
```

If collision is detected:

```text
Refuse merge.
Offer "Import as new vault ID" only in a later version.
```

## Recovery modes

### Recommended default: recovery kit + passphrase

The recovery kit contains high-entropy recovery material.

The passphrase protects it.

Possible forms:

```text
printable QR
recovery file
24-word phrase
```

The recovery kit should be exportable after vault creation.

Flow:

```text
User creates vault
→ app generates Vault Master Key
→ app generates Recovery Secret
→ app asks user for recovery passphrase
→ app encrypts Vault Master Key into recovery envelope
→ app creates printable/exportable recovery kit
```

Recovery:

```text
User enters/imports recovery kit
→ enters passphrase
→ app unwraps Vault Master Key
→ app downloads/decrypts vault manifest
```

### Passphrase-only mode

This is less safe.

If implemented:

```text
passphrase
→ Argon2id with strong memory/time cost
→ wrapping key
→ decrypt Vault Master Key envelope
```

Warnings:

```text
No password reset exists.
Weak passphrases can be guessed offline if encrypted vault data is stolen.
Use a long unique passphrase.
```

Do not make weak passwords acceptable.

## Device grants

A device grant stores the Vault Master Key or a derived vault-unlock key encrypted for local device use.

Desktop:

```text
store in system keyring if available
fallback to encrypted local config only with explicit warning
```

Android:

```text
store using Android Keystore where possible
```

Device grant should include:

```json
{
  "device_id": "...",
  "role": "admin | sync | browse | read_only",
  "created_at": 1777650000,
  "granted_by_device_id": "...",
  "permissions": [
    "browse",
    "download",
    "upload",
    "sync",
    "delete_soft",
    "purge",
    "export",
    "grant_device"
  ]
}
```

## Permission model

Tresor should not assume that every device with vault access can perform all actions.

Recommended roles:

### Owner/Admin

```text
browse
download
upload
sync
soft delete
clear folder
export
import/merge
grant devices
change recovery
request hard purge
```

### Sync device

```text
browse
download
upload
sync
soft delete
```

### Browse device

```text
browse
download
manual upload optional
no sync
no destructive actions by default
```

### Read-only device

```text
browse
download
download previous versions
no upload
no delete
no sync
no export
```

Why this matters:

```text
If a phone is compromised, the blast radius can be smaller.
```

## QR-assisted device addition

There should be three QR types, clearly separated.

### Pairing QR

Existing app device-pairing QR.

Purpose:

```text
connect two Desktop Connector devices
```

Must not contain vault recovery material.

### Vault join QR

Short-lived QR used to add a new device to an existing vault.

Purpose:

```text
grant vault access to a new device
```

Must contain only:

```text
relay URL
Vault ID
join request ID
ephemeral public key
expiry
```

Must not contain:

```text
Vault Master Key
Recovery Secret
Recovery Passphrase
```

### Recovery QR

Long-lived emergency material.

Purpose:

```text
restore vault if devices are lost
```

This is sensitive backup material.

UI must treat it like a password vault backup, not like a pairing QR.

## QR join flow

```text
1. New device chooses "Join existing Tresor from another device".
2. New device creates ephemeral key pair.
3. New device shows or sends join request.
4. Existing authorized device displays approval dialog.
5. User verifies short code on both devices.
6. Existing device wraps vault grant for new device.
7. New device stores grant securely.
8. Join token expires and cannot be reused.
```

## Recovery and import on new device

Recovery should not imply sync.

After successful recovery:

```text
device has Vault Master Key
device can decrypt manifest
device shows remote folders
all folders are unbound
browser mode is enabled
```

Only after explicit local-folder binding does sync begin.

## Secret rotation

Future enhancement, but the data model should allow it:

```text
rotate vault access secret
rotate recovery envelope
remove device grant
rotate folder key
```

Removing a device grant does not automatically revoke data already copied to that device.

The UI must be honest about that.

## Offline attack resistance

If encrypted vault header and recovery envelope leak, attacker may attempt offline guessing of passphrase.

Mitigations:

```text
recovery kit contains high entropy
passphrase protects recovery kit
Argon2id with strong cost
reject short passphrases for passphrase-only mode
allow recovery file/QR instead of memorized weak password
```

## No reset rule

The app must say clearly:

```text
If you lose all devices and your recovery material/passphrase, the vault cannot be recovered.
```

That is required for true account-less encryption.
