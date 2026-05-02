# Vault — `vault_v1` Wire Protocol

Status: draft, frozen at T0
Audience: server, desktop, and (future) Android maintainers
Scope: HTTP wire shapes for every vault endpoint, capability discovery, error envelope, and idempotency rules

This document layers on top of [`protocol.md`](protocol.md). Shapes for transfer, pairing, fasttrack, and device registration live there; this file covers the `/api/vaults/*` namespace and the vault capability bits added to `/api/health`.

When this document disagrees with [`desktop-connector-vault-T0-decisions.md`](../plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md), T0 wins. Byte-exact constructions (AAD strings, HKDF labels, manifest envelope, chunk envelope, recovery envelope, export bundle CBOR records, device-grant material) live in [`vault-v1-formats.md`](vault-v1-formats.md), drafted in T0.3.

---

## 1. Versioning and capability bits

Vault support is gated on capability bits advertised by `/api/health`. Old transfer-only relays advertise none of the `vault_*` bits and continue to work for transfers/fasttrack.

The `vault_v1` aggregate bit is **only** present when the relay implements every T1 mandatory bit. Clients gating on `vault_v1` get a complete v1 surface; clients gating on a finer bit get the corresponding feature without depending on later phases.

| Bit | Phase | Meaning |
|---|---|---|
| `vault_v1` | T1 | Aggregate: relay supports the v1 vault surface (implies all T1 bits below). |
| `vault_create_v1` | T1 | `POST /api/vaults`. |
| `vault_header_v1` | T1 | `GET/PUT /api/vaults/{id}/header`. |
| `vault_manifest_cas_v1` | T1 | Manifest CAS PUT with `expected_current_revision` (§A1 conflict shape). |
| `vault_chunk_v1` | T1 | Chunk PUT/GET/HEAD + `chunks/batch-head`. |
| `vault_gc_v1` | T1 | `POST /api/vaults/{id}/gc/plan` + `…/gc/execute` + `…/gc/cancel`. |
| `vault_soft_delete_v1` | T7 | Server understands tombstone retention semantics for GC. |
| `vault_export_v1` | T8 | Quota and headers handle large continuous transfers (export bundle is otherwise client-side). |
| `vault_migration_v1` | T9 | `POST/GET/PUT /api/vaults/{id}/migration/*` (§H2 state machine). |
| `vault_grant_qr_v1` | T13 | Join-request, device-grant, and access-secret-rotation endpoints. |
| `vault_purge_v1` | T14 | Delayed hard-purge job tracking on the server. |

Source of truth: T0 §D12. Adding bits is fine; renames or meaning-changes are not.

If a client requires a bit the relay doesn't advertise, the client refuses with `vault_protocol_unsupported` 426 and surfaces:

> "This relay does not support `<feature>`. Update the relay or use a different one."

### Health response shape

```http
GET /api/health
```

```json
{
  "ok": true,
  "server": "desktop-connector-relay",
  "capabilities": [
    "transfer_v1",
    "fasttrack_v1",
    "stream_v1",
    "vault_v1",
    "vault_create_v1",
    "vault_header_v1",
    "vault_manifest_cas_v1",
    "vault_chunk_v1",
    "vault_gc_v1",
    "vault_migration_v1",
    "vault_grant_qr_v1"
  ]
}
```

Existing `transfer_v1` / `fasttrack_v1` / `stream_v1` bits are unchanged. The capability list is unordered; clients match by membership, not position.

---

## 2. Authentication

Every authenticated vault endpoint requires both **device auth** (per `protocol.md` §3.3) and **vault auth**:

```http
X-Device-ID: <device_id>
Authorization: Bearer <device_auth_token>
X-Vault-ID: <vault_id>
X-Vault-Authorization: Bearer <vault_access_secret>
```

| Header | Source | Failure |
|---|---|---|
| `X-Device-ID` + `Authorization` | Existing device registration (`POST /api/devices/register`). | 401 `vault_auth_failed` with `details.kind = "device"`. |
| `X-Vault-ID` + `X-Vault-Authorization` | Vault creation or device-grant approval. The relay stores `hash(vault_access_secret)`; the secret never leaves clients except over the QR-grant flow (§8). | 401 `vault_auth_failed` with `details.kind = "vault"`. |

The vault access secret is a high-entropy bearer capability. It is **not** derived from the Vault Master Key — the relay must never see decryption material.

`X-Vault-ID` redundantly mirrors the path's `{vault_id}`. Mismatch is 400 `vault_invalid_request` with `details.field = "vault_id"`.

### Endpoints that bypass vault auth

