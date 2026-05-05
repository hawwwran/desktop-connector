<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * F-T05 — unit tests for ``VaultDeviceGrantsRepository``.
 *
 * Pin row-shape + revoke-soft-delete contract: revoked grants stay in
 * the table for audit; ``isActiveGrant`` flips false; ``listForVault``
 * still returns them so the Devices tab can show history.
 */
final class VaultDeviceGrantsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultDeviceGrantsRepository $repo;

    private const VAULT_ID = 'H9K7M4Q2Z8TD';
    private const DEVICE_A = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const DEVICE_B = 'b1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const ADMIN    = 'a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0';
    private const GRANT_A  = 'gr_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
    private const GRANT_B  = 'gr_v1_bbbbbbbbbbbbbbbbbbbbbbbb';

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_grants_test_');
        $this->db = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->repo = new VaultDeviceGrantsRepository($this->db);

        // FK: grants reference vaults(vault_id).
        (new VaultsRepository($this->db))->create(
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

    private function seedGrant(string $grantId, string $deviceId, string $role = 'sync'): void
    {
        $this->repo->insertGrant(
            $grantId, self::VAULT_ID, $deviceId, "Device $deviceId",
            $role, self::ADMIN, 'qr', 1000,
        );
    }

    public function test_insertGrant_writes_row(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $row = $this->repo->getByDevice(self::VAULT_ID, self::DEVICE_A);
        self::assertNotNull($row);
        self::assertSame(self::GRANT_A, $row['grant_id']);
        self::assertSame('sync', $row['role']);
        self::assertNull($row['revoked_at']);
        self::assertSame(1000, (int)$row['granted_at']);
    }

    public function test_listForVault_returns_all_grants_in_grant_order(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $this->repo->insertGrant(
            self::GRANT_B, self::VAULT_ID, self::DEVICE_B, 'B',
            'admin', self::ADMIN, 'qr', 2000,
        );
        $rows = $this->repo->listForVault(self::VAULT_ID);
        self::assertCount(2, $rows);
        self::assertSame(self::GRANT_A, $rows[0]['grant_id']);
        self::assertSame(self::GRANT_B, $rows[1]['grant_id']);
    }

    public function test_isActiveGrant_true_when_not_revoked(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        self::assertTrue($this->repo->isActiveGrant(self::VAULT_ID, self::DEVICE_A));
    }

    public function test_isActiveGrant_false_for_unknown(): void
    {
        self::assertFalse($this->repo->isActiveGrant(self::VAULT_ID, self::DEVICE_A));
    }

    public function test_revoke_marks_revoked_at_and_revoked_by(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $hit = $this->repo->revoke(
            self::VAULT_ID, self::DEVICE_A, self::ADMIN, 1500,
        );
        self::assertTrue($hit);
        $row = $this->repo->getByDevice(self::VAULT_ID, self::DEVICE_A);
        self::assertSame(1500, (int)$row['revoked_at']);
        self::assertSame(self::ADMIN, $row['revoked_by']);
    }

    public function test_revoke_returns_false_on_already_revoked(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $this->repo->revoke(self::VAULT_ID, self::DEVICE_A, self::ADMIN, 1500);
        self::assertFalse(
            $this->repo->revoke(self::VAULT_ID, self::DEVICE_A, self::ADMIN, 9999),
            "double-revoke must report no-change",
        );
    }

    public function test_isActiveGrant_false_after_revoke(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $this->repo->revoke(self::VAULT_ID, self::DEVICE_A, self::ADMIN, 1500);
        self::assertFalse(
            $this->repo->isActiveGrant(self::VAULT_ID, self::DEVICE_A),
        );
        // List still returns the revoked row (audit trail).
        $rows = $this->repo->listForVault(self::VAULT_ID);
        self::assertCount(1, $rows);
        self::assertSame(1500, (int)$rows[0]['revoked_at']);
    }

    public function test_bumpLastSeen_updates_active_grants(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $this->repo->bumpLastSeen(self::VAULT_ID, self::DEVICE_A, 1234);
        $row = $this->repo->getByDevice(self::VAULT_ID, self::DEVICE_A);
        self::assertSame(1234, (int)$row['last_seen_at']);
    }

    public function test_bumpLastSeen_does_not_touch_revoked_grants(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $this->repo->revoke(self::VAULT_ID, self::DEVICE_A, self::ADMIN, 1500);
        $this->repo->bumpLastSeen(self::VAULT_ID, self::DEVICE_A, 9999);
        $row = $this->repo->getByDevice(self::VAULT_ID, self::DEVICE_A);
        self::assertNull(
            $row['last_seen_at'],
            "revoked grants must not have last_seen_at bumped",
        );
    }

    public function test_recordRotation_writes_history_row(): void
    {
        $this->seedGrant(self::GRANT_A, self::DEVICE_A);
        $this->repo->recordRotation(
            self::VAULT_ID, self::ADMIN, 1700, self::GRANT_A,
        );
        $row = $this->db->querySingle(
            'SELECT vault_id, rotated_at, rotated_by,
                    triggered_by_revoke_grant_id
               FROM vault_access_secret_rotations
              WHERE vault_id = :v',
            [':v' => self::VAULT_ID],
        );
        self::assertNotNull($row);
        self::assertSame(1700, (int)$row['rotated_at']);
        self::assertSame(self::ADMIN, $row['rotated_by']);
        self::assertSame(self::GRANT_A, $row['triggered_by_revoke_grant_id']);
    }

    public function test_recordRotation_with_null_trigger(): void
    {
        $this->repo->recordRotation(self::VAULT_ID, self::ADMIN, 1800);
        $row = $this->db->querySingle(
            'SELECT triggered_by_revoke_grant_id
               FROM vault_access_secret_rotations
              WHERE vault_id = :v',
            [':v' => self::VAULT_ID],
        );
        self::assertNull($row['triggered_by_revoke_grant_id']);
    }
}
