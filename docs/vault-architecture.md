# Vault architecture (vault_v1)

The Desktop Connector Vault is an **account-less, end-to-end-encrypted
personal vault** on top of the same PHP relay that handles transfers
and fasttrack. This document is the canonical reference: every other
Vault doc (the wire-format spec, the byte-level formats spec, the
archived plan files, the ADR entries) exists to give a deeper view
into one slice of what's described here.

Read this first when you want to discuss Vault. Read the protocol
docs when you need wire-byte detail. Read the archived T0 lock when
you need the original rationale for a particular decision.

**Audience.** Engineers extending or debugging Vault. Assumes
familiarity with the rest of the Desktop Connector codebase
(transfers, fasttrack, the PHP relay layout).

**Source of truth for this doc.** The shipped code under
`desktop/src/vault/`, `server/src/Controllers/Vault*.php`,
`server/src/Repositories/Vault*.php`, `server/src/Crypto/VaultCrypto.php`,
plus the two protocol references in `docs/protocol/`. Where this
doc and those disagree, the code wins; please update this doc.

---

## §1. Overview & trust boundary

A **vault** is persistent encrypted storage bound to a relay,
accessed by one or more devices. Every vault has:

- A 12-character public **vault ID** rendered as three dashed groups
  of four (e.g. `H9K7-M4Q2-Z8TD`). Not a secret; the ID alone gives
  no access.
- A **vault access secret** (bearer-token-like) used as
  `X-Vault-Authorization: Bearer …` for vault API calls.
- A **master key** (256-bit) that is *never* sent to the relay.
  Every key the client actually uses (manifest key, chunk key,
  header key, chunk-ID HMAC key, recovery wrap key, device-grant
  wrap key, …) is HKDF-derived from this master key.

The master key is rebuilt on each unlock from one of:

1. A **recovery passphrase** + the file-resident recovery envelope
   (always available; required path for restoring on a fresh
   device).
2. A **device grant** stored in the system keyring or in an
   AEAD-encrypted fallback file (fast unlock on a device that has
   already onboarded).

The vault is **never sent to the server in plaintext form**.

### What the relay sees

The relay stores opaque ciphertext and bookkeeping:

- The encrypted **vault header** (small).
- A **root manifest envelope** (small — folder pointers, retention
  defaults, vault-wide op-log tail). The current revision is
  identified by a monotonically increasing integer. See
  ``docs/protocol/vault-v1-formats.md`` §10.A.
- One **folder shard envelope** per remote folder (potentially
  large — the actual file entries, version chains, tombstones,
  per-folder op-log tail). Each shard has its own per-folder
  ``shard_revision`` chain so an edit in one folder doesn't ship
  the others. The root carries each shard's
  ``shard_revision`` + ``shard_hash`` so a client can detect
  per-folder rollback at decrypt time (§10.C hash chain). See
  ``docs/protocol/vault-v1-formats.md`` §10.B.
- The **chunks**: AEAD-encrypted blobs keyed by
  content-addressed IDs (`ch_v1_<24 base32>`). Each chunk is
  immutable; updates produce new chunks with new IDs.
- Opaque bookkeeping for **GC plans**, **device-grant join
  requests** (the QR pairing channel for new devices), and
  **migration state** (for relay-to-relay moves).

The relay never sees plaintext filenames, folder names, file
contents, retention policies, op-log entries, or anything else
client-side meaningful. It enforces quota, per-root + per-shard
CAS, chunk-ID shape, and per-vault auth — that's the whole job.

**Manifest sharding** (landed 2026-05-17, replacing the
pre-shipping single-envelope design): the original v1 architecture
ship one AEAD-encrypted manifest per vault. As soon as a vault
held multiple folders, an edit in any folder shipped the whole
vault's manifest on every publish, scaling per-publish bandwidth
with vault size rather than edit size. The current shape splits
that envelope along the natural folder boundary: a small root
carries metadata + folder pointers, and per-folder shards hold
file entries. Most publishes (every file upload, version change,
or tombstone) touch one shard + atomically re-publish the root
via ``PUT /api/vaults/{id}/folders/{folder_id}/shard-with-root``;
the other folders' shards stay put. Folder set changes (add /
remove / rename) and vault-wide policy edits publish the root
alone. See ``docs/plans/vault-manifest-sharding.md`` for the
scoping doc and ``docs/architecture-decisions.md`` 2026-05-17
entry for the trade-offs.

### What stays client-side (encrypted)

Inside the manifest's AEAD body:

- The list of **remote folders** with encrypted display names.
- Each folder's file tree, version chain, tombstones, ignore
  patterns, retention policy.
- An **operation-log tail** capturing user actions (file uploaded,
  file deleted, device granted, eviction ran, …). Archived
  segments roll over once the tail exceeds 1000 entries (see §4).
- Per-device grants — i.e. the wrapped master-key blobs that
  let other paired devices unlock the vault. (Grants for *this*
  device are stored locally, not in the manifest.)

The threat model in §12 spells out what this trust boundary
actually protects against.

### Pointers

- Account-less framing and wire envelope ergonomics:
  `docs/protocol/vault-v1.md`.
- Byte-level format definitions: `docs/protocol/vault-v1-formats.md`.
- The original `CLAUDE.md` summary: the "## Vault (vault_v1)"
  block.

---

## §2. Product model

Vault has five distinct entities. Mixing them up is the most common
mistake when discussing the feature.

### Vault

A vault is the top-level container — one cryptographic identity, one
quota, one manifest chain, one set of authorised devices. A device
can in principle have access to more than one vault, but v1 ships
with a single-vault-per-device assumption (`vault.active` toggle is
a single switch). Import-into-existing is a *merge*, not a swap; the
import wizard refuses to overwrite a vault that has a different
genesis fingerprint (see §3).

### Remote folder

A **remote folder** is a top-level logical folder inside a vault,
shared across all devices that hold a grant. Identified by an
immutable `rf_v1_<24 lowercase base32>` ID (regex
`^rf_v1_[a-z2-7]{24}$`, generated client-side per
`desktop/src/vault/manifest.py`). Rename changes the encrypted
`display_name_enc` only — the ID is invariant. Each remote folder
carries its own `retention_policy` (default `keep_deleted_days: 30`,
`keep_versions: null` = unbounded) and `ignore_patterns` (default
list in §9). Owned by the vault, not by any one device.