- `POST /api/vaults` — creates the vault and its access secret; vault doesn't exist yet.
- `POST /api/vaults/{id}/join-requests/{req_id}/claim` — claimant uses the QR's `join_request_id` as the per-claim secret; vault auth comes after grant approval (§8).

Device auth is **required** on every `/api/vaults/*` endpoint without exception.

### Role enforcement

T0 §D11 defines four roles: `read-only`, `browse-upload`, `sync`, `admin`. The role is stored on the device-grant row and checked on every authenticated request. Per-endpoint role gating is documented inline below.

Insufficient role surfaces 403 `vault_access_denied` with `details.required_role = "<role>"`. Hard-purge specifically uses `vault_purge_not_allowed` so the UI can offer "ask an admin device to purge".

---

## 3. Identifier formats

### Vault ID

Vault IDs are 12 base32 characters arranged as three groups of four separated by `-`:

```text
H9K7-M4Q2-Z8TD
```

The dashes are **display-only**. On the wire and in URLs the canonical form is the 12 base32 chars without dashes; servers normalize by stripping `-` and uppercasing before matching. UI surfaces always render with dashes for readability.

Path templates accept either form: `/api/vaults/H9K7M4Q2Z8TD` and `/api/vaults/H9K7-M4Q2-Z8TD` resolve to the same vault. Byte-exact alphabet and hash-truncation rules: see `vault-v1-formats.md`.

### Chunk ID

Chunk IDs use a strict prefix: `^ch_v1_[a-z2-7]{24}$` — literal `ch_v1_` plus 24-char RFC 4648 base32 lowercase. The server rejects any deviation with 400 `vault_invalid_request` and `details.field = "chunk_id"`. The `v1` namespace prevents a future v2 chunk from being silently stored by a v1 server. (T0 §A19.)

### Manifest revision

`revision` is a non-negative integer monotonically incremented by the server on each successful CAS publish. Genesis is `revision = 1`. `parent_revision` chains to the predecessor; the server validates `parent_revision == revision - 1` on publish.

---

## 4. Common envelopes

### Success

Endpoints that return data wrap it in:

```json
{
  "ok": true,
  "data": { … }
}
```

Endpoints with no body return **204 No Content** (no envelope).

Binary payloads (chunk download) bypass the envelope and return raw bytes with `Content-Type: application/octet-stream`.

### Error

Every vault error response uses the stable envelope from T0:

```json
{
  "ok": false,
  "error": {
    "code": "vault_manifest_conflict",
    "message": "The vault manifest changed on the server.",
    "details": { … }
  }
}
```

- `code` is mandatory and stable. Clients gate behavior on it; messages are human-readable English (clients may localize).
- `details` is per-code; fields not listed in the T0 table are reserved. Clients ignore unknown fields.
- HTTP status codes match the T0 §"Error codes" table.

