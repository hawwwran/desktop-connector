# Desktop Connector Tresor — 05 Server API and Relay Migration

## Goal

Define the persistent vault API and migration behavior for moving a vault to a different relay server.

## API namespace

Use:

```text
/api/vaults/*
```

The product name remains Tresor.

## Authentication layers

Authenticated vault endpoints should require:

```http
X-Device-ID: <device id>
Authorization: Bearer <device auth token>
X-Vault-ID: <visible vault id>
X-Vault-Authorization: Bearer <vault access secret or derived bearer token>
```

Device auth says:

```text
this is a registered Desktop Connector device
```

Vault auth says:

```text
this caller has access to this vault's opaque storage
```

Vault auth must not be derived from the Vault Master Key in a way that exposes decryption material to the server.

Recommended:

```text
vault_access_secret = random high-entropy bearer capability
server stores hash(vault_access_secret)
```

## Capability discovery

Add relay capability endpoint or extend existing status response:

```json
{
  "server": "desktop-connector-relay",
  "capabilities": [
    "transfer_v1",
    "fasttrack_v1",
    "tresor_v1",
    "tresor_manifest_cas_v1",
    "tresor_export_v1"
  ]
}
```

## Core endpoints

### Create vault

```http
POST /api/vaults
```

Request:

```json
{
  "vault_id": "H9K7-M4Q2-Z8TD",
  "vault_access_token_hash": "...",
  "encrypted_header": "...",
  "initial_manifest_ciphertext": "...",
  "initial_manifest_hash": "..."
}
```

### Get vault header

```http
GET /api/vaults/{vault_id}/header
```

Returns encrypted header.

### Update vault header

```http
PUT /api/vaults/{vault_id}/header
```

Use CAS for header updates.

### Get current manifest

```http
GET /api/vaults/{vault_id}/manifest
```

Response:

```json
{
  "revision": 42,
  "parent_revision": 41,
  "manifest_hash": "...",
  "manifest_ciphertext": "..."
}
```

### Put manifest with CAS

```http
PUT /api/vaults/{vault_id}/manifest
```

Request:

```json
{
  "expected_current_revision": 42,
  "new_revision": 43,
  "parent_revision": 42,
  "manifest_hash": "...",
  "manifest_ciphertext": "..."
}
```

If server current is not 42:

```http
409 Conflict
```

### Upload chunk

```http
PUT /api/vaults/{vault_id}/chunks/{chunk_id}
Content-Type: application/octet-stream
```

Server checks:

```text
vault auth
quota
chunk ID format
max chunk size
whether chunk already exists
```

### Download chunk

```http
GET /api/vaults/{vault_id}/chunks/{chunk_id}
```

Returns ciphertext only.

### Check chunk

```http
HEAD /api/vaults/{vault_id}/chunks/{chunk_id}
```

Used to skip already-uploaded chunks.

### Batch chunk check

```http
POST /api/vaults/{vault_id}/chunks/check
```

Useful for export/import migration and sync resume.

## Delete/clear endpoints

Do not create server endpoints like:

```text
DELETE /api/vaults/{vault_id}/folders/Documents
```

That would push too much semantic authority to the server.

Instead:

```text
soft delete = encrypted manifest update
hard purge = authorized GC of unreferenced chunks after retention
```

## Garbage collection endpoint

```http
POST /api/vaults/{vault_id}/gc/plan
```

Request:

```json
{
  "manifest_revision": 60,
  "encrypted_gc_auth": "...",
  "candidate_chunk_ids": ["ch_..."]
}
```

Server should not decide file-level deletion.

The client provides candidate chunks that are no longer referenced according to decrypted manifests and retention policy.

## Relay migration modes

### Mode 1: Protected export file

```text
Old relay → client downloads encrypted vault bundle
Client writes protected export file
New relay → client imports bundle
```

Best for offline backup/manual migration.

### Mode 2: Direct relay-to-relay migration through client

```text
client connects old relay
client connects new relay
client streams encrypted objects old → new
client verifies hashes
client publishes imported manifest
```

The old and new relays never see plaintext.

### Mode 3: Folder-level export/import

Later enhancement.

## Migration to different relay

User flow:

```text
Tresor settings
→ Migrate vault to another relay
→ enter new relay URL
→ authenticate/register device on new relay
→ choose "copy existing vault"
→ app uploads encrypted header, manifests, chunks
→ app verifies all chunks
→ app switches local vault remote URL only after successful verification
```

Critical rule:

```text
Do not change the active local vault URL until the target relay has a complete verified copy.
```

## Target relay already has vault with same ID

On import/migration:

```text
1. Check if vault ID exists.
2. If not, create it.
3. If yes, download existing encrypted header/manifest.
4. Ask user for existing vault recovery/unlock if needed.
5. Decrypt both vault identities client-side.
6. Compare genesis vault identity.
```

### Same vault identity

Allow merge.

```text
same Vault ID
same genesis identity
different manifest revisions
→ merge revisions/chunks
```

### Different vault identity

Refuse automatic merge.

Offer:

```text
Cancel
Use another relay
Import as new vault ID later, after rewrap support exists
```

Never merge two cryptographically different vaults just because visible Vault ID matches.

## Merge strategy

When importing into an existing same-identity vault:

```text
copy missing chunks
copy missing manifest revisions
load current target manifest
load imported manifest
merge operation logs
create new merge manifest revision if needed
```

If both sides changed same file path:

```text
keep both versions
create conflict record
do not overwrite silently
```

If imported manifest is older:

```text
offer to keep as historical revision only
do not roll back active head automatically
```

Default:

```text
merge, preserve both, no silent destructive changes
```

## Migration verification

After import/migration:

```text
verify manifest hash
verify chunk count
verify total ciphertext bytes
verify random sample of chunks
optionally verify all chunk MACs by decrypting
show success only after verification
```

## API abuse controls

Since this is account-less:

```text
rate-limit vault auth attempts
rate-limit create vault
rate-limit join requests
limit max chunk size
limit max manifest size
limit max chunks per batch
expire incomplete uploads
```
