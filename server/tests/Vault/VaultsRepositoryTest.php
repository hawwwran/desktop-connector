<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for VaultsRepository (T1.2). One temp-file SQLite database per
 * test, fully migrated, then exercised through the repo. The aim is byte-shape
 * coverage — what columns exist, default values, CAS semantics, idempotency —
 * not service-level behavior, which lives in TransferService-style tests later.
 */
final class VaultsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultsRepository $repo;

    private const VAULT_ID    = 'H9K7M4Q2Z8TD';                   // 12 base32, formats §3.1
    private const TOKEN_HASH  = "\xaa\xbb\xcc\xdd";                // raw bytes; 4-byte stub fine for tests
    private const ENC_HEADER  = "\xde\xad\xbe\xef";                // raw envelope bytes
    private const HEADER_HASH = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const MFST_HASH   = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const NOW         = 1714680000;                        // 2024-05-02T19:20:00Z, fixed for determinism

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_repo_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->repo   = new VaultsRepository($this->db);
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    private function seedVault(): void
    {
        $this->repo->create(
            self::VAULT_ID,
            self::TOKEN_HASH,
            self::ENC_HEADER,
            self::HEADER_HASH,
            self::MFST_HASH,
            self::NOW
        );
    }

    public function test_create_writes_row_with_expected_defaults(): void
    {
        $this->seedVault();

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertNotNull($row);
        self::assertSame(self::VAULT_ID, $row['vault_id']);
        self::assertSame(1, (int)$row['header_revision']);
        self::assertSame(1, (int)$row['current_manifest_revision']);
        self::assertSame(0, (int)$row['used_ciphertext_bytes']);
        self::assertSame(0, (int)$row['chunk_count']);
        self::assertSame(1073741824, (int)$row['quota_ciphertext_bytes']);   // D2: 1 GB default
        self::assertNull($row['migrated_to']);
        self::assertNull($row['migrated_at']);
        self::assertSame(self::NOW, (int)$row['created_at']);
        self::assertSame(self::NOW, (int)$row['updated_at']);
    }

    public function test_getById_returns_null_for_unknown_vault(): void
    {
        self::assertNull($this->repo->getById('UNKNOWNVAULT'));
    }

    public function test_listForDashboard_exposes_vaults_ordered_by_latest_update(): void
    {
        $this->seedVault();
        $olderVault = 'AAAAAAAAAAAA';
        $this->repo->create(
            $olderVault,
            self::TOKEN_HASH,
            self::ENC_HEADER,
            self::HEADER_HASH,
            self::MFST_HASH,
            self::NOW + 5
        );
        $this->repo->incUsedBytes(self::VAULT_ID, 2048, 2, self::NOW + 30);

        $rows = $this->repo->listForDashboard();

        self::assertCount(2, $rows);
        self::assertSame(self::VAULT_ID, $rows[0]['vault_id']);
        self::assertSame(2, (int)$rows[0]['chunk_count']);
        self::assertSame(2048, (int)$rows[0]['used_ciphertext_bytes']);
        self::assertSame(self::NOW + 30, (int)$rows[0]['updated_at']);
        self::assertSame($olderVault, $rows[1]['vault_id']);
    }

    public function test_getHeaderCiphertext_exposes_the_quota_pair(): void
    {
        $this->seedVault();

        $header = $this->repo->getHeaderCiphertext(self::VAULT_ID);
        self::assertNotNull($header);
        self::assertSame(self::ENC_HEADER, $header['encrypted_header']);
        self::assertSame(self::HEADER_HASH, $header['header_hash']);
        self::assertSame(1, (int)$header['header_revision']);
        self::assertSame(1073741824, (int)$header['quota_ciphertext_bytes']);
        self::assertSame(0, (int)$header['used_ciphertext_bytes']);
        self::assertNull($header['migrated_to']);
    }

    public function test_setHeaderCiphertext_succeeds_at_expected_revision(): void
    {
        $this->seedVault();

        $newEnc = "\x01\x02\x03\x04";
        $newHash = str_repeat('a', 64);
        $ok = $this->repo->setHeaderCiphertext(self::VAULT_ID, $newEnc, $newHash, 1, self::NOW + 10);

        self::assertTrue($ok);

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertSame($newEnc, $row['encrypted_header']);
        self::assertSame($newHash, $row['header_hash']);
        self::assertSame(2, (int)$row['header_revision']);                    // bumped by CAS
        self::assertSame(self::NOW + 10, (int)$row['updated_at']);
    }

    public function test_setHeaderCiphertext_fails_on_revision_mismatch(): void
    {
        $this->seedVault();

        $ok = $this->repo->setHeaderCiphertext(self::VAULT_ID, "\xff", str_repeat('b', 64), 99, self::NOW + 10);
        self::assertFalse($ok);

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertSame(self::ENC_HEADER, $row['encrypted_header']);          // unchanged
        self::assertSame(1, (int)$row['header_revision']);                     // unchanged
    }

    public function test_setHeaderCiphertext_concurrent_writes_only_one_winner(): void
    {
        $this->seedVault();

        // Two callers both think the current revision is 1. Only one CAS lands.
        $a = $this->repo->setHeaderCiphertext(self::VAULT_ID, "A", str_repeat('1', 64), 1, self::NOW + 1);
        $b = $this->repo->setHeaderCiphertext(self::VAULT_ID, "B", str_repeat('2', 64), 1, self::NOW + 2);

        self::assertTrue($a);
        self::assertFalse($b);
    }

    public function test_incUsedBytes_adjusts_counters_atomically(): void
    {
        $this->seedVault();

        $this->repo->incUsedBytes(self::VAULT_ID, 2_097_168, 1, self::NOW + 5);
        $this->repo->incUsedBytes(self::VAULT_ID, 1_048_576, 1, self::NOW + 6);

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertSame(2_097_168 + 1_048_576, (int)$row['used_ciphertext_bytes']);
        self::assertSame(2, (int)$row['chunk_count']);
        self::assertSame(self::NOW + 6, (int)$row['updated_at']);
    }

    public function test_incUsedBytes_accepts_negative_delta_for_gc(): void
    {
        $this->seedVault();
        $this->repo->incUsedBytes(self::VAULT_ID, 5_000_000, 3, self::NOW);
        $this->repo->incUsedBytes(self::VAULT_ID, -2_000_000, -1, self::NOW + 1);

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertSame(3_000_000, (int)$row['used_ciphertext_bytes']);
        self::assertSame(2, (int)$row['chunk_count']);
    }

    public function test_getQuotaRemaining_returns_default_for_fresh_vault(): void
    {
        $this->seedVault();
        self::assertSame(1073741824, $this->repo->getQuotaRemaining(self::VAULT_ID));
    }

    public function test_getQuotaRemaining_decreases_with_usage(): void
    {
        $this->seedVault();
        $this->repo->incUsedBytes(self::VAULT_ID, 100_000_000, 50, self::NOW);
        self::assertSame(1073741824 - 100_000_000, $this->repo->getQuotaRemaining(self::VAULT_ID));
    }

    public function test_getQuotaRemaining_is_null_for_unknown_vault(): void
    {
        self::assertNull($this->repo->getQuotaRemaining('UNKNOWNVAULT'));
    }

    public function test_markMigratedTo_makes_vault_read_only_per_h2(): void
    {
        $this->seedVault();
        self::assertFalse($this->repo->isReadOnly(self::VAULT_ID));            // not migrated yet

        $ok = $this->repo->markMigratedTo(self::VAULT_ID, 'https://new.example.com', self::NOW + 100);
        self::assertTrue($ok);

        // Acceptance criterion: after markMigratedTo, the source is read-only.
        self::assertTrue($this->repo->isReadOnly(self::VAULT_ID));

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertSame('https://new.example.com', $row['migrated_to']);
        self::assertSame(self::NOW + 100, (int)$row['migrated_at']);
    }

    public function test_markMigratedTo_idempotent_for_same_target(): void
    {
        $this->seedVault();
        self::assertTrue($this->repo->markMigratedTo(self::VAULT_ID, 'https://new.example.com', self::NOW + 1));
        self::assertTrue($this->repo->markMigratedTo(self::VAULT_ID, 'https://new.example.com', self::NOW + 2));

        // Second call refreshed updated_at; migrated_at is the original commit time per H2.
        // (Repo doesn't enforce; but the controller should preserve the original committed_at.)
        $row = $this->repo->getById(self::VAULT_ID);
        self::assertSame('https://new.example.com', $row['migrated_to']);
    }

    public function test_markMigratedTo_rejects_different_target_when_already_migrated(): void
    {
        $this->seedVault();
        self::assertTrue($this->repo->markMigratedTo(self::VAULT_ID, 'https://target-A.example.com', self::NOW + 1));
        // The H2 controller surfaces this as 409 vault_migration_in_progress.
        self::assertFalse($this->repo->markMigratedTo(self::VAULT_ID, 'https://target-B.example.com', self::NOW + 2));

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertSame('https://target-A.example.com', $row['migrated_to']);
    }

    public function test_cancelMigration_clears_migrated_to(): void
    {
        $this->seedVault();
        $this->repo->markMigratedTo(self::VAULT_ID, 'https://new.example.com', self::NOW + 1);
        self::assertTrue($this->repo->isReadOnly(self::VAULT_ID));

        $cancelled = $this->repo->cancelMigration(self::VAULT_ID, self::NOW + 2);
        self::assertTrue($cancelled);
        self::assertFalse($this->repo->isReadOnly(self::VAULT_ID));

        $row = $this->repo->getById(self::VAULT_ID);
        self::assertNull($row['migrated_to']);
        self::assertNull($row['migrated_at']);
    }

    public function test_cancelMigration_returns_false_when_not_migrated(): void
    {
        $this->seedVault();
        self::assertFalse($this->repo->cancelMigration(self::VAULT_ID, self::NOW + 1));
    }

    public function test_isReadOnly_false_for_unknown_vault(): void
    {
        self::assertFalse($this->repo->isReadOnly('UNKNOWNVAULT'));
    }
}