The full code table — including required `details` fields and retry classes (`auto` / `user-action` / `permanent` / `info`) — lives in [T0 §"Error codes (vault_v1)"](../plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md#error-codes-vault_v1). This document references codes by name. Treat that table as authoritative for retry behavior.

---

## 5. Idempotency rules

| Method / endpoint shape | Idempotent? | Notes |
|---|:---:|---|
| `POST /api/vaults` | no | Re-creating with the same `vault_id` returns 409 `vault_already_exists`. |
| `PUT /api/vaults/{id}/header` | yes (CAS) | `expected_header_revision` mismatch returns 409 `vault_manifest_conflict`. |
| `PUT /api/vaults/{id}/manifest` | yes (CAS) | `expected_current_revision` mismatch returns 409 with the §A1 shape. |
| `PUT /api/vaults/{id}/chunks/{chunk_id}` | yes | Same `chunk_id` + same ciphertext: 200 OK. Same id + different ciphertext: 422 `vault_chunk_size_mismatch` or `vault_chunk_tampered`. |
| `GET` / `HEAD` (any) | yes | Pure reads. |
| `POST /chunks/batch-head` | yes | Pure read; no state. |
| `POST /gc/plan` | yes | Same input set returns the same plan (until the plan TTL elapses). |
| `POST /gc/execute` | yes | Already-purged chunks are no-ops; re-execute returns the same outcome counts. |
| `POST /gc/cancel` | yes | Re-cancelling an already-cancelled / already-elapsed job is 204. |
| `POST /migration/start` | yes | Returns the same `migration_token` on repeat (§H2). Different `target_relay_url` mid-flight returns 409 `vault_migration_in_progress`. |
| `GET /migration/verify-source` | yes | Pure read. |
| `PUT /migration/commit` | yes | Repeat returns 200 with the original `committed_at`. |
| `POST /join-requests` | no | Each call creates a new request with a new `join_request_id`. |
| `POST /join-requests/{id}/claim` | yes | Repeat claim from the same device with the same pubkey returns the same outcome. |
| `POST /join-requests/{id}/approve` | yes | Repeat approve with the same wrapped material is a no-op. |
| `DELETE /join-requests/{id}` | yes | Already-rejected requests return 204. |
| `DELETE /device-grants/{id}` | yes | Already-revoked grants return 204. |
| `POST /access-secret/rotate` | no | The old secret only validates once; a second call with the same `(old, new)` after success fails 401 `vault_auth_failed`. |

Clients should retry idempotent endpoints freely on transient errors (`vault_storage_unavailable`, `vault_chunk_missing` within budget, network errors). Non-idempotent endpoints surface explicit user state (already-exists, authentication moved on) rather than retrying silently.

---

## 6. T1 — Foundational endpoints

All endpoints under §6 require both device and vault auth unless noted. Capability bits per §1; role enforcement per §2.

### 6.1 Create vault

```http
POST /api/vaults
```

**Auth**: device only (vault doesn't exist yet).
**Capability**: `vault_create_v1`.

Request:

```json
{
  "vault_id": "H9K7-M4Q2-Z8TD",
  "vault_access_token_hash": "<base64 hash(vault_access_secret) — server stores as-is>",
  "encrypted_header": "<base64 ciphertext>",
  "header_hash": "<hex sha-256>",
  "initial_manifest_ciphertext": "<base64 ciphertext>",
  "initial_manifest_hash": "<hex sha-256>"
}
```

Success: **201 Created**

```json
{
  "ok": true,
  "data": {
    "vault_id": "H9K7-M4Q2-Z8TD",
    "header_revision": 1,
    "manifest_revision": 1,
    "quota_ciphertext_bytes": 1073741824,
    "used_ciphertext_bytes": 0,
    "created_at": "2026-05-02T10:00:00Z"
  }
}
```

The creator's device is **not** automatically issued a server-side device grant by this endpoint. The first device authenticates on subsequent requests by knowing `vault_access_secret` (which it generated and supplied as `hash` here). Bringing a second device in goes through the §8 grant flow.

Errors:
- 409 `vault_already_exists` — `details.vault_id` matches an existing vault on this relay.
- 400 `vault_invalid_request` — malformed fields (vault_id format, hash length, missing initial manifest).
- 426 `vault_protocol_unsupported` — relay does not advertise `vault_create_v1`.

### 6.2 Get vault header

```http
GET /api/vaults/{vault_id}/header
```

**Auth**: vault auth, any role.
**Capability**: `vault_header_v1`.

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "vault_id": "H9K7-M4Q2-Z8TD",
    "encrypted_header": "<base64>",
    "header_hash": "<hex>",
    "header_revision": 5,
    "quota_ciphertext_bytes": 1073741824,
    "used_ciphertext_bytes": 524288000,
    "migrated_to": null
  }
}
```

`migrated_to` is set to the target relay URL after a §7 migration commits. Clients see this on their next header fetch and switch active relay automatically (§H2 multi-device propagation).

`quota_ciphertext_bytes` and `used_ciphertext_bytes` drive the 80 / 90 / 100 % pressure bands without an extra round-trip (T1.8).

Errors: 401 `vault_auth_failed`, 404 `vault_not_found`, 422 `vault_header_tampered`.

### 6.3 Update vault header

```http
PUT /api/vaults/{vault_id}/header
```

**Auth**: vault auth, role `admin`.
**Capability**: `vault_header_v1`.

Request:

```json
{
  "expected_header_revision": 5,
  "new_header_revision": 6,
  "encrypted_header": "<base64>",
  "header_hash": "<hex>"
}
```

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "header_revision": 6,
    "header_hash": "<hex>"
  }
}
```

CAS-protected. On revision mismatch returns 409 `vault_manifest_conflict` with `details.current_revision` (the header's) and `details.expected_revision = 5`. The client re-fetches the header (§6.2) and retries with the right base.

Header changes are admin-tier because they include `migrated_to` and other vault-bedrock state.

### 6.4 Get current manifest

```http
GET /api/vaults/{vault_id}/manifest
```

**Auth**: vault auth, any role.
**Capability**: `vault_manifest_cas_v1`.

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "revision": 42,
    "parent_revision": 41,
    "manifest_hash": "<hex>",
    "manifest_ciphertext": "<base64>",
    "manifest_size": 184320
  }
}
```

`manifest_size` is the ciphertext byte length (used by the client for memory preflight before decoding base64).

### 6.5 Get specific manifest revision

```http
GET /api/vaults/{vault_id}/manifest/revisions/{revision}
```

**Auth**: vault auth, any role.
**Capability**: `vault_manifest_cas_v1`.

Same response shape as §6.4. Used by the activity timeline (T17.1) and "restore folder to date" (T11.5). Returns 404 `vault_not_found` (with `details.revision`) for unknown revisions.

Revisions are immutable once written. Servers retain every revision referenced by any current op-log segment or by the activity-window policy.

### 6.6 Publish manifest (CAS)

```http
PUT /api/vaults/{vault_id}/manifest
```

**Auth**: vault auth, role `browse-upload` or higher.
**Capability**: `vault_manifest_cas_v1`.

Request:

```json
{
  "expected_current_revision": 42,
  "new_revision": 43,
  "parent_revision": 42,
  "manifest_hash": "<hex>",
  "manifest_ciphertext": "<base64>"
}
```

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "revision": 43,
    "manifest_hash": "<hex>"
  }
}
```