### Binding

A **binding** is a *per-device* mapping from a remote folder to a
local filesystem path. Bindings are device-local — they live in the
device's local index, not in the manifest. Two devices can bind the
same remote folder to different local paths, and renaming the
remote folder doesn't change either local path.

A binding has a **sync direction** (see §9 vocabulary) and a
**state**:

- `unbound` — remote folder visible in the browser, no local path
  attached. Browse-only.
- `needs_preflight` — user has picked a path; the preflight scan
  hasn't run or hasn't been confirmed. No sync traffic.
- `bound` — preflight confirmed; sync runs per the chosen
  direction.
- `paused` — bound but no traffic (manual pause, ransomware
  trip, quota stop).
- `error` — terminal sync error; user intervention required.

A device with no binding for a folder is implicitly in **Browse
only** mode for that folder: it can list, download, upload via the
remote browser, but no automatic filesystem syncing happens.

### Device

A **device** is an instance accessing the vault: a desktop, an
Android phone (post-v1), a freshly-restored backup. Devices are
identified by the standard Desktop Connector device identity; the
vault layer sits *above* device auth. Vault grants are issued
per-device.

### Grant

A **grant** is a per-device unlock token: the master key (or a
device-bound unlock key) encrypted for that device. Stored locally
(system keyring preferred, AEAD-encrypted file fallback). The
manifest records *which* devices have a grant and *which role* each
holds; the actual wrapped key material lives on the device itself.

Four roles (canonical hyphen-lowercase per A9):

- `read-only` — browse, download, download previous versions.
- `browse-upload` — `read-only` + upload, soft-delete.
- `sync` — `browse-upload` + run binding sync, trigger eviction.
- `admin` — `sync` + hard-purge, grant other devices, rotate
  access secret.

Revoking a grant takes effect for *future* operations on the relay
(the device's bearer token is invalidated); it does not erase the
master key that device already possesses. The wording §14 makes this
explicit to users.

### Pointers

- Folder cache schema and binding-state transitions:
  `desktop/src/vault/state/` (and the cache table
  `vault_remote_folders_cache` documented in T0 §D6, archived).
- Role enforcement: `server/src/Auth/VaultAuthService.php` and
  the role helpers in `desktop/src/vault/grant/`.

---

## §3. Identity & crypto

The cryptographic surface is small on purpose. Three primitives, a
KDF, and a handful of subkey labels.

### Primitives

- **AEAD:** XChaCha20-Poly1305 (24-byte nonce + 16-byte tag). Used
  for every encrypted blob (manifest, chunk, header, recovery
  envelope, device grant, export bundle records). The 24-byte
  nonce removes the "random nonce" birthday-bound concern.
- **KDF (passphrase → key):** Argon2id with v1-locked params
  `m = 128 MiB, t = 4, p = 1`, output 32 bytes. libsodium fixes
  `p = 1`; the spec locks `p = 1` to match
  (`desktop/src/vault/crypto.py:78` defines
  `ARGON2ID_PARALLELISM = 1`).
- **Subkey derivation (key → key):** HKDF-SHA256 (RFC 5869). Salt
  is 32 zero bytes by convention; the *label* (HKDF `info`)
  carries the context.

### HKDF labels

All HKDF labels live under the `dc-vault-v1/` namespace. The shipped
set is:

- `dc-vault-v1/chunk-id` — HMAC key for content-addressed chunk
  IDs.
- `dc-vault-v1/chunk-nonce` — derivation key for per-chunk AEAD
  nonces.
- `dc-vault-v1/content-fingerprint` — MAC key for chunk dedup /
  verification.
- `dc-vault-v1/recovery-wrap` — wrapping key for the recovery
  envelope.
- `dc-vault-v1/device-grant-wrap` — wrapping key for per-device
  grants.

The byte-exact construction of each derived key — including the
manifest, chunk, and header content keys — is in
`docs/protocol/vault-v1-formats.md`. Don't re-derive the labels
from this document; cite the formats spec.

### AAD construction

Every AEAD envelope binds its plaintext to its context via the
AAD (additional authenticated data). The three schemas
(verbatim from `desktop/src/vault/crypto.py:322,427,588`):

- `dc-vault-manifest-v1` (manifest)
- `dc-vault-chunk-v1` (chunk)
- `dc-vault-header-v1` (header)

Each AAD is `schema-tag || ` followed by the context fields the
formats spec pins (vault ID, revision, parent revision, author
device ID for manifests; vault ID, remote folder ID, file ID, file
version ID, chunk index, plaintext size for chunks; …). Cross-
context replay is therefore impossible — a chunk's ciphertext
cannot be served as a manifest or vice versa even if the relay
tries.

### Recovery envelope

The recovery envelope is the **only path to a vault on a fresh
device**. It bundles a high-entropy `recovery_secret` (generated at
vault creation) plus a passphrase-derived wrap key:

```
argon_output = argon2id(passphrase_NFC, argon_salt, m=128MiB, t=4, p=1, 32)
wrap_key     = HKDF-SHA256(salt=argon_output, ikm=recovery_secret,
                           info="dc-vault-v1/recovery-wrap", L=32)
```

The wrap key AEAD-encrypts the master key. The envelope is written
to disk as a single file (`<vault-id>.dc-vault-recovery`); QR is an
optional rendering and was not in v1 scope as a *primary* delivery
channel.

The onboarding wizard performs a **mandatory recovery test** by
default (re-derives the wrap key end-to-end and AEAD-verifies the
master-key payload). Skip is offered but not recommended; recovery
status (Untested / Ready / Stale / Failed / Missing) is surfaced
in Vault Settings → Recovery and as a persistent banner when the
status is anything but Ready.

### Device grants

A device grant is the same construction with a different wrap-key
source: `dc-vault-v1/device-grant-wrap` is derived from the
device's shared-secret material (system keyring entry, AEAD-fallback
file keyed off the device-local crypto key from
`desktop/src/crypto.py`). The wrapped master key is the payload;
the role lives alongside it in the manifest's encrypted grants
table.

