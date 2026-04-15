<?php

class Database
{
    private static ?Database $instance = null;
    private SQLite3 $db;

    private function __construct(string $dbPath)
    {
        $this->db = new SQLite3($dbPath);
        $this->db->exec('PRAGMA journal_mode=WAL');
        $this->db->exec('PRAGMA busy_timeout=5000');
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
    }

    public function query(string $sql, array $params = []): SQLite3Result
    {
        $stmt = $this->db->prepare($sql);
        foreach ($params as $key => $value) {
            $stmt->bindValue($key, $value);
        }
        return $stmt->execute();
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