**Conflict (CAS mismatch) — §A1 shape:**

```http
HTTP/1.1 409 Conflict
```

```json
{
  "ok": false,
  "error": {
    "code": "vault_manifest_conflict",
    "message": "The vault manifest changed on the server.",
    "details": {
      "current_revision": 44,
      "expected_revision": 42,
      "current_manifest_hash": "<hex>",
      "current_manifest_ciphertext": "<base64>",
      "current_manifest_size": 192384
    }
  }
}
```

The server returns the **current** manifest ciphertext + hash + revision in the error. The client never has to issue a follow-up `GET /manifest` after a 409 — it has everything it needs to run the §D4 merge algorithm and retry. This keeps the retry loop one round-trip and removes the race between 409 and a separate GET landing on a yet-newer revision.

If the new manifest references chunks not yet uploaded, **or** would push `used_ciphertext_bytes > quota_ciphertext_bytes`, the server may reject before storing the manifest:

- 422 `vault_chunk_missing` with `details.chunk_id` (first missing chunk encountered). Client uploads the chunk(s) and re-publishes.
- 507 `vault_quota_exceeded` with `details.{used_bytes, quota_bytes, eviction_available}`. Client runs the §D2 eviction pass (when `eviction_available=true`) or surfaces "vault full, sync stopped".

Other errors: 401 `vault_auth_failed`, 403 `vault_access_denied`, 422 `vault_manifest_tampered`, 422 `vault_format_version_unsupported`.

### 6.7 Get archived op-log segment

```http
GET /api/vaults/{vault_id}/op-log-segments/{segment_id}
```

**Auth**: vault auth, any role.
**Capability**: `vault_v1` (no separate bit; segments are part of the manifest model per §D14).

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "segment_id": "<id>",
    "seq": 7,
    "first_ts": "2026-04-15T08:30:00Z",
    "last_ts": "2026-04-22T14:12:00Z",
    "ciphertext": "<base64>",
    "hash": "<hex>",
    "created_at": "2026-04-22T14:12:00Z"
  }
}
```

Segments are immutable once written. Clients fetch on demand based on the `archived_op_segments` list in the current manifest header. Returns 404 `vault_not_found` (with `details.segment_id`) if the segment is unknown — this can happen on a segment garbage-collected after no current manifest references it (T0 §D14).

### 6.8 Upload chunk

```http
PUT /api/vaults/{vault_id}/chunks/{chunk_id}
Content-Type: application/octet-stream
```

**Auth**: vault auth, role `browse-upload` or higher.
**Capability**: `vault_chunk_v1`.

Body: raw ciphertext bytes (size limits per §10 / `vault-v1-formats.md`).

Success: **201 Created** (new chunk) or **200 OK** (chunk already stored byte-identically).

```json
{
  "ok": true,
  "data": {
    "chunk_id": "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
    "size": 2097152,
    "stored": true
  }
}
```

Idempotency:

- Same `chunk_id` + same ciphertext: 200 OK.
- Same `chunk_id` + different ciphertext: 422 `vault_chunk_tampered` or `vault_chunk_size_mismatch` with the mismatched hashes / sizes in `details`.

Errors:
- 400 `vault_invalid_request` — `chunk_id` fails `^ch_v1_[a-z2-7]{24}$`.
- 507 `vault_quota_exceeded` — write would exceed quota; per-chunk evaluation per H3.
- 503 `vault_storage_unavailable` — relay-side I/O issue.

### 6.9 Download chunk

```http
GET /api/vaults/{vault_id}/chunks/{chunk_id}
```

**Auth**: vault auth, any role.
**Capability**: `vault_chunk_v1`.

Response body: raw ciphertext, `Content-Type: application/octet-stream`, `Content-Length` set.

Errors:
- 404 `vault_chunk_missing` — referenced chunk not present (transient during another writer's upload window; client retries within budget then surfaces as permanent).
- 422 `vault_chunk_tampered` / `vault_chunk_size_mismatch` — server-side integrity check failed at fetch time.

### 6.10 Head chunk

```http
HEAD /api/vaults/{vault_id}/chunks/{chunk_id}
```

**Auth**: vault auth, any role.
**Capability**: `vault_chunk_v1`.

Returns headers only:

- `200 OK` + `Content-Length`, `X-Chunk-Hash: <hex>`, `X-Chunk-Stored-At: <RFC3339>`.
- `404 Not Found` if the chunk is not stored. No response body.

Used to skip already-uploaded chunks before issuing PUT.

### 6.11 Batch chunk head

```http
POST /api/vaults/{vault_id}/chunks/batch-head
```

**Auth**: vault auth, any role.
**Capability**: `vault_chunk_v1`.

Request:

```json
{
  "chunk_ids": [
    "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
    "ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb",
    "ch_v1_cccccccccccccccccccccccc"
  ]
}
```

Limits: max 1024 ids per request (§10). Larger sets are split client-side.

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "chunks": {
      "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa": { "present": true, "size": 2097152, "hash": "<hex>" },
      "ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb": { "present": false },
      "ch_v1_cccccccccccccccccccccccc": { "present": true, "size": 2097152, "hash": "<hex>" }
    }
  }
}
```