A device opening the vault for the first time presents a recovery
passphrase; subsequent unlocks pull the grant from the keyring (no
passphrase entry needed) until the **unlock timeout** expires
(default 15 min idle, on screen-lock, on quit; configurable per
§13). Sensitive operations (clear vault, hard purge, rotate access
secret, revoke device, rotate recovery) always require fresh unlock
regardless of timeout.

### Genesis fingerprint

`HMAC(genesis_vault_secret, "dc-vault-v1/genesis-fingerprint")[0..16]`
is recorded in the header. It uniquely identifies a vault's
crypto-identity (independent of the public vault ID) and lets the
import wizard refuse a merge between two unrelated vaults that
happen to share an ID. Mismatch surfaces as
`vault_identity_mismatch` (see §5).

### Wrong-passphrase rate limit

There is **no explicit retry counter**. Protection is intrinsic:
each verify attempt costs ~1–10 s of Argon2id-bound CPU/RAM. A
generated 7-word passphrase has ~84 bits of entropy; offline
brute-force is infeasible at these params, and online attempts are
naturally bounded by physical access + wall-clock cost. ADR entry
`docs/architecture-decisions.md#2026-05-12`.

### Pointers

- All crypto helpers: `desktop/src/vault/crypto.py` (desktop) and
  `server/src/Crypto/VaultCrypto.php` (PHP mirror).
- Cross-runtime test vectors: `tests/protocol/vault-v1/*.json`.
- Byte-exact format: `docs/protocol/vault-v1-formats.md`.

---

## §4. Storage model

### Manifest envelope

The manifest is a single AEAD-encrypted blob carrying the whole
client-meaningful vault state (folders, files, versions, tombstones,
op-log tail, grants table). Its wire shape on the relay:

```
[manifest_format_version : 1 byte = 0x01]
[nonce : 24 bytes]
[AEAD ciphertext : variable]
[Poly1305 tag : 16 bytes]
```

`manifest_format_version` is **plaintext** (not part of AAD). The
relay reads it to gate v2-bumped envelopes before any AEAD attempt:
a future v2 client posting a `0x02` envelope to a v1 server gets
a `vault_format_version_unsupported` 422 instead of an opaque AEAD
failure.

Inside the ciphertext is JSON-like plaintext containing:

- `schema: "dc-vault-manifest-v1"`, `vault_id`, `revision`,
  `parent_revision`, `created_at`, `author_device_id`.
- `remote_folders[]` — encrypted display names + ignore patterns
  + retention policy per folder.
- The per-folder file trees, version chains, tombstones.
- `operation_log_tail[]` — see below.
- `archived_op_segments[]` — pointers to overflowed segments.
- Grants table — wrapped master-key blobs per authorised device.

The **version chain** is enforced by manifest CAS on the relay: a
PUT carries `expected_current_revision`; on mismatch the server
returns 409 with the *full current ciphertext + hash + revision*
(A1) so the client can merge without an extra round-trip. See §6.

### Op-log tail + archived segments

The `operation_log_tail` is capped at **1000 entries**. When that
cap is reached during a CAS publish, the oldest **500** entries are
sealed into an immutable segment encrypted with the subkey
`dc-vault-v1/op-log-segment/<segment_id>` and stored in
`vault_op_log_segments` on the relay. The newest 500 remain in the
manifest tail. The manifest records the archived segments in
`archived_op_segments` (newest-first) so any device can resurrect
the full history if needed. Rollover inherits the CAS guarantee
because it happens *during* a manifest publish.

This split is per **D14**: it keeps the manifest blob bounded
without losing audit history.

### Chunk envelope

```
[nonce : 24 bytes]
[AEAD ciphertext : variable]
[Poly1305 tag : 16 bytes]
```

The chunk ID is content-addressed — derived from chunk content
plus the file/version context via the chunk-ID HMAC subkey. Server
and client both enforce the regex
`^ch_v1_[a-z2-7]{24}$` (server: `VaultChunksRepository::CHUNK_ID_REGEX`;
desktop: `desktop/src/vault/crypto.py`). 24 base32 chars = 120 bits
of identifier; the prefix-2 sharding (1024 buckets) maps directly
to the storage layout below.

`CHUNK_SIZE = 2 MiB` (`desktop/src/vault/vault.py:VAULT_CHUNK_SIZE
= 2 * 1024 * 1024`). Last chunk is allowed to be smaller. Chunks
are immutable once written; a "modified" file is just a new
version pointing at new chunk IDs.

Dedup is local to a vault: the same plaintext encrypted under the
same chunk key produces the same chunk ID, so re-uploads short-
circuit via `POST /api/vaults/{id}/chunks/batch-head`. Cross-vault
dedup is impossible by construction (different master keys → different
chunk-ID HMAC keys).

### Header envelope

A small AEAD blob containing the vault's identity-level state:
genesis fingerprint, recovery envelope metadata (so the recovery
test knows which Argon2id params were used at create-time even if
defaults change later), and operational flags (`migrated_to` after a
relay move, `quota_ciphertext_bytes`, `used_ciphertext_bytes`).

`GET /api/vaults/{id}/header` returns the encrypted blob *plus*
the plaintext quota + used bytes so the desktop can compute
80/90/100% pressure bands without a separate query (see §8).

### Server tables

`server/migrations/002_vault.sql` creates eight tables:

- `vaults` — one row per vault: ID, header ciphertext + hash,
  quota, used bytes, current manifest revision, optional
  `migrated_to` URL, access-token hash.
- `vault_manifests` — manifest history by revision. CAS guards
  ordering.
- `vault_chunks` — chunk ciphertext metadata + state (active /
  retained / gc_pending / purged).
- `vault_chunk_uploads` — in-flight upload bookkeeping.
- `vault_join_requests` — QR-assisted device joins (T13).
- `vault_audit_events` — relay-side audit trail.
- `vault_gc_jobs` — GC plan tracking + delayed-purge schedule.
- `vault_op_log_segments` — sealed op-log overflow (D14).

Chunks live on disk at
`server/storage/vaults/<vault_id>/<chunk_id_prefix>/<chunk_id>`
(per **D13**), strictly isolated from the transfer storage tree.
Legacy `DELETE /api/transfers` paths cannot reach vault chunks.

### Pointers

