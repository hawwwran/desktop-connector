<?php

/**
 * Marker class for parameter values that must be bound as SQLITE3_BLOB
 * rather than the default SQLITE3_TEXT inferred from a PHP string.
 *
 * Background: `SQLite3Stmt::bindValue($key, $stringValue)` without an
 * explicit type binds as TEXT, which goes through `sqlite3_bind_text()`
 * with -1 length — that truncates at the first null byte. SHA-256
 * digests, AEAD ciphertext, and X25519 keys all contain ~12% probability
 * of an embedded null per 32-byte field, so they MUST bind as BLOB to
 * round-trip cleanly. Repositories wrap these fields in `new Blob(...)`.
 */
class Blob
{
    public function __construct(public readonly string $bytes) {}
}

class Database
{
    private static ?Database $instance = null;
    private SQLite3 $db;

    private function __construct(string $dbPath)
    {
        $this->db = new SQLite3($dbPath);
        // busyTimeout MUST come first. The PRAGMA-exec form can itself
        // fail with "database is locked" under concurrent load before
        // any busy-wait is configured — that's what produced the big
        // yellow warning banner on the dashboard whenever two upload
        // workers hit PHP at the same time. The native busyTimeout()
        // call isn't a PRAGMA and can't be blocked, so it takes effect
        // before any subsequent exec/prepare.
        $this->db->busyTimeout(5000);
        $this->db->exec('PRAGMA journal_mode=WAL');
        $this->db->exec('PRAGMA foreign_keys=ON');
    }

    public static function getInstance(): self
    {
        if (self::$instance === null) {
            $dataDir = __DIR__ . '/../data';
            if (!is_dir($dataDir)) {
                mkdir($dataDir, 0700, true);
            }
            self::$instance = new self($dataDir . '/connector.db');
        }
        return self::$instance;
    }

    /**
     * Construct an isolated Database against an explicit path, bypassing
     * the singleton. Used by PHPUnit tests that need a fresh per-test
     * SQLite file; production never calls this.
     */
    public static function fromPath(string $dbPath): self
    {
        return new self($dbPath);
    }

