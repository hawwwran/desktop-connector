-- Review §1.H1: vault auth + create rate limits per protocol §10.
--
-- Spec mandates:
--   - 10 vault_auth attempts per (device_id, vault_id) per minute
--   - 5 create-vault attempts per device_id per hour
--
-- Pre-fix neither was enforced: a compromised paired device could
-- hammer vault_auth_failed indefinitely with no telemetry signal and
-- no defence-in-depth against a future weakening of the 32-byte
-- bearer secret (e.g. operator misconfig).
--
-- Fixed-window counter keyed (device_id, scope, kind). ``scope`` is
-- the vault_id for the 'auth' kind, the empty string for 'create'.
-- The composite primary key gives us O(1) UPSERT lookups. Each
-- entry tracks how many attempts have been recorded inside the
-- current window — when the count exceeds the kind's cap, every
-- subsequent attempt returns 429 with Retry-After = window_end - now.

CREATE TABLE IF NOT EXISTS vault_auth_attempts (
    device_id    TEXT NOT NULL,
    scope        TEXT NOT NULL,        -- vault_id for kind='auth', '' for kind='create'
    kind         TEXT NOT NULL CHECK (kind IN ('auth', 'create')),
    window_start INTEGER NOT NULL,     -- epoch seconds; window_end = window_start + window_s
    attempts     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, scope, kind)
);

CREATE INDEX IF NOT EXISTS idx_vault_auth_attempts_window
    ON vault_auth_attempts(window_start);