- Schema: `server/migrations/002_vault.sql` plus the per-feature
  migrations that follow.
- Storage layout: `server/src/Repositories/VaultChunksRepository.php`.

---

## §5. Wire protocol — summary

The full endpoint list, request/response shapes, error envelopes,
and idempotency semantics live in `docs/protocol/vault-v1.md` (over
1000 lines). This section is a conceptual map.

### Endpoint groups

- **Create + header.** `POST /api/vaults`, `GET/PUT
  /api/vaults/{id}/header` (CAS).
- **Manifest CAS.** `GET /api/vaults/{id}/manifest`,
  `PUT /api/vaults/{id}/manifest` (carries
  `expected_current_revision`; A1 conflict shape on 409).
- **Chunks.** `PUT/GET/HEAD /api/vaults/{id}/chunks/{chunk_id}`
  + `POST /api/vaults/{id}/chunks/batch-head` for dedup.
- **GC.** `POST /api/vaults/{id}/gc/plan` (propose) →
  `POST /api/vaults/{id}/gc/execute` (commit) →
  `POST /api/vaults/{id}/gc/cancel`.
- **Grants.** `POST /api/vaults/{id}/grants/join-request`
  (sender), `GET /api/vaults/{id}/grants/join-request/{req_id}`
  (poll), `POST /api/vaults/{id}/grants/claim` (joiner),
  `DELETE /api/vaults/{id}/grants/{grant_id}` (revoke).
  Join requests TTL 15 min.
- **Migration (H2).** See §11.

### Capability bits

Advertised on `GET /api/health.capabilities`:

- `vault_v1` — aggregate; only flips on when **all** T1
  mandatory bits below are present.
- `vault_create_v1`, `vault_header_v1`, `vault_root_cas_v1`,
  `vault_shard_cas_v1`,
  `vault_chunk_v1`, `vault_gc_v1` — the T1 mandatory set.
- `vault_soft_delete_v1` — server understands tombstone
  retention.
- `vault_export_v1` — relay can handle the large continuous
  reads/writes of export.
- `vault_migration_v1` — H2 endpoints present.
- `vault_grant_qr_v1` — QR-assisted grant flow.
- `vault_purge_v1` — delayed hard-purge job tracking.

Clients gate features on these bits; a transfer-only relay
advertises none and stays compatible with transfer + fasttrack
operations.

### Auth composition

Vault endpoints require **device auth** (`Authorization: Bearer
<device-token>`) **and** **vault auth**
(`X-Vault-Authorization: Bearer <vault-secret>`). The vault
middleware `VaultAuthService::requireVaultAuth` composes with the
existing `requireAuth` device middleware. Missing or wrong vault
header → 401 `vault_auth_failed` with `details.kind = "vault"` (so
the client can distinguish from a device-token expiry).

### Error envelope

```json
{ "ok": false,
  "error": { "code": "vault_…", "message": "…",
             "details": { … } } }
```

Error code groups (full list in the T0 lock):

- **Auth & access:** `vault_auth_failed`, `vault_access_denied`,
  `vault_not_found`, `vault_already_exists`.
- **Manifest & integrity:** `vault_manifest_conflict` (A1
  conflict payload), `vault_manifest_tampered`,
  `vault_header_tampered`, `vault_format_version_unsupported`.
- **Chunks:** `vault_chunk_missing`, `vault_chunk_tampered`,
  `vault_chunk_size_mismatch`.
- **Quota & storage:** `vault_quota_exceeded` (507),
  `vault_local_disk_full`, `vault_storage_unavailable`.
- **Import / export / migration:** `vault_export_tampered`,
  `vault_export_passphrase_invalid`, `vault_identity_mismatch`,
  `vault_import_requires_merge`, `vault_import_failed`,
  `vault_migration_in_progress`, `vault_migration_verify_failed`.
- **Recovery:** `vault_recovery_failed`,
  `vault_recovery_not_configured`.
- **Capability / protocol:** `vault_protocol_unsupported`,
  `vault_client_too_old`, `vault_server_too_old`.
- **Sync (client-local):** `vault_sync_paused_suspicious_change`,
  `vault_sync_paused_quota_drained`, `vault_unlock_required`.
- **Rate / generic:** `vault_rate_limited`,
  `vault_invalid_request`, `vault_internal_error`.

### Pointers

- Wire spec: `docs/protocol/vault-v1.md`.
- Server routing: `server/src/Router.php`,
  `server/src/Controllers/VaultController.php`,
  `server/src/Controllers/VaultGrantsController.php`.

---

## §6. CAS merge

When two devices race a manifest publish, the loser receives a 409
with the full current manifest ciphertext + hash + revision (A1). It
decrypts, diffs against its own parent_revision, applies per-
operation merge rules, builds a new manifest at
`parent_revision = K, revision = K + 1`, and retries CAS.

### Nine auto-mergeable ops (D4)

The desktop merges these deterministically without prompting:

1. New file at path P (different paths) → both added.
2. Soft-delete of P → tombstone wins; other-side upload of the
   same path becomes a new version on the live entry once the
   tombstone is restored (if it ever is).
3. New version of P → both appended to `versions[]`, ordered by
   `(timestamp, device_id_hash)` lex tie-break.
4. Rename of a directory subtree → all contained paths follow.
5. Soft-delete of a directory subtree → cascade tombstones.
6. Restore previous version of P → becomes new live version.
7. File metadata / permissions update → latest `(modified_at,
   device_id_hash)` wins.
8. Folder tag / label dictionary updates → merge.
9. Retention-policy update → server-head wins on tie.

Tie-breaker hash: `SHA-256(author_device_id)` big-endian. The
chosen rule for each op is fixed; non-determinism is forbidden so
two devices reaching 409 simultaneously will compute the same
merged manifest.

### The one manual case

