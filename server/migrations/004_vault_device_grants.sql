-- T13 — Granted devices + access-secret rotation history.
--
-- vault_join_requests already lives in 002_vault.sql; this migration
-- adds the *post-approval* tables: which devices are currently
-- authorised to act on the vault, and what role they have. Revoking
-- a device is a UPDATE here — the row stays for audit, but
-- `revoked_at` is set so subsequent vault ops return
-- vault_access_denied.
--
-- Per §A5, access-secret rotation is the only allowed rotation in
-- v1. The vaults table already carries the active hash (via
-- vault_access_token_hash); we keep an audit trail of rotations
-- here so the "tell other devices" banner can show *when* the
-- secret last changed.

CREATE TABLE IF NOT EXISTS vault_device_grants (
    grant_id           TEXT PRIMARY KEY,                  -- dg_v1_<24base32>
    vault_id           TEXT NOT NULL REFERENCES vaults(vault_id) ON DELETE CASCADE,
    device_id          TEXT NOT NULL,                     -- 32-hex-char per protocol
    device_name        TEXT,                               -- claimant-supplied label
    role               TEXT NOT NULL,                      -- read-only|browse-upload|sync|admin (D11)
    granted_by         TEXT NOT NULL,                      -- approver device_id
    granted_via        TEXT NOT NULL DEFAULT 'qr',         -- 'qr'|'create' (the creator gets an implicit grant)
    granted_at         INTEGER NOT NULL,
    revoked_at         INTEGER,                            -- nullable; set on DELETE .../device-grants/{id}
    revoked_by         TEXT,                               -- which admin pressed Revoke
    last_seen_at       INTEGER,                            -- last successful vault auth from this device
    UNIQUE (vault_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_vault_device_grants_vault_active
    ON vault_device_grants(vault_id, revoked_at);

-- §A5 access-secret rotation audit. One row per rotation; the
-- current hash is still in `vaults.vault_access_token_hash`. This
-- table only carries history for the "tell other devices" banner +
-- diagnostics.
CREATE TABLE IF NOT EXISTS vault_access_secret_rotations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id           TEXT NOT NULL REFERENCES vaults(vault_id) ON DELETE CASCADE,
    rotated_at         INTEGER NOT NULL,
    rotated_by         TEXT NOT NULL,                      -- admin device_id
    triggered_by_revoke_grant_id TEXT                       -- NULL unless this rotation came from a "Revoke and rotate" combo
);

CREATE INDEX IF NOT EXISTS idx_vault_access_secret_rotations_vault
    ON vault_access_secret_rotations(vault_id, rotated_at DESC);