Used by upload (skip already-stored chunks), migration verify (§7.2), and resume after a crash (§T6.5).

Errors:
- 400 `vault_invalid_request` — any single id fails the regex; `details.field = "chunk_ids"`, `details.bad_id = "<offender>"`.

### 6.12 GC plan

```http
POST /api/vaults/{vault_id}/gc/plan
```

**Auth**: vault auth, role `sync` or higher.
**Capability**: `vault_gc_v1`.

Request:

```json
{
  "manifest_revision": 60,
  "encrypted_gc_auth": "<base64 — vault-derived authorization material>",
  "candidate_chunk_ids": [
    "ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
    "ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb"
  ]
}
```

The client provides candidate chunks it has determined are no longer referenced (by walking decrypted manifests + retention policy). The server cross-checks against every manifest revision it currently retains: a chunk is safe to delete only if no retained revision references it.

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "plan_id": "<random>",
    "safe_to_delete": ["ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa"],
    "still_referenced": ["ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb"],
    "expires_at": "2026-05-02T10:15:00Z"
  }
}
```

`plan_id` + `expires_at` (15 minutes default) bind the plan to a subsequent execute. Plans not executed within their TTL are silently dropped server-side; the client must re-plan.

Triggers per §A16: sync-driven (every manifest fetch), eviction-driven (D2 step 1), manual (Maintenance → "Optimize storage now"). All three call the same endpoint.

Errors:
- 403 `vault_access_denied` — caller's role < `sync`.
- 422 `vault_manifest_tampered` — `manifest_revision` not in the chain.

### 6.13 GC execute

```http
POST /api/vaults/{vault_id}/gc/execute
```

**Auth**: vault auth, role `sync` for sync-driven GC; role `admin` when `purge_secret` is supplied (T14 hard-purge).
**Capability**: `vault_gc_v1` (and `vault_purge_v1` when `purge_secret` is supplied).

Request:

```json
{
  "plan_id": "<from /gc/plan>",
  "purge_secret": "<base32 — admin-only, required for hard-purge per T14>"
}
```

`purge_secret` is a separate high-entropy secret stored in the recovery kit (per plan file 09 + §T14.4). It is **only** required when the planned chunks include any whose tombstones have not yet expired — i.e. T14 admin hard-purge. Sync-driven expiry GC works without it.

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "plan_id": "<id>",
    "deleted_count": 142,
    "skipped_count": 0,
    "freed_ciphertext_bytes": 297795584
  }
}
```

Already-purged chunks count as `skipped_count`. Idempotent — re-execution returns the same totals.

After execute returns, downloading any deleted chunk returns 404 `vault_chunk_missing` and `used_ciphertext_bytes` decreases by `freed_ciphertext_bytes`.

Errors:
- 403 `vault_purge_not_allowed` — `purge_secret` required by the plan and caller is not admin (or `purge_secret` invalid).
- 404 `vault_not_found` — plan expired or unknown `plan_id`.

### 6.14 GC cancel

```http
POST /api/vaults/{vault_id}/gc/cancel
```

**Auth**: vault auth, role `sync` (for sync GC plans) or `admin` (for scheduled hard-purge jobs).
**Capability**: `vault_gc_v1`.

Request:

```json
{
  "plan_id": "<id, optional>",
  "job_id": "<id, optional>"
}
```

At least one of `plan_id` (in-flight plan) or `job_id` (scheduled hard-purge per T14) must be supplied. Both can be supplied together — the server cancels whichever match.

