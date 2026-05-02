# Desktop Connector Tresor — 04 Vault Storage, Data Model, and Usage Accounting

## Goal

Define persistent relay storage and client-side vault data structures, including visible Vault ID and used-space reporting.

## Storage principle

The relay stores opaque encrypted objects.

The client understands vault semantics.

```text
Relay:
  stores encrypted header, manifests, chunks, and revision metadata

Client:
  decrypts manifest
  computes file tree
  computes per-folder usage
  renders browser
  resolves conflicts
```

## Server-side objects

Recommended tables or storage collections:

```text
vaults
vault_manifests
vault_chunks
vault_chunk_uploads
vault_join_requests
vault_audit_events
vault_gc_jobs
```

## `vaults`

```text
vault_id
vault_access_token_hash
encrypted_header
current_manifest_revision
current_manifest_hash
created_at
updated_at
used_ciphertext_bytes
chunk_count
quota_ciphertext_bytes
soft_deleted_at
pending_purge_at
```

The relay can keep total used bytes because it stores chunks.

It should not know folder names or per-folder usage.

## `vault_manifests`

```text
vault_id
revision
parent_revision
manifest_hash
manifest_ciphertext
manifest_size
author_device_id
created_at
```

The manifest is encrypted.

The server only needs revision and hash for CAS and integrity tracking.

## `vault_chunks`

```text
vault_id
chunk_id
ciphertext_size
created_at
last_referenced_at
state
storage_path
```

Possible states:

```text
active
retained
gc_pending
purged
```

The server does not know which plaintext file a chunk belongs to.

## Encrypted manifest structure

Conceptual plaintext before encryption:

```json
{
  "schema": "dc-tresor-manifest-v1",
  "vault_id": "H9K7-M4Q2-Z8TD",
  "revision": 42,
  "parent_revision": 41,
  "created_at": 1777650000,
  "author_device_id": "desktop_...",
  "remote_folders": [
    {
      "remote_folder_id": "rf_...",
      "name": "Documents",
      "state": "active",
      "retention": {
        "keep_deleted_days": 30,
        "keep_versions": 10
      },
      "entries": []
    }
  ],
  "operation_log_tail": []
}
```

For first implementation, one encrypted manifest is acceptable.

Later, split into:

```text
vault manifest
folder manifests
file-version indexes
operation log segments
chunk index pages
```

to avoid rewriting a giant manifest.

## File entry

```json
{
  "entry_id": "fe_...",
  "type": "file",
  "path": "Invoices/2026/example.pdf",
  "latest_version_id": "fv_...",
  "deleted": false,
  "versions": [
    {
      "version_id": "fv_...",
      "created_at": 1777650000,
      "modified_at": 1777649000,
      "logical_size": 123456,
      "ciphertext_size": 124004,
      "content_fingerprint": "client-side-secret-mac",
      "chunks": [
        {
          "chunk_id": "ch_...",
          "index": 0,
          "plaintext_size": 123456,
          "ciphertext_size": 123500
        }
      ],
      "author_device_id": "desktop_..."
    }
  ]
}
```

## Tombstone

Delete operations should create tombstones, not immediately purge chunks.

```json
{
  "entry_id": "fe_...",
  "path": "Invoices/2026/example.pdf",
  "deleted": true,
  "deleted_at": 1777660000,
  "deleted_by_device_id": "desktop_...",
  "delete_operation_id": "op_...",
  "recoverable_until": 1780252000
}
```

## Operation log

Keep an encrypted operation log tail for browser history, auditing, and safer merge.

Example operations:

```text
folder_created
file_uploaded
file_version_added
file_deleted
file_restored
folder_cleared
folder_binding_created
device_granted
device_revoked
```

The operation log is encrypted.

The server does not inspect it.

## Usage accounting

The user requested:

```text
Vault ID visible
used space for each main folder
used space for whole vault
```

There are two different usage numbers.

### Logical usage

Plaintext logical file sizes.

Example:

```text
Documents: 4.1 GB logical
Photos: 12.8 GB logical
Whole vault: 16.9 GB logical
```

Computed by client from decrypted manifest.

### Remote storage usage

Actual encrypted storage used on relay.

Example:

```text
Documents: 4.3 GB stored
Photos: 13.2 GB stored
Whole vault: 18.1 GB stored
```

Whole-vault stored usage can be returned by server because server knows encrypted blob sizes.

Per-folder stored usage should be computed by client from decrypted manifest chunk references.

## Deduplication and usage attribution

If the same chunk is referenced by multiple versions or folders, usage accounting must define a policy.

Recommended display:

```text
Logical size:
  simple sum of current visible file sizes

Stored size:
  unique encrypted chunks referenced by this folder
```

For whole vault:

```text
Stored size = unique chunks in whole vault + manifest/header overhead
```

Recommended for v1:

```text
No cross-folder deduplication.
Deduplicate only within one folder or one file/version chain.
```

Then per-folder stored sizes add up predictably.

## Usage UI labels

Avoid ambiguous "size".

Use:

```text
Logical size
Remote storage used
Retained versions
Deleted files retained
```

Example:

```text
Vault H9K7-M4Q2-Z8TD
Remote storage used: 18.1 GB / 50 GB
Logical current files: 16.9 GB
Retained history: 1.2 GB

Folders:
  Documents
    Current files: 4.1 GB
    Stored encrypted data: 4.3 GB
    History/deleted retained: 300 MB
```

## Server quota

Server should enforce quota on ciphertext bytes.

```text
used_ciphertext_bytes + new_upload_size <= quota_ciphertext_bytes
```

## Chunk ID

Chunk ID should not be plaintext content hash unless it is keyed.

Recommended:

```text
chunk_id = random 128/256-bit ID
```

or:

```text
chunk_id = HMAC(chunk_id_key, plaintext_chunk_hash || file_version_id || chunk_index)
```

Random chunk IDs are simpler and leak less.

## Manifest integrity

Each manifest revision should include:

```text
revision number
parent revision
manifest hash
author device
timestamp
```

Server CAS checks:

```text
client update from revision N
server current revision must be N
otherwise 409 conflict
```

Client verifies:

```text
manifest decrypts
parent_revision matches expected when applicable
manifest_hash matches ciphertext
operation log is consistent
chunk references are valid
```
