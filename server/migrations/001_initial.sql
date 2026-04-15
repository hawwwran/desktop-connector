-- Desktop Connector: Server Schema

CREATE TABLE IF NOT EXISTS devices (
    device_id       TEXT PRIMARY KEY,
    public_key      TEXT NOT NULL,
    auth_token      TEXT NOT NULL UNIQUE,
    device_type     TEXT NOT NULL DEFAULT 'unknown',  -- 'desktop' or 'phone'
    created_at      INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pairing_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    desktop_id      TEXT NOT NULL,
    phone_id        TEXT NOT NULL,
    phone_pubkey    TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    claimed         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pairings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_a_id     TEXT NOT NULL,
    device_b_id     TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    bytes_transferred INTEGER DEFAULT 0,
    transfer_count  INTEGER DEFAULT 0,
    UNIQUE(device_a_id, device_b_id)
);

CREATE TABLE IF NOT EXISTS transfers (
    id              TEXT PRIMARY KEY,
    sender_id       TEXT NOT NULL,
    recipient_id    TEXT NOT NULL,
    encrypted_meta  TEXT NOT NULL,
    chunk_count     INTEGER NOT NULL,
    chunks_received INTEGER DEFAULT 0,
    complete        INTEGER DEFAULT 0,
    created_at      INTEGER NOT NULL,
    downloaded      INTEGER DEFAULT 0,
    delivered_at    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    transfer_id     TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    blob_path       TEXT NOT NULL,
    blob_size       INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (transfer_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_transfers_recipient ON transfers(recipient_id, complete, downloaded);
CREATE INDEX IF NOT EXISTS idx_pairing_req_desktop ON pairing_requests(desktop_id, claimed);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_pairings_devices ON pairings(device_a_id, device_b_id);