Success: **204 No Content**.

Idempotent: re-cancelling an already-cancelled / already-elapsed job returns 204. Used by the §A17 "toggle Vault OFF" path to clear pending purges.

---

## 7. T9 — Migration endpoints (H2 state machine)

The vault migration flow moves a vault from one relay to another **without re-keying**. The source relay publishes that the vault has moved; other devices learn on their next header fetch and switch transparently. Full state machine: T0 §H2.

```text
idle → started → copying → verified → committed → idle (on new relay)
         ↑                                ↓
         └──────────  rollback ───────────┘ (only from started/copying/verified)
```

Chunks copy via the standard §6.8 PUT on the **target** relay. There is no migration-specific upload endpoint — the same chunk store handles both first-write and migration-write.

All endpoints in §7 require vault auth on the **source** relay, role `admin`, and capability `vault_migration_v1`.

### 7.1 Start migration

```http
POST /api/vaults/{vault_id}/migration/start
```

Request:

```json
{
  "target_relay_url": "https://new.example.com"
}
```

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "migration_token": "<opaque>",
    "started_at": "2026-05-02T10:00:00Z",
    "state": "started"
  }
}
```

Idempotent: calling again with the same `target_relay_url` returns the same `migration_token`. Calling with a different `target_relay_url` while a migration is in progress returns 409 `vault_migration_in_progress` with `details.{state, target_relay_url}`.

### 7.2 Verify source

```http
GET /api/vaults/{vault_id}/migration/verify-source
```

Returns the source relay's view of the vault for client-side comparison against the target.

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "manifest_revision": 60,
    "manifest_hash": "<hex>",
    "chunk_count": 12483,
    "ciphertext_byte_total": 8589934592,
    "header_hash": "<hex>"
  }
}
```

Client compares against the target relay's `GET /api/vaults/{id}/manifest` + `POST /chunks/batch-head`. Mismatches surface client-side as `vault_migration_verify_failed` with `details.mismatch ∈ ["manifest_hash", "chunk_count", "byte_total", "chunk_sample"]`.

### 7.3 Commit migration

```http
PUT /api/vaults/{vault_id}/migration/commit
```

Request:

```json
{
  "migration_token": "<from /start>"
}
```

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "state": "committed",
    "committed_at": "2026-05-02T10:30:00Z",
    "migrated_to": "https://new.example.com"
  }
}
```

After commit:

- The source vault is **read-only**. Subsequent writes on the source return 409 `vault_migration_in_progress` with `details.state = "committed"`.
- `GET /header` on the source returns `migrated_to: <target_url>`. Other devices switch on next header fetch (§H2 multi-device propagation).
- Clients keep `previous_relay_url` locally for **7 days** (per §H2) for the "Switch back to previous relay" affordance. Source-side data retention beyond `committed_at` is operator policy; this protocol does not mandate a retention window on the source server.

Idempotent: re-commit returns the original `committed_at`. Calling commit before verify returns 409 `vault_migration_in_progress` with `details.state = "started"` or `"copying"`.

Errors:
- 409 `vault_migration_in_progress` — state ≠ `verified` (e.g., commit called before client-side verify completed).
- `vault_migration_verify_failed` — server-side last-second verify fails.

---

## 8. T13 — QR-assisted grants and access-secret rotation

Multi-device pairing onto an existing vault. An admin device generates a QR; the receiving device claims it; the admin approves with wrapped vault material. Capability `vault_grant_qr_v1`.

QR payload (out-of-band):

```text
vault://<relay_host>/<vault_id>/<join_request_id>/<ephemeral_pubkey_b64>?expires=<unix_ts>
```

Default expiry: **15 minutes** from creation. The `join_request_id` itself is the per-claim secret — anyone with the QR can claim, exactly once (the second claimer gets 409). Verification code (§8.3) defends against MITM-displayed QRs.

### 8.1 Create join request

```http
POST /api/vaults/{vault_id}/join-requests
```

**Auth**: vault auth, role `admin`.

Request:

```json
{
  "ephemeral_pubkey": "<base64 X25519>",
  "expires_at": "2026-05-02T10:15:00Z"
}
```

Success: **201 Created**

```json
{
  "ok": true,
  "data": {
    "join_request_id": "jr_<random>",
    "vault_id": "H9K7-M4Q2-Z8TD",
    "expires_at": "2026-05-02T10:15:00Z",
    "claim_url": "vault://relay.example.com/H9K7-M4Q2-Z8TD/jr_<random>/<ephemeral_pubkey>?expires=…"
  }
}
```

The admin device renders `claim_url` as a QR. Each call creates a new request; un-claimed requests can coexist up to the §10 limit.

The server **never** sees the admin's ephemeral private key. The verification code is computed locally on each side from the X25519 shared secret (admin priv × claimant pub == claimant priv × admin pub). The server only relays public material.

### 8.2 Get join request status

```http
GET /api/vaults/{vault_id}/join-requests/{join_request_id}
```

**Auth**: device auth always; **and** either (a) vault auth role `admin` (admin polling for claim) **or** (b) the requesting `device_id` matches `claimant_device_id` from a prior §8.3 claim (claimant polling for approval).

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "join_request_id": "jr_<random>",
    "state": "pending|claimed|approved|rejected|expired",
    "claimant_device_id": "<device_id, present if state ≥ claimed>",
    "claimant_pubkey": "<base64, present if state ≥ claimed>",
    "device_name": "<claimant-supplied, present if state ≥ claimed>",
    "approved_role": "sync|admin|browse-upload|read-only (present if state == approved)",
    "wrapped_vault_grant": "<base64 ciphertext, present if state == approved>",
    "expires_at": "<RFC3339>"
  }
}
```

