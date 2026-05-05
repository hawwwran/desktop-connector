<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * F-T04 / F-T05 — unit tests for ``VaultMigrationIntentsRepository``.
 *
 * Pin the contract that the controller relies on: idempotent insert
 * (existing token wins on retry), independent verified/committed
 * timestamps, and the COALESCE preservation of original timestamps
 * across re-marks. Service-level coverage lives in
 * ``test_server_vault_migration.py``; this is the byte-shape test.
 */
final class VaultMigrationIntentsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultMigrationIntentsRepository $repo;

    private const VAULT_ID = 'H9K7M4Q2Z8TD';
    private const TOKEN_A  = "\xaa\xbb\xcc\xdd\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"
                           . "\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b";
    private const TOKEN_B  = "\xff\xee\xdd\xcc\xbb\xaa\x99\x88\x77\x66\x55\x44\x33\x22\x11\x00"
                           . "\x0f\x0e\x0d\x0c\x0b\x0a\x09\x08\x07\x06\x05\x04\x03\x02\x01\x00";
    private const DEVICE   = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const TARGET_A = 'https://relay-a.example.test';
    private const TARGET_B = 'https://relay-b.example.test';

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_mig_intents_test_');
        $this->db = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->repo = new VaultMigrationIntentsRepository($this->db);

        // FK to vaults(vault_id) — seed a parent row.
        $vaultsRepo = new VaultsRepository($this->db);
        $vaultsRepo->create(
            self::VAULT_ID,
            "\x00\x00\x00\x00",
            "\xde\xad\xbe\xef",
            'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344',
            'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff',
            1714680000,
        );
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    // ---------------------------------------------------------------- recordIntent

    public function test_recordIntent_first_call_creates_row(): void
    {
        $result = $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        self::assertTrue($result['created']);
        $row = $result['record'];
        self::assertSame(self::VAULT_ID, $row['vault_id']);
        self::assertSame(self::TARGET_A, $row['target_relay_url']);
        self::assertSame(self::DEVICE, $row['initiating_device']);
        self::assertSame(1000, (int)$row['started_at']);
        self::assertNull($row['verified_at']);
        self::assertNull($row['committed_at']);
    }

    public function test_recordIntent_idempotent_returns_existing_row(): void
    {
        $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        // Second call with the same vault — even with a *different* token
        // and target — must yield the existing record (the original
        // token wins per F-S05). That's the contract /migration/start
        // depends on for retried calls.
        $result = $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_B, self::TARGET_B, self::DEVICE, 2000,
        );
        self::assertFalse($result['created']);
        $row = $result['record'];
        self::assertSame(self::TARGET_A, $row['target_relay_url']);
        self::assertSame(self::TOKEN_A, $row['token_hash']);
        self::assertSame(1000, (int)$row['started_at']);
    }

    public function test_getIntent_returns_null_when_absent(): void
    {
        self::assertNull($this->repo->getIntent(self::VAULT_ID));
    }

    // ---------------------------------------------------------------- markVerified

    public function test_markVerified_sets_timestamp_first_time(): void
    {
        $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        $hit = $this->repo->markVerified(self::VAULT_ID, 1500);
        self::assertTrue($hit);
        $row = $this->repo->getIntent(self::VAULT_ID);
        self::assertSame(1500, (int)$row['verified_at']);
    }

    public function test_markVerified_repeat_preserves_original_timestamp(): void
    {
        $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        $this->repo->markVerified(self::VAULT_ID, 1500);
        // Re-marking is idempotent — the COALESCE in the SQL preserves
        // the original timestamp so a retried /verify keeps returning
        // the same value. F-S05 contract.
        $this->repo->markVerified(self::VAULT_ID, 9999);
        $row = $this->repo->getIntent(self::VAULT_ID);
        self::assertSame(1500, (int)$row['verified_at']);
    }

    // ---------------------------------------------------------------- markCommitted

    public function test_markCommitted_sets_timestamp_first_time(): void
    {
        $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        $hit = $this->repo->markCommitted(self::VAULT_ID, 1700);
        self::assertTrue($hit);
        $row = $this->repo->getIntent(self::VAULT_ID);
        self::assertSame(1700, (int)$row['committed_at']);
    }

    public function test_markCommitted_repeat_preserves_original_timestamp(): void
    {
        $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        $this->repo->markCommitted(self::VAULT_ID, 1700);
        $this->repo->markCommitted(self::VAULT_ID, 8888);
        $row = $this->repo->getIntent(self::VAULT_ID);
        self::assertSame(1700, (int)$row['committed_at']);
    }

    public function test_verified_and_committed_are_independent(): void
    {
        $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        $this->repo->markVerified(self::VAULT_ID, 1500);
        // Committed lands later; verified stays put.
        $this->repo->markCommitted(self::VAULT_ID, 2500);
        $row = $this->repo->getIntent(self::VAULT_ID);
        self::assertSame(1500, (int)$row['verified_at']);
        self::assertSame(2500, (int)$row['committed_at']);
    }

    // ---------------------------------------------------------------- cancelIntent

    public function test_cancelIntent_removes_row(): void
    {
        $this->repo->recordIntent(
            self::VAULT_ID, self::TOKEN_A, self::TARGET_A, self::DEVICE, 1000,
        );
        $hit = $this->repo->cancelIntent(self::VAULT_ID);
        self::assertTrue($hit);
        self::assertNull($this->repo->getIntent(self::VAULT_ID));
    }

    public function test_cancelIntent_returns_false_when_absent(): void
    {
        self::assertFalse($this->repo->cancelIntent(self::VAULT_ID));
    }
}
