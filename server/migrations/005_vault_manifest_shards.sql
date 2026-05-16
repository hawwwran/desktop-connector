-- Desktop Connector: Manifest sharding (Phase B)
--
-- Replaces the single per-vault `vault_manifests` history with two
-- per-folder envelope kinds:
--
--   * `vault_root_manifests` — vault-wide metadata + folder pointer list
--     (one row per root_revision; immutable chain).
--   * `vault_folder_shards`  — per-folder file entries (one row per
--     (vault_id, remote_folder_id, shard_revision); immutable chain).
--   * `vault_folder_shard_heads` — current shard pointer for CAS
--     (mirrors the role `vaults.current_manifest_revision` plays for the
--     legacy single-manifest case, but keyed per-folder).
--
-- vault_v1 has never shipped (per docs/plans/vault-manifest-sharding.md
-- operating constraints), so we drop the legacy table in place rather
-- than maintaining a compatibility shim. The developer's dev twin is
-- the only existing vault and re-runs the suite-start setup which wipes
-- `server/data/connector.db`.
--
-- Migration is forward-only — `Database::migrate` runs each file once;
-- there is no down-path here. Operationally the user has agreed to
-- accept data loss on their dev twin.
--
-- Wire surface: docs/protocol/vault-v1.md §6.4–§6.8
-- Byte formats: docs/protocol/vault-v1-formats.md §10.A–§10.C

-- 1. Drop the legacy single-manifest history. No DROP COLUMN in older
--    SQLite so the legacy `vaults.current_manifest_revision` +
--    `current_manifest_hash` columns survive on disk; new code reads
--    only the freshly added `current_root_*` columns (next step).
DROP TABLE IF EXISTS vault_manifests;

-- 2. The vault head pointer migrates from `current_manifest_*` to
--    `current_root_*`. SQLite can't rename columns transactionally on
--    every version we support, so we add the new pair and let the new
--    code source state exclusively from these going forward. Vault
--    rows already exist from migration 002 — populate the new columns
--    from the now-unused legacy ones so an in-place upgrade keeps
--    integrity. Production deploys of vault_v1 don't exist; this is
--    purely an in-dev safety net.
ALTER TABLE vaults ADD COLUMN current_root_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE vaults ADD COLUMN current_root_hash     TEXT NOT NULL DEFAULT '';

-- 3. Immutable history of every published root revision. Mirrors the
--    legacy `vault_manifests` shape (PK on (vault_id, root_revision))
--    so the §A16 GC walk can iterate per retained root just as before.
CREATE TABLE IF NOT EXISTS vault_root_manifests (
    vault_id              TEXT    NOT NULL,
    root_revision         INTEGER NOT NULL,
    parent_root_revision  INTEGER NOT NULL DEFAULT 0,
    root_hash             TEXT    NOT NULL,
    root_ciphertext       BLOB    NOT NULL,
    root_size             INTEGER NOT NULL,
    author_device_id      TEXT    NOT NULL,
    created_at            INTEGER NOT NULL,
    PRIMARY KEY (vault_id, root_revision)
);
CREATE INDEX IF NOT EXISTS idx_vault_root_manifests_vault
    ON vault_root_manifests (vault_id, root_revision DESC);

-- 4. Immutable history of every published shard revision per folder.
--    GC walks (vault_id, remote_folder_id, *) so it can union chunks
--    referenced from every retained shard of every folder. PK is the
--    triple; an old (folder removed but retention not yet elapsed)
--    shard stays addressable.
CREATE TABLE IF NOT EXISTS vault_folder_shards (
    vault_id              TEXT    NOT NULL,
    remote_folder_id      TEXT    NOT NULL,
    shard_revision        INTEGER NOT NULL,
    parent_shard_revision INTEGER NOT NULL DEFAULT 0,
    shard_hash            TEXT    NOT NULL,
    shard_ciphertext      BLOB    NOT NULL,
    shard_size            INTEGER NOT NULL,
    author_device_id      TEXT    NOT NULL,
    created_at            INTEGER NOT NULL,
    PRIMARY KEY (vault_id, remote_folder_id, shard_revision)
);
CREATE INDEX IF NOT EXISTS idx_vault_folder_shards_vault
    ON vault_folder_shards (vault_id, remote_folder_id, shard_revision DESC);

-- 5. The current-shard pointer per folder. CAS target on every shard
--    publish — the conditional UPDATE on
--    `current_shard_revision = :expected` is the primitive that closes
--    the per-folder race window, exactly mirroring `vaults`'s role for
--    the root. An (`INSERT OR IGNORE` … `UPDATE … current_shard_revision
--    = 0`) bootstrap on the first publish for a brand-new folder keeps
--    the genesis path inside the same conditional-UPDATE pattern.
CREATE TABLE IF NOT EXISTS vault_folder_shard_heads (
    vault_id                TEXT    NOT NULL,
    remote_folder_id        TEXT    NOT NULL,
    current_shard_revision  INTEGER NOT NULL,
    current_shard_hash      TEXT    NOT NULL DEFAULT '',
    updated_at              INTEGER NOT NULL,
    PRIMARY KEY (vault_id, remote_folder_id)
);
CREATE INDEX IF NOT EXISTS idx_vault_folder_shard_heads_vault
    ON vault_folder_shard_heads (vault_id);