The verification code is **not** in the response. Each side derives it locally from X25519 + SHA-256 of the public material both already hold (the admin has its own ephemeral privkey + the claimant pubkey from this response; the claimant has its own ephemeral privkey + the admin pubkey from the QR). Both should display the same 6-digit code; user confirms match before the admin approves. Derivation: see `vault-v1-formats.md` §"Device grant".

`wrapped_vault_grant` is the AEAD-encrypted vault unlock material the admin produced in §8.4, sealed to the claimant's pubkey. The claimant decrypts using its ephemeral private key after fetching this response.

### 8.3 Claim join request

```http
POST /api/vaults/{vault_id}/join-requests/{join_request_id}/claim
```

**Auth**: device auth only. The `join_request_id` (from the QR) is the per-claim authority.

Request:

```json
{
  "claimant_pubkey": "<base64 X25519>",
  "device_name": "Laptop"
}
```

`device_name` is a human-readable label the admin will see when approving (defends against device-spoofing — the admin compares this to the device they expected to scan the QR).

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "state": "claimed",
    "expires_at": "<RFC3339>"
  }
}
```

Idempotent: a repeat claim from the same `device_id` with the same `claimant_pubkey` returns the same outcome. A claim from a different device after the request is already `claimed` returns 409 `vault_invalid_request` with `details.field = "claimant"` — first-claimer wins.

Errors:
- 410 — join request expired (`expires_at` passed).
- 409 `vault_invalid_request` — already claimed by a different device.

### 8.4 Approve join request

```http
POST /api/vaults/{vault_id}/join-requests/{join_request_id}/approve
```

**Auth**: vault auth, role `admin`.

Request:

```json
{
  "approved_role": "sync",
  "wrapped_vault_grant": "<base64 ciphertext>"
}
```

`approved_role`: one of `read-only`, `browse-upload`, `sync`, `admin`. UI defaults to `sync` per §D11; the wire field has no default.

`wrapped_vault_grant` carries the vault's unlock material wrapped to `claimant_pubkey`, plus the claimant's `device_id` baked into AAD so it can't be replayed onto another device. Format: see `vault-v1-formats.md` §"Device grant".

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "state": "approved",
    "device_id": "<claimant_device_id>",
    "approved_role": "sync",
    "approved_at": "<RFC3339>"
  }
}
```

Idempotent: a repeat approve with the same wrapped material returns 200 with the original `approved_at`. Re-approve with a different `approved_role` or different wrapped material returns 409 `vault_invalid_request`.

Errors:
- 409 — request not in `claimed` state (still `pending`, already `approved`, or `rejected`).
- 410 — request expired.

### 8.5 Reject join request

```http
DELETE /api/vaults/{vault_id}/join-requests/{join_request_id}
```

**Auth**: vault auth, role `admin`.

Success: **204 No Content**.

Marks the request `rejected`; the claimant's next §8.2 poll returns `state: "rejected"`. Idempotent.

### 8.6 Revoke device grant

```http
DELETE /api/vaults/{vault_id}/device-grants/{device_id}
```

**Auth**: vault auth, role `admin`.

Success: **204 No Content**.

After revocation, the revoked device's next vault op returns 403 `vault_access_denied`. Local data on that device is **unaffected** — the relay has no remote-delete capability against client filesystems (T0 §gaps §22).

For compromised-device cases the admin UI offers a "Revoke and rotate" combo that calls this endpoint **and** §8.7 atomically. Revocation alone leaves cached creds usable until rotation.

Idempotent: re-revoke is 204.

### 8.7 Rotate access secret

```http
POST /api/vaults/{vault_id}/access-secret/rotate
```

