-- Desktop Connector: Vault Schema (vault_v1)
--
-- Tables added in T1.1. Spec references:
--   T0 §D2  — quota + eviction (vaults.quota_ciphertext_bytes, used_ciphertext_bytes)
--   T0 §D13 — storage isolation (storage path under server/storage/vaults/)
--   T0 §D14 — op-log segments (vault_op_log_segments)
--   T0 §A19 — chunk-id format (^ch_v1_[a-z2-7]{24}$)
--   T0 §A21 — quota counts unique chunks across the whole vault
--
-- Wire surface: docs/protocol/vault-v1.md
-- Byte formats: docs/protocol/vault-v1-formats.md
--
-- Storage path convention (D13):
--   server/storage/vaults/<vault_id>/<chunk_id_prefix>/<chunk_id>
-- where <chunk_id_prefix> is the first 2 chars of the random portion
-- of chunk_id (chars after `ch_v1_`). Created lazily by chunk-upload code.
--
-- Timestamp convention: Unix epoch seconds (INTEGER), matching the existing
-- transfer/fasttrack tables. Wire responses format these as RFC 3339 at
-- the controller layer.

-- vault metadata, quota, migration state (D2 / A21 / H2)
CREATE TABLE IF NOT EXISTS vaults (
    vault_id                  TEXT PRIMARY KEY,                                   -- 12-char base32 (formats §3.1)
    vault_access_token_hash   BLOB NOT NULL,                                      -- SHA-256(vault_access_secret)
    encrypted_header          BLOB NOT NULL,                                      -- envelope per formats §9
    header_revision           INTEGER NOT NULL DEFAULT 1,
    header_hash               TEXT NOT NULL,                                      -- hex sha-256
    current_manifest_revision INTEGER NOT NULL DEFAULT 1,
    current_manifest_hash     TEXT NOT NULL,                                      -- hex sha-256
    used_ciphertext_bytes     INTEGER NOT NULL DEFAULT 0,                         -- A21: global, unique chunks
    chunk_count               INTEGER NOT NULL DEFAULT 0,
    quota_ciphertext_bytes    INTEGER NOT NULL DEFAULT 1073741824,                -- D2: 1 GB default
    purge_token_hash          BLOB,                                                -- T14: hash(purge_secret); set at create
    migrated_to               TEXT,                                                -- H2: target relay URL after commit
    migrated_at               INTEGER,
    previous_relay_url        TEXT,                                                -- H2: source URL after switch-back
    soft_deleted_at           INTEGER,
    pending_purge_at          INTEGER,
    created_at                INTEGER NOT NULL,
    updated_at                INTEGER NOT NULL
);

-- immutable manifest revisions (CAS chain, formats §10)
CREATE TABLE IF NOT EXISTS vault_manifests (
    vault_id            TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    parent_revision     INTEGER NOT NULL DEFAULT 0,                                -- 0 = genesis
    manifest_hash       TEXT NOT NULL,                                             -- hex sha-256
    manifest_ciphertext BLOB NOT NULL,                                             -- envelope per formats §10.1
    manifest_size       INTEGER NOT NULL,
    author_device_id    TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    PRIMARY KEY (vault_id, revision)
);

-- relay-stored encrypted chunks (D13, A19)
CREATE TABLE IF NOT EXISTS vault_chunks (
    vault_id           TEXT NOT NULL,
    chunk_id           TEXT NOT NULL,                                              -- ^ch_v1_[a-z2-7]{24}$
    ciphertext_size    INTEGER NOT NULL,
    chunk_hash         TEXT NOT NULL,                                              -- hex sha-256 of envelope bytes
    storage_path       TEXT NOT NULL,                                              -- relative to server/storage/
    state              TEXT NOT NULL DEFAULT 'active',                             -- active|retained|gc_pending|purged
    created_at         INTEGER NOT NULL,
    last_referenced_at INTEGER NOT NULL,
    PRIMARY KEY (vault_id, chunk_id)
);

-- in-flight chunk uploads; orphan-collected after expires_at if unreferenced
-- by any retained manifest revision (vault-v1.md §10 — 24h TTL).
CREATE TABLE IF NOT EXISTS vault_chunk_uploads (
    vault_id    TEXT NOT NULL,
    chunk_id    TEXT NOT NULL,
    uploaded_at INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    PRIMARY KEY (vault_id, chunk_id)
);