**Hard-purge collision** is the single op that surfaces "manual" to
the user. If two devices both initiate a hard-purge that touches
overlapping chunks, the merge can't auto-resolve which side's
authorization wins. The client surfaces a clear UI banner ("Storage
full, manual cleanup required") and the user re-runs the purge
flow (full typed-confirm + fresh unlock).

### Pointers

- Desktop merge implementation:
  `desktop/src/vault/manifest.py::merge_with_remote_head` (and the
  per-op helpers it calls).
- Test vectors: `tests/protocol/vault-v1/manifest_v1.json`.

---

## §7. Versions, tombstones, retention

### D10 vocabulary

Lock this terminology — drift here causes user-facing bugs.

- **Version** — an immutable entry in `file.versions[]` with
  `version_id`, chunk list, plaintext-size, content fingerprint,
  `author_device_id`, `modified_at`.
- **Add version** — append to `versions[]`, update
  `latest_version_id`. The previous version remains in history.
- **Rename** — change the path key in the parent folder. Does
  *not* create a version.
- **Restore** — append a version that points at a historical
  version's chunks, then update `latest_version_id` so the
  restored chunks are now the live ones.

### Tombstones (D5)

A deleted file becomes a tombstone with:

- `deleted_at` — client-provided RFC 3339 ms-precision. Informational
  only (A8).
- `recoverable_until = deleted_at + keep_deleted_days * 86400`,
  computed on the **server's** clock when the deletion is
  CAS-published — not on whoever wrote `deleted_at`. This blocks
  the "back-date a deletion to expedite GC" attack from a
  compromised paired device.
- `deleted_by_device_id`.

The default `keep_deleted_days = 30` lives in
`desktop/src/vault/manifest.py:DEFAULT_RETENTION_POLICY`. The
policy is per-folder and immutable after folder creation in v1.

### §22 local-effects vocabulary

Don't reuse these terms ambiguously in UI strings:

- **Disconnect** — stop sync. Local files unchanged, remote files
  unchanged. Reversible.
- **Delete** — soft-delete (tombstone). Recoverable until
  retention expires.
- **Clear** — bulk soft-delete (folder or whole vault).
  Recoverable. Local bound folders unaffected by the *remote*
  clear; the next sync pass propagates the tombstones if the
  binding is two-way.
- **Purge** — permanent, irreversible, *only* affects the relay.
  Default 24 h scheduling grace; cancellable until the deadline
  fires.

### Pointers

- Manifest entry shape and tombstone helpers:
  `desktop/src/vault/manifest.py`.
- Retention defaults and per-folder overrides: same file.

---

## §8. Quota & eviction

### Quota defaults

`quota_ciphertext_bytes` defaults to **1 GB** (1073741824) per
vault — set in `server/migrations/002_vault.sql:33`. The relay
charges every chunk, manifest, and header byte against the quota;
deletions don't recover space until GC purges the chunks.

The header `GET` response includes `quota_ciphertext_bytes` and
`used_ciphertext_bytes` so clients compute the pressure band
locally:

- **< 80 %** — silent.
- **≥ 80 %** — warning bar in Vault Settings.
- **≥ 90 %** — warning + update-notice slot
  (`_NEAR_QUOTA_THRESHOLD = 0.9` in
  `desktop/src/vault/state/usage.py`).
- **100 %** — escalated warning, auto-eviction offered.

### Eviction order (D2 — strict)

When a 507 fires or sync-driven GC kicks in (§A16 three triggers:
opportunistic, eviction-driven, manual), the desktop walks four
stages **in order**, never skipping ahead:

1. Hard-purge **expired tombstones** (their
   `recoverable_until < now`).
2. Hard-purge **unexpired tombstones**, oldest `deleted_at`
   first.
3. Hard-purge oldest **historical versions** of multi-version live
   files (preserve current + one backup).
4. **No more candidates** → stop sync, surface the §D2 step-4
   "make space" banner.

Each stage emits a deterministic `EvictionStageResult` from
`desktop/src/vault/ops/eviction.py` and produces an
`vault.eviction.<event>` activity-log entry. Stages 2 and 3 are
guarded behind the `sync` or `admin` role.

### Per-folder vs whole-vault usage (A21)

- **Whole-vault `used_ciphertext_bytes`** — server-authoritative
  global unique-chunk sum.
- **Per-folder usage** — descriptive only, client-computed from
  the decrypted manifest by summing chunk sizes referenced by
  each folder's current and retained entries. If two folders
  share a chunk (rare in v1 — cross-folder dedup is *not* the
  design goal, but it can happen for byte-identical files), the
  chunk's size appears in **both** folder rows but only once in
  the whole-vault total.

The Folders tab's *Current / Stored / History* columns are all
client-computed and may legitimately add up to more than the
server total.

### Pointers

- Eviction implementation: `desktop/src/vault/ops/eviction.py`.
- Usage calculation: `desktop/src/vault/state/usage.py` and the
  helpers in `desktop/src/vault/folder/`.

---

## §9. Sync engine

### Sync-mode vocabulary (§20 — locked)

- **Browse only** — no binding. Remote folder visible in the
  browser; no automatic sync.
- **Backup only** — local → remote one-way. Default for new
  bindings. Local deletions become remote tombstones; remote
  changes are ignored.
- **Two-way** — both directions. Conflicts use the §A20 naming
  convention.
- **Download only** — remote → local one-way. Useful for restore.
- **Paused** — binding exists, no traffic.

### Binding-state machine (A12)

States: `unbound`, `needs_preflight`, `bound`, `paused`, `error`.
A binding goes `unbound → needs_preflight → bound` only after the
user confirms the preflight diff. Preflight (D15) shows the
tombstone count and `recoverable_until` dates for everything that
would be applied on the first sync, *but* the initial baseline
treats existing remote tombstones as already-applied — they do
**not** delete local files before the baseline is laid down. This
is the primary defence against accidental destruction on a fresh
binding.

### Ransomware detector (§6 defaults)

Defaults: **200 file changes within 5 minutes** OR **≥ 50 % rename
ratio** in a single batch. Either trip pauses the binding
immediately (no pre-pause prompt per A15), surfaces the
`vault_sync_paused_suspicious_change` error, and requires user
review before resuming. Configurable per folder. Disabling the
detector requires confirmation.

### Default ignore patterns (§7)

Per-folder, gitignore-syntax, stored encrypted in the manifest.
Defaults include `.git/`, `.svn/`, `.hg/`, `node_modules/`,
`vendor/`, `target/`, `build/`, `dist/`, `.gradle/`, `.idea/`,
`.vscode/`, `__pycache__/`, `*.pyc`, `.mypy_cache/`,
`.pytest_cache/`, `.DS_Store`, `Thumbs.db`, `desktop.ini`,
`.Trash-*/`, `*.tmp`, `*.temp`, `*.swp`, `~$*`.

Plus a **2 GB per-file cap** (configurable). Skipped files log
`vault.sync.file_skipped_too_large`.

### Special files (§8) and case sensitivity (§9)

- **Symlinks** are skipped by default. A target-as-metadata model
  is post-v1.
- **FIFOs, sockets, device files** — always skipped, logged as
  `vault.sync.special_file_skipped`.
- **Hardlinks** — stored once, restored as independent files.
- **Remote** is always **case-sensitive**. On case-insensitive
  locals the engine detects collisions and offers (Keep one,
  Skip both, Pick one for me) rather than silent-merging. Default
  is never silent.

### Conflict naming (§A20)

`<stem> (conflict <kind> [<device>] <YYYY-MM-DD HH-MM>)<ext>`.

`<kind>` is one of `uploaded`, `imported`, `synced`, `restored`.
Device is omitted on bundle imports (no device context). The
exhaust path falls back to a random 4-byte hex token after 20
candidates fail — see `desktop/src/vault/conflict_naming.py` and
`tests/protocol/test_desktop_vault_conflict_naming.py`.

### Resume state

Per-binding sync state lives in the local SQLite index
(`vault-local-index.sqlite3`) under tables `vault_bindings`,
`vault_local_entries`, `vault_pending_operations`. Upload sessions
spill to `~/.cache/desktop-connector/vault/uploads/<session_id>.json`
so a process restart batch-HEADs already-uploaded chunks rather than
re-uploading.

### Pointers

- Binding + sync code: `desktop/src/vault/binding/`.
- Filesystem watcher: `desktop/src/vault/binding/watcher.py`.
- Ransomware detector:
  `desktop/src/vault/binding/ransomware_detector.py`.
- Two-way merge: `desktop/src/vault/binding/twoway.py`.

---

## §10. Export / import bundles

### Format (A10)

An export bundle is a single file written atomically (write +
fsync + rename, checkpointed for resume). On-disk shape:

```
[outer envelope header : Argon2id params + nonce + wrapped file key]
[CBOR record stream]
[footer record : hash chain + record count]
```

Inner records are CBOR-framed `[type, length, payload]` tuples,
each AEAD-encrypted under the file key:

- `RECORD_TYPE_HEADER` (1) — vault ID, timestamp, manifest
  revision.
- `RECORD_TYPE_BUNDLE_INDEX` (2) — summary of contained chunks +
  manifests.
- `RECORD_TYPE_MANIFEST` (3) — encrypted manifest envelope
  verbatim from the relay.
- `RECORD_TYPE_OP_LOG_SEGMENT` (4) — archived op-log entries.
- `RECORD_TYPE_CHUNK` (5) — encrypted chunk envelope.
- `RECORD_TYPE_FOOTER` (6) — SHA-256 chain hash + record count.

The outer envelope is keyed off the **export passphrase** —
deliberately separate from the recovery passphrase per **D8**.
Reusing them is allowed but the wizard nudges users toward
separation. Argon2id params are the same v1 lock.

### Verification pass

After writing, the export wizard re-opens the bundle, walks
records recomputing the SHA-256 chain over `length || nonce ||
ciphertext`, verifies the footer, and sample-decrypts a random
subset of chunks. Failure surfaces as `vault_export_tampered`
*before* any reminder UX claims the export succeeded.

### Import merge (D9)

Importing always merges into an existing vault on the same relay
if there is one. Three modes per remote folder:

- **Overwrite** — incoming wins, existing version moves to
  history. Dangerous; explicit confirmation required.
- **Skip** — existing wins, incoming goes to history.
- **Rename** — incoming lands at a conflict-named path.
  **Default.** Uses §A20 with `kind = imported` and no device
  segment.

### Per-folder conflict batches (A4)

The wizard shows one dialog per remote folder, listing the
conflicting paths in that folder. The first dialog has an "Apply
to remaining folders" checkbox so the user can decide once for
the whole bundle when the choice is obvious.

### Import preview (§17)

Eight fields, in order:

1. Vault fingerprint (first 12 chars highlighted, classified
   *matches* / *different* / *no active*).
2. Source (relay URL or filename).
3. Size (logical + ciphertext).
4. Remote folders (count + top-10 list + "…+N more").
5. History (current / versions / tombstones).
6. Conflicts with active vault (only if merging).
7. Head impact (will the active manifest revision change: yes/no).
8. Bandwidth preview (chunks already on relay vs to upload).

### Export reminder cadence (§16)

Default: **monthly** if the user hasn't run a fresh export in 30
days. Configurable Off / Weekly / Monthly / Quarterly / Yearly.
Per-occurrence dismissable, never blocking.

### Pointers

- Writer: `desktop/src/vault/export/bundle.py`.
- Reader and merge: `desktop/src/vault/import_/bundle.py`.
- Test vectors: `tests/protocol/vault-v1/export_bundle_v1.json`.

---

## §11. Relay migration (H2)

H2 is the relay-to-relay migration state machine: a vault on
relay A is verified into relay B *before* anyone commits to the
move. No flag-day, no orphan vaults if anything fails mid-way.

### States

```
idle → started → copying → verified → committed → idle (on target)
                                          ↓
                                        rollback → idle (on source)
```

- `started` — `POST /api/vaults/{id}/migration/start` on source
  is idempotent and returns a migration token.
- `copying` — client batch-uploads chunks to target via the
  standard `PUT /api/vaults/{id}/chunks/{chunk_id}` endpoint;
  dedup via `batch-head` skips chunks already on the target.
- `verified` —
  `GET /api/vaults/{id}/migration/verify-source` returns manifest
  hash + chunk count + ciphertext byte total. The client diffs
  against target; mismatches surface as
  `vault_migration_verify_failed`.
- `committed` — `PUT /api/vaults/{id}/migration/commit` on
  source marks the vault `migrated_to: <target_url>`, idempotent,
  source becomes read-only.

### Rollback and switch-back

Pre-commit cancellation is free — `POST
/api/vaults/{id}/migration/cancel` drops state on the source;
target chunks become orphans that the next GC sweep removes.

Post-commit, the source retains `previous_relay_url` for **7
days**. During that window, *Settings → Migration → Switch back*
reverses the move without re-copying. After 7 days the link
expires.

### Multi-device propagation

Migration is initiated by one device. Others learn on their next
`GET /header` — the response returns `migrated_to`; the client
switches its relay URL and stores `previous_relay_url` itself.
No coordination needed.

### Pointers

- Server: `server/src/Controllers/VaultController.php` migration
  routes + `server/src/Repositories/VaultsRepository.php` with
  `markMigratedTo` / `cancelMigration`.
- Desktop: `desktop/src/vault/migration/`.

---

## §12. Destructive actions & threat model

### Adversary list

- **Malicious or compromised relay.** Can delete blobs, hide /
  serve stale manifests, corrupt ciphertext. *Cannot* decrypt,
  forge encrypted updates, or know plaintext names. Mitigations:
  manifest hash chain, client-side AEAD, missing-chunk
  detection, integrity check (§14), protected exports, relay
  migration.
- **Public attacker with vault ID only.** Cannot do anything —
  vault ID alone is not access. Mitigations: rate limiting, no
  unauthenticated endpoints.
- **Attacker with device auth but no vault auth.** Cannot
  control the vault. Mitigation: separate vault auth layer.
- **Compromised authorised device.** The most serious case.
  Can issue valid deletes and grants up to its role. Cannot be
  *fully* prevented in v1. Mitigations: per-role permissions,
  fresh-unlock requirement on sensitive ops, soft delete +
  retention, **delayed** hard-purge, operation log, device
  revocation, protected exports as a re-baseline path.

### What v1 explicitly does not defend against

- A relay operator with full database access can delete
  everything. Only an independent backup (protected export)
  recovers from this.
- A brand-new device restoring from a recovery kit cannot
  detect "the relay just rolled the manifest back by N
  revisions" without a separate trusted witness. The integrity
  check + operation log will reveal it on the *first synced
  device*; the brand-new restore-only device sees only the
  state it's served.
- Ransomware that encrypts a bound folder *before* the
  detector trips. The detector buys time; it does not prevent
  initial damage.

### Destructive-action ledger

The seven destructive actions, their guards, and audit events:

| Action | Guard | Recoverable? | Audit event |
|---|---|---|---|
| **Delete file/folder** | Browser confirm | Until retention expires | (browser delete — folded into manifest revision history) |
| **Clear folder** | Typed-confirm of folder display name (case-sensitive, trimmed) + fresh unlock | Until retention expires | `vault.folder.cleared remote_folder_id=<id> tombstoned=<count> author=<device_id>` |
| **Clear vault** | Typed-confirm of full dashed vault ID (case-insensitive) + fresh unlock | Until retention expires | `vault.vault.cleared total_tombstoned=<count> author=<device_id>` |
| **Schedule hard purge** | Typed-confirm vault ID + delay (default 24 h, configurable hours) + fresh unlock + `admin` role | Cancellable until deadline fires | `vault.purge.scheduled scope=vault job_id=<id> scheduled_for=<epoch>` then `vault.purge.executed` or `vault.purge.cancelled` |
| **Disconnect device** | Alert confirm | Local only; vault unchanged | (local — no relay event) |
| **Revoke device grant** | Alert confirm with §14-locked wording | Future ops blocked; already-downloaded plaintext on the revoked device cannot be erased | `vault.device.revoked device_id=<id>` |
| **Rotate access secret** | Fresh unlock + `admin` role | Old secret valid for **7-day grace** per A5 | `vault.access_secret.rotated` |

The `keep_deleted_days = 30` default + the 24 h hard-purge grace
together mean a *paired* but-compromised device that runs a clear
takes at least 24 h to actually destroy anything — buying time for
the legitimate user to notice and revoke.

### Unlock timeout (§13)

Default: 15 min idle, on screen-lock, on quit. Configurable: Never
/ 5 / 15 / 30 / 60 min / On every sensitive action / On screen
lock only. Sensitive ops (clear vault, hard purge, rotate access
secret, revoke device, rotate recovery) **always** require fresh
unlock regardless of the timeout setting.

### Pointers

- Guard helpers: `desktop/src/windows_vault/tab_danger.py` plus
  the typed-confirm helpers in `desktop/src/vault/ops/clear.py`
  and `desktop/src/vault/ops/purge_schedule.py`.
- Diagnostic events: `docs/diagnostics.events.md` "### vault"
  section.

---

## §13. UI surfaces

The vault UI lives across five subprocess windows, the tray menu,
and the existing main settings window.

### Main settings → Vault section (D16)

A single toggle (`vault.active`) plus an **Open Vault settings…**
button. Toggle defaults to **ON** on fresh install. Wizard
cancellation never changes the toggle (A2). When the toggle is OFF
the tray submenu is hidden and background sync is paused.

### Vault Settings window (`vault-main`)

`Adw.ApplicationWindow`, 880 × 560, sidebar + stack. Sidebar tabs:

- **Recovery** — emergency-access block: status (Untested / Ready
  / Stale / Failed / Missing), last-tested timestamp, "Test
  recovery now" dialog, kit-management actions. Persistent banner
  at top of the window when status is not Ready.
- **Folders** — list (Name / Binding / Current / Stored / History
  / Status / LastSync), add / rename / pause / disconnect / sync
  now / upload / download / clear / settings. Uses
  `vault_folders_tab.py`.
- **Activity** — encrypted op-log timeline (the always-on layer)
  with search and per-kind filters.
- **Maintenance** — Quick + Full integrity check (see §14),
  debug-bundle download (redacted).
- **Migration** — current relay URL, previous relay (7-day
  switch-back window), cancel pending migration.
- **Danger zone** — disconnect vault, clear folder, clear whole
  vault, schedule hard purge (+ cancel pending). Each action
  uses the guard taxonomy from §12.
- **Devices, Security, Sync safety, Storage** — placeholders in
  v1 (real content lands with T13 access-secret rotation work).

### Vault Browser window (`vault-browser`)

Standalone `Adw.ApplicationWindow`. Sidebar list of remote folders
+ header bar with breadcrumb. File list shows Name / Size /
Modified / Versions / Status. Right pane shows version history +
per-row hamburger menus (Download / Versions / Delete / Restore).
The Wave 1–3.7 chrome refresh
(`temp/finished-plans/vault-browser-chrome-redesign.md`) landed all
the polish.

### Onboarding wizard (`vault-onboard`)

Create-new flow: relay picker → recovery passphrase entry + confirm
→ **mandatory recovery test** prompt → success screen with
recovery-kit save action. Argon2id derivation runs on a worker
thread with a spinner explaining the wait. Cancelling on a fresh
install leaves the toggle alone (A2 revised).

### Import wizard (`vault-import`)

Choose export bundle → enter passphrase → §17 preview → per-folder
merge dialogs (A4) → progress + completion → verification pass.

### Tray menu states (§D16)

Tray submenu visibility is gated by the `vault.active` toggle
*plus* the on-device vault existence check
(`tray/vault_submenu.py::should_show_vault_submenu` and
`vault_submenu_entries`).

| Toggle | Vault exists locally? | Submenu |
|---|---|---|
| OFF | (irrelevant) | hidden |
| ON | no | Create vault… / Import vault… |
| ON | yes | Open Vault… / Sync now / Export… / Import… / Settings |

### Pointers

- Windows: `desktop/src/windows_vault/main_window.py`,
  `windows_vault/onboard_window.py`,
  `windows_vault_browser/` (the package, post Wave 1.5),
  `windows_vault_import.py`.
- Tabs: `desktop/src/windows_vault/tab_*.py`.

---

## §14. Diagnostics

Two layers of audit trail, plus the integrity check, plus the
debug bundle.

### Activity / op-log (§21)

1. **Encrypted operation log** — lives in the manifest
   (`operation_log_tail` + archived `vault_op_log_segments`),
   always on, shared across devices. Captures file/folder ops,
   version restores, grants, eviction stages, mode changes,
   destructive actions. The Activity tab reads this. Never leaks
   keys, passphrases, or decrypted content.
2. **Local per-device log** — `~/.config/desktop-connector/logs/vault.log`,
   plaintext, **off by default**, gated on the main "Allow logging"
   toggle. Captures API calls, AEAD failures, stalls, file waits,
   integrity results. Never logs keys, passphrases, decrypted
   filenames, decrypted content, FCM tokens, or public keys (same
   policy as the rest of the project).

### Integrity check (§19)

Two levels:

- **Quick** — manifest hash chain + chunk-index sanity +
  AEAD-current-revision. Seconds. Available on demand; also fires
  automatically once a week if the desktop is idle ≥ 30 min.
- **Full** — decrypts every manifest revision and AEAD-verifies
  every reachable chunk. Manual only; minutes to hours.

Quick failure → prompt to run Full. Full failure → list affected
items, user marks them broken in a new manifest revision *or*
restores from an export bundle. The check **never auto-repairs by
deletion** — corruption is surfaced, not silently overwritten.

### Debug bundle

Generated on demand from Maintenance tab. Contents are aggressively
redacted (`scan_for_forbidden` regex set per `vault/diagnostics/`).
Sensitive substrings in keys (`secret`, `recovery`, `passphrase`,
`master_key`, `authorization`, `purge`, `token`, `bearer`,
`credential`, `private`) are dropped; values matching the standard
or url-safe base32-of-32-bytes shape are caught by the leak scan
and the bundle write aborts with `DebugBundleError` rather than
shipping the secret. Coverage:
`tests/protocol/test_desktop_vault_debug_bundle.py`.

### Pointers

- Diagnostics module: `desktop/src/vault/diagnostics/`.
- Event catalogue: `docs/diagnostics.events.md` "### vault"
  section.

---

## §15. Where the canonical bits live

When you need more detail than this doc, look here.

### Wire & byte detail

- `docs/protocol/vault-v1.md` — every endpoint's request /
  response / error shape. ~1000 lines.
- `docs/protocol/vault-v1-formats.md` — byte-exact AAD
  constructions, HKDF labels, manifest / chunk / header / recovery
  / export envelope formats. ~1100 lines.
- `tests/protocol/vault-v1/*.json` — cross-runtime test vectors
  exercised by both `pytest tests/protocol/` and
  `phpunit tests/Vault/`.

### Original decision rationale

- `temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md`
  — the locked decisions doc (D1–D16, A1–A21, gap closures
  §1–§22, error-code list, capability bits). Authoritative spec
  when there is any ambiguity between this doc and the wire / code.
- `temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-critical-risks-and-weaknesses.md`
  — the 20-risk catalogue + 8 acknowledged weaknesses. Drives the
  "critical-risks evaluation gate" still open per
  `docs/plans/vault-open-items.md`.
- `temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-{01..11}-*.md`
  — the original per-phase plans. Read these only when you need
  the *why* behind a specific choice; the *what* is here.
- `temp/finished-plans/desktop-connector-vault-plan-md/VAULT-progress.md`
  — historical tracker; status frozen at archive time.

### ADR entries

`docs/architecture-decisions.md` carries dated entries for choices
made *during* implementation (as opposed to pre-implementation in
T0). Notable vault-relevant entries:

- 2026-05-12 — wrong-passphrase rate limit is Argon2id-implicit
  (see §3).
- 2026-05-12 — cross-session vault-create orphans get a local-only
  resume affordance.

### Code anchors

The vault implementation is concentrated in three trees:

- `desktop/src/vault/` — all client logic (crypto, manifest,
  binding, ops, export, import, migration, diagnostics,
  recovery, grants).
- `desktop/src/windows_vault/` and
  `desktop/src/windows_vault_browser/` — the GTK windows.
- `server/src/{Controllers,Repositories,Crypto,Auth}/Vault*.php`
  + `server/migrations/00{2..4}_vault*.sql` — the relay surface.

### Live work

- `docs/plans/vault-open-items.md` — what remains before v1 is
  fully labelled (the critical-risks evaluation gate + four UI
  wire-up holes).
- `docs/plans/live-testing-followup.md` — rolling backlog of
  live-driver findings; the un-driven flows backlog
  (eviction, resume-after-kill, cross-device grant,
  concurrent edits, large folder bind, migration switch-back,
  ransomware detector, scheduled purge, debug bundle on a real
  install) feeds this doc's items 10+.