**Auth**: vault auth (with **old** secret in `X-Vault-Authorization`), role `admin`.

Request:

```json
{
  "new_vault_access_token_hash": "<base64 hash(new_secret)>"
}
```

The server validates the old secret then **atomically** replaces `vault_access_token_hash` with the new value. Single active hash; no multi-hash grace window server-side (§A5). Clients distribute the new secret to surviving paired devices over QR or shared kit; the "7-day grace" is purely client-side UX.

Success: **200 OK**

```json
{
  "ok": true,
  "data": {
    "rotated_at": "<RFC3339>"
  }
}
```

After the response returns:

- Subsequent requests using the old secret return 401 `vault_auth_failed` with `details.kind = "vault"`.
- In-flight requests authenticated with the old secret finish; new requests use the new secret.

Errors:
- 401 `vault_auth_failed` — old secret didn't validate; `details.kind = "vault"`.
- 400 `vault_invalid_request` — `new_vault_access_token_hash` malformed.

Recovery passphrase rotation and Vault Master Key rotation are **out of scope for v1** (§A14): T13 ships access-secret rotation only.

---

## 9. Errors

Every error response uses the §4 envelope. The full `code` table — with HTTP status, retry class, and required `details` — lives in [T0 §"Error codes (vault_v1)"](../plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md#error-codes-vault_v1).

This document references codes inline by name. Treat the T0 table as authoritative for retry class. v2+ reserved codes (`vault_key_rotation_in_progress`, `vault_grant_expired`, `vault_grant_revoked`, `vault_folder_locked`, `vault_offline_pending`) are not emitted by v1 servers.

**Critical retry-loop semantics** (covered above but worth highlighting):

- `vault_manifest_conflict` carries the **current** ciphertext — clients never need to follow up with `GET /manifest` after a 409 (§A1).
- `vault_quota_exceeded` carries `eviction_available: bool` — drives whether the client's eviction pass (§D2) runs or whether the "vault full, sync stopped" terminal banner is surfaced (§A6).
- `vault_chunk_missing` is `auto`-retry up to the existing transfer retry budget; only after exhaustion does it become permanent.

---

## 10. Rate limits and abuse controls

Vault endpoints inherit the existing rate-limit middleware where applicable. Vault-specific limits below.

> **Defaults are draft and may be tuned during T1 implementation;** lock at the end of T1 review. The mechanism (per-device + per-vault, returns `vault_rate_limited` with `retry_after_ms`) is the contract.

| Limit | Default | Endpoint(s) | Surfaced as |
|---|---|---|---|
| Vault auth attempts (per device, per vault) | 10 / minute | All vault-authenticated endpoints | 429 `vault_rate_limited` with `Retry-After` + `details.retry_after_ms`. |
| Create vault (per device) | 5 / hour | `POST /api/vaults` | 429 `vault_rate_limited`. |
| Pending join requests (per vault) | 5 simultaneous | `POST /join-requests` | 409 `vault_invalid_request` with `details.field = "pending_count"`. |
| Chunks per `batch-head` request | 1024 | `POST /chunks/batch-head` | 400 `vault_invalid_request` with `details.field = "chunk_ids"`. |
| Max chunk ciphertext size | per `vault-v1-formats.md` | `PUT /chunks/{id}` | 413 `payload_too_large` (existing relay-wide code; not vault-specific). |
| Max manifest ciphertext size | 16 MiB | `PUT /manifest` | 413 `payload_too_large`. |
| Incomplete-upload TTL | 24 h | Chunks not referenced by any retained manifest revision | Server-side cleanup; clients never see it. |

`vault_rate_limited` always carries `details.retry_after_ms` — more precise than the HTTP `Retry-After` header. Per-vault and per-device limits compose multiplicatively.

---

## 11. References

- T0 decisions: [`desktop-connector-vault-T0-decisions.md`](../plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md) — authoritative spec; this document defers to it.
- Byte formats: [`vault-v1-formats.md`](vault-v1-formats.md) — AAD strings, HKDF labels, manifest envelope, chunk envelope, recovery envelope, export bundle CBOR records, device grant. *(Drafted in T0.3.)*
- Test vectors: `tests/protocol/vault-v1/` — JSON test cases exercised by both desktop Python and server PHP. *(Stubbed in T0.4 and populated in T2.)*
- Base protocol: [`protocol.md`](protocol.md) — device registration, pairing, transfers, fasttrack.
- Plan files: [`docs/plans/desktop-connector-vault-plan-md/`](../plans/desktop-connector-vault-plan-md/) — narrative architecture (01–11) and the T0 decision lock.
- Working tracker: [`VAULT-progress.md`](../plans/desktop-connector-vault-plan-md/VAULT-progress.md).