    public function migrate(): void
    {
        $sql = file_get_contents(__DIR__ . '/../migrations/001_initial.sql');
        $this->db->exec($sql);

        // Add delivered_at column if missing (upgrade from older schema)
        $cols = $this->db->querySingle("SELECT sql FROM sqlite_master WHERE type='table' AND name='transfers'");
        if ($cols && strpos($cols, 'delivered_at') === false) {
            $this->db->exec('ALTER TABLE transfers ADD COLUMN delivered_at INTEGER DEFAULT 0');
        }

        // Add fcm_token column if missing (FCM push wake support)
        $deviceCols = $this->db->querySingle("SELECT sql FROM sqlite_master WHERE type='table' AND name='devices'");
        if ($deviceCols && strpos($deviceCols, 'fcm_token') === false) {
            $this->db->exec('ALTER TABLE devices ADD COLUMN fcm_token TEXT DEFAULT NULL');
        }

        // Track the last time an FCM push to this device was accepted by
        // Google's FCM service. Surfaces on the dashboard as "ready 12s ago" /
        // "ready 4m ago" so operators can tell at a glance whether pushes
        // are actually working vs the device just having a stale token.
        // Re-read $deviceCols since the block above may have just ALTERed.
        $deviceCols = $this->db->querySingle("SELECT sql FROM sqlite_master WHERE type='table' AND name='devices'");
        if ($deviceCols && strpos($deviceCols, 'fcm_last_success_at') === false) {
            $this->db->exec('ALTER TABLE devices ADD COLUMN fcm_last_success_at INTEGER DEFAULT NULL');
        }

        // Add chunks_downloaded column if missing (download progress tracking for sender)
        if ($cols && strpos($cols, 'chunks_downloaded') === false) {
            $this->db->exec('ALTER TABLE transfers ADD COLUMN chunks_downloaded INTEGER DEFAULT 0');
        }

        // Fasttrack: lightweight encrypted message relay between paired devices
        $this->db->exec('
            CREATE TABLE IF NOT EXISTS fasttrack_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id       TEXT NOT NULL,
                recipient_id    TEXT NOT NULL,
                encrypted_data  TEXT NOT NULL,
                created_at      INTEGER NOT NULL
            )
        ');
        $this->db->exec('CREATE INDEX IF NOT EXISTS idx_fasttrack_recipient ON fasttrack_messages(recipient_id, created_at)');

        // Ping rate limit: atomic per-(sender, recipient) cooldown slot
        $this->db->exec('
            CREATE TABLE IF NOT EXISTS ping_rate (
                sender_id       TEXT NOT NULL,
                recipient_id    TEXT NOT NULL,
                cooldown_until  INTEGER NOT NULL,
                PRIMARY KEY (sender_id, recipient_id)
            )
        ');

        // Streaming-relay columns. Additive; old clients keep
        // reading/writing the classic subset because `mode` defaults
        // to 'classic' and everything else is nullable / zero-default.
        // Design: docs/plans/streaming-improvement.md (§2 schema).
        //
        // Column-existence check goes via PRAGMA table_info — the other
        // migrations above use strpos() on sqlite_master.sql, which works
        // for distinctive names but gets fragile when you need to
        // distinguish `aborted` from `aborted_at`. PRAGMA is unambiguous.
        $existingCols = [];
        foreach ($this->queryAll('PRAGMA table_info(transfers)') as $row) {
            if (isset($row['name'])) {
                $existingCols[$row['name']] = true;
            }
        }
        if (!isset($existingCols['mode'])) {
            $this->db->exec("ALTER TABLE transfers ADD COLUMN mode TEXT NOT NULL DEFAULT 'classic'");
        }
        if (!isset($existingCols['aborted'])) {
            $this->db->exec('ALTER TABLE transfers ADD COLUMN aborted INTEGER NOT NULL DEFAULT 0');
        }
        if (!isset($existingCols['abort_reason'])) {
            $this->db->exec('ALTER TABLE transfers ADD COLUMN abort_reason TEXT');
        }
        if (!isset($existingCols['aborted_at'])) {
            $this->db->exec('ALTER TABLE transfers ADD COLUMN aborted_at INTEGER');
        }
        if (!isset($existingCols['stream_ready_at'])) {
            $this->db->exec('ALTER TABLE transfers ADD COLUMN stream_ready_at INTEGER');
        }
        if (!isset($existingCols['chunks_uploaded'])) {
            $this->db->exec('ALTER TABLE transfers ADD COLUMN chunks_uploaded INTEGER NOT NULL DEFAULT 0');
        }
        $this->db->exec('CREATE INDEX IF NOT EXISTS idx_transfers_aborted ON transfers(aborted)');

        // Vault schema (vault_v1). T1.1 — six tables under server/migrations/002_vault.sql.
        // Idempotent: every CREATE uses IF NOT EXISTS, so re-running on an existing DB is a no-op.
        $vaultSql = file_get_contents(__DIR__ . '/../migrations/002_vault.sql');
        $this->db->exec($vaultSql);

        // F-S16: reap two tables that were declared at T1.1 but never
        // wired by any controller — leftover dead schema. The CREATE
        // statements were dropped from the migration above so fresh
        // installs no longer get them; this DROP path catches existing
        // devices whose tables were created by an older migration. Both
        // tables have always been empty (no INSERT site ever shipped),
        // so DROP IF EXISTS is data-safe.
        //   - vault_audit_events: superseded by
        //     manifest.operation_log_tail (encrypted, client-built).
        //   - vault_chunk_uploads: §10 incomplete-upload reaper still
        //     planned; bringing the table back is part of that work.
        $this->db->exec('DROP INDEX IF EXISTS idx_vault_chunk_uploads_expires');
        $this->db->exec('DROP INDEX IF EXISTS idx_vault_audit_vault_time');
        $this->db->exec('DROP TABLE IF EXISTS vault_chunk_uploads');
        $this->db->exec('DROP TABLE IF EXISTS vault_audit_events');

        // Pre-sharding ``vault_manifests`` table — replaced by the
        // sharded ``vault_root_manifests`` + ``vault_folder_shards``
        // pair from migration 005. Old deployments get this DROP so
        // the dead schema doesn't accumulate.
        $this->db->exec('DROP INDEX IF EXISTS idx_vault_manifests_vault');
        $this->db->exec('DROP TABLE IF EXISTS vault_manifests');

        // Drop the legacy vaults.current_manifest_* columns on
        // upgrades from a pre-sharding schema. SQLite 3.35+ supports
        // ALTER TABLE DROP COLUMN; older versions would need the
        // table-rebuild pattern. PRAGMA table_info gates the call so
        // a fresh install (where the columns never existed) is a
        // no-op.
        $vaultsCols = [];
        $vinfo = $this->db->query("PRAGMA table_info(vaults)");
        while ($row = $vinfo->fetchArray(SQLITE3_ASSOC)) {
            $vaultsCols[$row['name']] = true;
        }
        $vinfo->finalize();
        if (isset($vaultsCols['current_manifest_revision'])) {
            $this->db->exec('ALTER TABLE vaults DROP COLUMN current_manifest_revision');
        }
        if (isset($vaultsCols['current_manifest_hash'])) {
            $this->db->exec('ALTER TABLE vaults DROP COLUMN current_manifest_hash');
        }

        // T9.2 — relay-migration intent table (idempotent CREATE IF NOT EXISTS).
        $migrationSql = file_get_contents(__DIR__ . '/../migrations/003_vault_migration.sql');
        if ($migrationSql !== false) {
            $this->db->exec($migrationSql);
        }

        // T13 — device grants + access-secret rotation audit.
        $grantsSql = file_get_contents(__DIR__ . '/../migrations/004_vault_device_grants.sql');
        if ($grantsSql !== false) {
            $this->db->exec($grantsSql);
        }

        // Phase B (2026-05-16) — manifest sharding. Drops the legacy
        // single-manifest history, adds root + folder shard history
        // tables + per-folder head pointer, and grows the `vaults` row
        // with current_root_revision / current_root_hash. ``vault_v1``
        // has never shipped, so the legacy `vault_manifests` data is
        // discarded; the dev twin re-seeds its vault via the suite-
        // start setup.
        //
        // ``ALTER TABLE ADD COLUMN`` fails if the column already
        // exists; on fresh installs the columns are brand-new, on
        // upgrades from a prior dev twin they might already exist.
        // SQLite has no ``IF NOT EXISTS`` for ALTER, so we probe
        // ``PRAGMA table_info`` per column and run only the ALTERs
        // that are still needed. The CREATE TABLE / INDEX statements
        // are unconditionally idempotent via ``IF NOT EXISTS``; the
        // ``DROP TABLE IF EXISTS vault_manifests`` is also a no-op
        // on a re-run. Each statement is executed directly from PHP
        // (no SQL-file regex parsing) so a future migration adding a
        // semicolon inside a quoted literal won't fight a fragile
        // splitter.
        $vaultColumns = [];
        $info = $this->db->query("PRAGMA table_info(vaults)");
        while ($row = $info->fetchArray(SQLITE3_ASSOC)) {
            $vaultColumns[$row['name']] = true;
        }
        // Finalize the PRAGMA cursor before any DDL runs — leaving
        // it open holds a read lock that fights subsequent DDL and
        // emits a "database table is locked" warning under PHPUnit's
        // strict-warning mode.
        $info->finalize();

        if (!isset($vaultColumns['current_root_revision'])) {
            $this->db->exec(
                "ALTER TABLE vaults ADD COLUMN current_root_revision INTEGER NOT NULL DEFAULT 1"
            );
        }
        if (!isset($vaultColumns['current_root_hash'])) {
            $this->db->exec(
                "ALTER TABLE vaults ADD COLUMN current_root_hash TEXT NOT NULL DEFAULT ''"
            );
        }

        // Three new tables + their indexes. ``IF NOT EXISTS`` on each
        // makes re-runs a no-op. The byte-exact schemas live in
        // migrations/005_vault_manifest_shards.sql as the canonical
        // record + documentation; this PHP path is the executor and
        // must stay in sync with the file (server/tests/Vault/
        // VaultSchemaTest covers the round-trip).
        $this->db->exec(
            'CREATE TABLE IF NOT EXISTS vault_root_manifests (
                vault_id              TEXT    NOT NULL,
                root_revision         INTEGER NOT NULL,
                parent_root_revision  INTEGER NOT NULL DEFAULT 0,
                root_hash             TEXT    NOT NULL,
                root_ciphertext       BLOB    NOT NULL,
                root_size             INTEGER NOT NULL,
                author_device_id      TEXT    NOT NULL,
                created_at            INTEGER NOT NULL,
                PRIMARY KEY (vault_id, root_revision)
            )'
        );
        $this->db->exec(
            'CREATE INDEX IF NOT EXISTS idx_vault_root_manifests_vault
                ON vault_root_manifests (vault_id, root_revision DESC)'
        );
        $this->db->exec(
            'CREATE TABLE IF NOT EXISTS vault_folder_shards (
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
            )'
        );
        $this->db->exec(
            'CREATE INDEX IF NOT EXISTS idx_vault_folder_shards_vault
                ON vault_folder_shards (vault_id, remote_folder_id, shard_revision DESC)'
        );
        $this->db->exec(
            "CREATE TABLE IF NOT EXISTS vault_folder_shard_heads (
                vault_id                TEXT    NOT NULL,
                remote_folder_id        TEXT    NOT NULL,
                current_shard_revision  INTEGER NOT NULL,
                current_shard_hash      TEXT    NOT NULL DEFAULT '',
                updated_at              INTEGER NOT NULL,
                PRIMARY KEY (vault_id, remote_folder_id)
            )"
        );
        $this->db->exec(
            'CREATE INDEX IF NOT EXISTS idx_vault_folder_shard_heads_vault
                ON vault_folder_shard_heads (vault_id)'
        );

        // Review §1.H1 — vault auth + create rate limit table per
        // protocol §10. Loaded from the SQL file like the other
        // vault migrations.
        $authAttemptsSql = file_get_contents(
            __DIR__ . '/../migrations/006_vault_auth_attempts.sql'
        );
        if ($authAttemptsSql !== false) {
            $this->db->exec($authAttemptsSql);
        }
    }

    public function query(string $sql, array $params = []): SQLite3Result
    {
        $stmt = $this->db->prepare($sql);
        if ($stmt === false) {
            throw new RuntimeException(
                'SQLite prepare failed: ' . $this->db->lastErrorMsg(),
                $this->db->lastErrorCode()
            );
        }
        foreach ($params as $key => $value) {
            // Wrap binary values in Blob so PHP's default SQLITE3_TEXT
            // binding doesn't truncate at the first null byte. Plain
            // strings/ints/etc. fall through to type inference.
            if ($value instanceof Blob) {
                $stmt->bindValue($key, $value->bytes, SQLITE3_BLOB);
            } else {
                $stmt->bindValue($key, $value);
            }
        }
        // Suppress the PHP warning that SQLite3Stmt::execute() emits on
        // constraint violations etc. — we surface the failure as a clean
        // RuntimeException with the underlying message/code instead, so
        // controllers can map it to ApiError without parsing PHP warnings.
        $result = @$stmt->execute();
        if ($result === false) {
            throw new RuntimeException(
                'SQLite execute failed: ' . $this->db->lastErrorMsg(),
                $this->db->lastErrorCode()
            );
        }
        return $result;
    }

    public function querySingle(string $sql, array $params = []): ?array
    {
        $result = $this->query($sql, $params);
        $row = $result->fetchArray(SQLITE3_ASSOC);
        return $row === false ? null : $row;
    }

    public function queryAll(string $sql, array $params = []): array
    {
        $result = $this->query($sql, $params);
        $rows = [];
        while ($row = $result->fetchArray(SQLITE3_ASSOC)) {
            $rows[] = $row;
        }
        return $rows;
    }

    public function execute(string $sql, array $params = []): void
    {
        $this->query($sql, $params);
    }

    public function lastInsertId(): int
    {
        return $this->db->lastInsertRowID();
    }

    public function changes(): int
    {
        return $this->db->changes();
    }
}