-- QR-assisted device grant flow (T13, vault-v1.md §8)
CREATE TABLE IF NOT EXISTS vault_join_requests (
    join_request_id        TEXT PRIMARY KEY,                                       -- jr_v1_<24base32>
    vault_id               TEXT NOT NULL,
    state                  TEXT NOT NULL DEFAULT 'pending',                        -- pending|claimed|approved|rejected|expired
    ephemeral_admin_pubkey BLOB NOT NULL,                                          -- 32 bytes X25519
    claimant_device_id     TEXT,
    claimant_pubkey        BLOB,                                                    -- 32 bytes X25519, set on claim
    device_name            TEXT,                                                    -- claimant-supplied label
    approved_role          TEXT,                                                    -- read-only|browse-upload|sync|admin (D11)
    wrapped_vault_grant    BLOB,                                                    -- AEAD ciphertext, set on approve
    granted_by_device_id   TEXT,
    expires_at             INTEGER NOT NULL,                                        -- T13.2: 15 min default
    created_at             INTEGER NOT NULL,
    claimed_at             INTEGER,
    approved_at            INTEGER,
    rejected_at            INTEGER
);

-- server-side audit log; backs the encrypted activity timeline (T17.1)
CREATE TABLE IF NOT EXISTS vault_audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id   TEXT NOT NULL,
    device_id  TEXT,                                                                 -- nullable for system events
    event_type TEXT NOT NULL,                                                        -- e.g. 'vault.create', 'manifest.publish'
    details    TEXT,                                                                 -- JSON-encoded context
    created_at INTEGER NOT NULL
);

-- GC plans + scheduled hard-purge jobs (T14, vault-v1.md §6.12-§6.14).
-- One table covers both because they share state machine + execute path.
-- 'kind' distinguishes ephemeral plans from delayed purges.
CREATE TABLE IF NOT EXISTS vault_gc_jobs (
    job_id                 TEXT PRIMARY KEY,                                          -- pl_v1_... (plan) or jb_v1_... (purge)
    vault_id               TEXT NOT NULL,
    kind                   TEXT NOT NULL,                                             -- 'sync_plan'|'expiry_plan'|'scheduled_purge'
    state                  TEXT NOT NULL DEFAULT 'planned',                           -- planned|executing|completed|cancelled|expired|failed
    target_chunk_ids       TEXT NOT NULL,                                             -- JSON array of chunk_ids
    scheduled_for          INTEGER,                                                   -- T14: 24h delay; null for in-flight plans
    expires_at             INTEGER NOT NULL,                                          -- plan TTL (15min) or job execution deadline
    started_at             INTEGER,
    completed_at           INTEGER,
    cancelled_at           INTEGER,
    deleted_count          INTEGER NOT NULL DEFAULT 0,
    freed_bytes            INTEGER NOT NULL DEFAULT 0,
    requested_by_device_id TEXT NOT NULL,
    created_at             INTEGER NOT NULL
);

-- archived encrypted op-log segments (D14)
CREATE TABLE IF NOT EXISTS vault_op_log_segments (
    vault_id   TEXT NOT NULL,
    segment_id TEXT PRIMARY KEY,                                                       -- os_v1_<24base32>
    seq        INTEGER NOT NULL,
    first_ts   INTEGER NOT NULL,
    last_ts    INTEGER NOT NULL,
    ciphertext BLOB NOT NULL,
    hash       TEXT NOT NULL,                                                          -- hex sha-256
    created_at INTEGER NOT NULL
);

-- indexes for hot-path lookups
CREATE INDEX IF NOT EXISTS idx_vault_chunks_state           ON vault_chunks(state, last_referenced_at);
CREATE INDEX IF NOT EXISTS idx_vault_chunks_vault           ON vault_chunks(vault_id, state);
CREATE INDEX IF NOT EXISTS idx_vault_manifests_vault        ON vault_manifests(vault_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_vault_chunk_uploads_expires  ON vault_chunk_uploads(expires_at);
CREATE INDEX IF NOT EXISTS idx_vault_join_requests_vault    ON vault_join_requests(vault_id, state);
CREATE INDEX IF NOT EXISTS idx_vault_join_requests_expires  ON vault_join_requests(expires_at);
CREATE INDEX IF NOT EXISTS idx_vault_audit_vault_time       ON vault_audit_events(vault_id, created_at);
CREATE INDEX IF NOT EXISTS idx_vault_gc_jobs_state_sched    ON vault_gc_jobs(state, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_vault_gc_jobs_vault          ON vault_gc_jobs(vault_id, state);
CREATE INDEX IF NOT EXISTS idx_vault_op_log_segments_vault  ON vault_op_log_segments(vault_id, seq DESC);
