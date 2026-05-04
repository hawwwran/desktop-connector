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

        // Vault schema (vault_v1). T1.1 — eight tables under server/migrations/002_vault.sql.
        // Idempotent: every CREATE uses IF NOT EXISTS, so re-running on an existing DB is a no-op.
        $vaultSql = file_get_contents(__DIR__ . '/../migrations/002_vault.sql');
        $this->db->exec($vaultSql);

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
