# Desktop Connector Vault — 06 Export, Import, and Protected Bundles

## Goal

Make vaults exportable/importable, including migration to a different relay server, while keeping exports fully protected.

## Export requirements

A vault export must:

```text
contain enough data to restore or migrate the vault
not expose plaintext filenames
not expose plaintext file contents
not expose vault master key
not be importable without proper recovery/unlock material
detect tampering
support large files/chunks
support resume or partial retry if possible
```

## Export format

Use one primary format first:

```text
.dc-vault-export
```

Optional later:

```text
.dc-vault-folder-export
.dc-vault-key-backup
```

## Two export security layers

The vault data is already encrypted internally.

But the export should still have an additional outer export envelope.

Recommended:

```text
inner layer:
  normal Vault encrypted vault header/manifests/chunks

outer layer:
  export bundle encrypted with export passphrase or export key file
```

This means a stolen export file does not even reveal Vault ID or chunk layout unless the attacker opens the export envelope.

## Export bundle structure

Plain conceptual structure before outer encryption:

```json
{
  "schema": "dc-vault-export-v1",
  "created_at": 1777650000,
  "source_relay_url": "https://old.example.com",
  "vault_id": "H9K7-M4Q2-Z8TD",
  "vault_genesis_fingerprint": "...",
  "export_type": "full_vault",
  "header": {
    "encrypted_header": "...",
    "header_revision": 5
  },
  "manifests": [
    {
      "revision": 42,
      "parent_revision": 41,
      "manifest_hash": "...",
      "manifest_ciphertext": "..."
    }
  ],
  "chunks": [
    {
      "chunk_id": "ch_...",
      "ciphertext_size": 2097300,
      "hash": "...",
      "stream_offset": 12345678
    }
  ],
  "usage": {
    "ciphertext_bytes": 18300000000,
    "manifest_bytes": 4200000
  }
}
```

The actual file should be streamable, not a huge JSON blob.

Recommended physical layout:

```text
export header
encrypted bundle index
encrypted manifest records
encrypted chunk records
final authentication tag / footer
```

## Outer export encryption

Recommended:

```text
export_passphrase
→ Argon2id
→ export_wrapping_key
→ decrypt random export_file_key

export_file_key
→ streaming AEAD encryption of bundle records
```

Do not rely on classic ZIP password encryption.

## Export UX

Export flow:

```text
Vault
→ Export vault
→ choose destination file
→ app requires vault unlock
→ app asks for export passphrase
→ app warns that export passphrase cannot be recovered
→ app creates encrypted export
→ app verifies export can be opened
```

## Export modes

### Full export

Contains:

```text
vault header
current manifest
retained manifest history needed for versions
all referenced chunks
tombstones and retained deleted versions
device grant metadata if appropriate
```

Use for migration and disaster recovery.

### Current-state-only export

Later enhancement.

### Folder export

Later enhancement.

## Export should not include active local bindings

A vault export should not export device-local paths as active bindings.

Default:

```text
local bindings are not restored
imported vault opens browse-only
```

Reason:

```text
local paths from one device are usually invalid or dangerous on another device
```

## Import requirements

Import must support:

```text
new vault on empty relay
merge into existing same vault
refuse wrong vault collision
browse-only after import
safe folder binding later
```

## Import flow

```text
Vault
→ Import vault
→ select .dc-vault-export
→ enter export passphrase
→ app opens protected bundle
→ app shows Vault ID and export summary
→ user chooses relay target
→ app checks whether vault exists
```

If vault does not exist:

```text
create vault on target relay
upload header
upload manifests
upload chunks
verify
open browse-only
```

If vault exists:

```text
download target vault header
unlock/decrypt both identities
compare genesis identity
if same: merge
if different: refuse
```

## Merge import behavior

When target has the same vault:

```text
copy missing chunks
copy missing manifest revisions
merge operation logs
preserve newer target changes
preserve imported changes
create new merge manifest revision if needed
```

Default must not be:

```text
replace existing vault
rollback target to imported revision
delete target-only files
purge target-only chunks
```

Default must be:

```text
add missing history and files
preserve both sides
show conflicts
```

## Import conflict handling

If imported and existing vault have same path with different latest content:

```text
keep existing file
import other version as conflict
```

Example:

```text
report.docx
report (imported conflict 2026-05-02 17-30).docx
```

Default:

```text
Keep both
```

## Import as new vault ID

If Vault ID collision exists but identity differs, v1 should refuse automatic import.

Reason:

```text
Vault ID may be part of AEAD associated data.
Safe import-as-new-ID may require re-encrypting manifests and possibly chunks.
```

Better v2 behavior:

```text
Import as new ID with controlled rewrap/re-encryption.
```

## Export verification

After creating export:

```text
open export envelope
verify header
verify index
verify manifest hashes
verify chunk count
verify chunk ciphertext hashes
optionally decrypt sample chunks
```

Show:

```text
Export verified successfully.
```

## Import verification

After import:

```text
verify uploaded chunk count
verify server-reported usage
download random sample chunks
verify manifest current revision
verify target vault can be opened
```

For full confidence:

```text
full verification mode
```

## Interrupted export/import

Export/import should be resumable.

For export:

```text
write temp file
checkpoint exported chunk IDs
finalize only after footer/tag written
```

For import:

```text
batch HEAD/check chunks
upload missing only
resume from last successful uploaded chunk
publish manifest only after chunks are present
```

## Security: malicious export file

Import parser must be strict.

Reject:

```text
unknown required schema version
oversized manifest
oversized chunk
path traversal attempts in metadata
invalid chunk IDs
duplicate chunk records with different hashes
manifest revision loops
invalid parent chains
compression bombs if compression is used
```

Even though contents are encrypted, the outer export file is attacker-controlled input.
