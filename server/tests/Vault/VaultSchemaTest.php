<?php

declare(strict_types=1);

use PHPUnit\Framework\Attributes\RunTestsInSeparateProcesses;
use PHPUnit\Framework\TestCase;

/**
 * Vault schema invariants — pin which tables exist after a fresh
 * migrate(). Catches accidental re-introduction of dead schema (F-S16)
 * and accidental removal of live tables.
 */
#[RunTestsInSeparateProcesses]
final class VaultSchemaTest extends TestCase
{
    private string $dbPath;
    private Database $db;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_schema_db_');
        $this->db = Database::fromPath($this->dbPath);
        $this->db->migrate();
    }

    protected function tearDown(): void
    {
        unset($this->db);
        if (is_string($this->dbPath) && file_exists($this->dbPath)) {
            @unlink($this->dbPath);
        }
    }

    public function test_dead_schema_is_dropped_after_migrate(): void
    {
        // F-S16: ``vault_audit_events`` and ``vault_chunk_uploads``
        // were declared at T1.1 but never wired by any controller.
        // The migration runner now drops them so existing devices'
        // empty tables don't accumulate; this test pins that state.
        $tables = $this->existingTables();
        self::assertNotContains('vault_audit_events', $tables);
        self::assertNotContains('vault_chunk_uploads', $tables);
    }

    public function test_live_vault_tables_still_exist(): void
    {
        // Sentinel: the F-S16 cleanup must NOT take down any live
        // tables. The set below is the production schema. Phase B
        // (2026-05-16) replaced ``vault_manifests`` with
        // ``vault_root_manifests`` + ``vault_folder_shards`` +
        // ``vault_folder_shard_heads`` per the manifest-sharding work
        // (use case B); the legacy table is no longer expected.
        $tables = $this->existingTables();
        $expected = [
            'vault_access_secret_rotations',
            'vault_chunks',
            'vault_device_grants',
            'vault_folder_shard_heads',
            'vault_folder_shards',
            'vault_gc_jobs',
            'vault_join_requests',
            'vault_migration_intents',
            'vault_op_log_segments',
            'vault_root_manifests',
            'vaults',
        ];
        foreach ($expected as $table) {
            self::assertContains(
                $table, $tables,
                "F-S16 reaper must not have dropped {$table}"
            );
        }
        self::assertNotContains(
            'vault_manifests', $tables,
            'legacy single-manifest history table should be gone post Phase B',
        );
    }

    public function test_dead_schema_drop_is_idempotent_when_table_already_absent(): void
    {
        // A re-run of migrate() on a freshly-migrated DB must not
        // raise — the DROP statements use IF EXISTS so the tables
        // stay absent and no error fires.
        $this->db->migrate();
        $this->db->migrate();
        $this->assertNotContains('vault_audit_events', $this->existingTables());
    }

    public function test_dead_schema_drop_handles_pre_existing_table_data_safely(): void
    {
        // F-S16 risk surface: a device that was migrated before the
        // drop landed has empty ``vault_audit_events`` /
        // ``vault_chunk_uploads`` tables on disk. We simulate that
        // legacy state by re-creating both tables and re-running
        // migrate() — the runner's DROP path must reap them.
        $this->db->execute('CREATE TABLE IF NOT EXISTS vault_audit_events (id INTEGER PRIMARY KEY)');
        $this->db->execute('CREATE TABLE IF NOT EXISTS vault_chunk_uploads (vault_id TEXT, chunk_id TEXT)');
        self::assertContains('vault_audit_events', $this->existingTables());
        self::assertContains('vault_chunk_uploads', $this->existingTables());

        $this->db->migrate();

        $tables = $this->existingTables();
        self::assertNotContains('vault_audit_events', $tables);
        self::assertNotContains('vault_chunk_uploads', $tables);
    }

    /** @return list<string> */
    private function existingTables(): array
    {
        $names = [];
        foreach ($this->db->queryAll(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as $row) {
            $names[] = (string)$row['name'];
        }
        return $names;
    }
}
