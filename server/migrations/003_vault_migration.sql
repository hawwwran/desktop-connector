-- T9.2 — Per-vault migration intent (§H2).
--
-- One row per vault while a relay-to-relay migration is in flight.
-- Recorded by POST /api/vaults/{id}/migration/start; consumed by
-- /migration/verify-source and /migration/commit; cleared on rollback
-- (DELETE) or post-commit (kept for audit, but the vault itself becomes
-- read-only via vaults.migrated_to).
--
-- The token is the bearer secret that the *initiating* device hands to
-- the target relay so it can prove the source intended this migration.
-- We store sha256(token) so a leak of the DB doesn't grant migration
-- authority retroactively (same shape as vaults.vault_access_token_hash).

CREATE TABLE IF NOT EXISTS vault_migration_intents (
    vault_id           TEXT PRIMARY KEY REFERENCES vaults(vault_id) ON DELETE CASCADE,
    token_hash         BLOB NOT NULL,                 -- sha256(token), 32 bytes
    target_relay_url   TEXT NOT NULL,
    started_at         INTEGER NOT NULL,
    verified_at        INTEGER,
    committed_at       INTEGER,
    initiating_device  TEXT NOT NULL                  -- X-Device-ID at /start time
);

CREATE INDEX IF NOT EXISTS idx_vault_migration_intents_started_at
    ON vault_migration_intents(started_at);
