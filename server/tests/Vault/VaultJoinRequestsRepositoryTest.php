<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * F-T05 — unit tests for ``VaultJoinRequestsRepository``.
 *
 * Pin the lifecycle gates: ``pending → claimed → approved`` (only the
 * right state transitions land), ``reject`` works from any non-terminal
 * state, ``expirePastDue`` flips state but doesn't delete, ``countPending``
 * returns pending+claimed for the F-S08 rate limit.
 */
final class VaultJoinRequestsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultJoinRequestsRepository $repo;

    private const VAULT_ID = 'H9K7M4Q2Z8TD';
    private const REQ_A    = 'jr_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
    private const REQ_B    = 'jr_v1_bbbbbbbbbbbbbbbbbbbbbbbb';
    private const ADMIN    = 'a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0';
    private const CLAIMANT = 'c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1';

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_jr_test_');
        $this->db = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->repo = new VaultJoinRequestsRepository($this->db);

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

    private function adminPubkey(): string
    {
        // 32 bytes with embedded nulls so we exercise the BLOB binding
        // (F-T05 surfaced this issue in the migration-intents repo).
        return str_repeat("\x00\xaa", 16);
    }

    private function claimantPubkey(): string
    {
        return str_repeat("\xff\x01", 16);
    }

    private function seedPending(string $reqId, int $expiresAt = 9999): void
    {
        $this->repo->create(
            $reqId, self::VAULT_ID, $this->adminPubkey(),
            1000, $expiresAt,
        );
    }

    // ---------------------------------------------------------------- create + get

    public function test_create_writes_pending_row_with_pubkey(): void
    {
        $this->seedPending(self::REQ_A);
        $row = $this->repo->get(self::REQ_A);
        self::assertNotNull($row);
        self::assertSame('pending', $row['state']);
        self::assertSame($this->adminPubkey(), $row['ephemeral_admin_pubkey']);
        self::assertNull($row['claimant_pubkey']);
        self::assertNull($row['claimed_at']);
        self::assertNull($row['approved_at']);
    }

    public function test_get_returns_null_for_unknown(): void
    {
        self::assertNull($this->repo->get('jr_v1_unknown'));
    }

    // ---------------------------------------------------------------- claim

    public function test_claim_flips_pending_to_claimed(): void
    {
        $this->seedPending(self::REQ_A);
        $row = $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'My laptop', 1500,
        );
        self::assertNotNull($row);
        self::assertSame('claimed', $row['state']);
        self::assertSame(self::CLAIMANT, $row['claimant_device_id']);
        self::assertSame($this->claimantPubkey(), $row['claimant_pubkey']);
        self::assertSame('My laptop', $row['device_name']);
        self::assertSame(1500, (int)$row['claimed_at']);
    }

    public function test_claim_returns_null_when_already_claimed(): void
    {
        $this->seedPending(self::REQ_A);
        $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'A', 1500,
        );
        $second = $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'B', 1700,
        );
        self::assertNull($second);
    }

    public function test_claim_returns_null_for_unknown(): void
    {
        $row = $this->repo->claim(
            'jr_v1_unknown', self::CLAIMANT, $this->claimantPubkey(),
            'A', 1500,
        );
        self::assertNull($row);
    }

    // ---------------------------------------------------------------- approve

    public function test_approve_flips_claimed_to_approved(): void
    {
        $this->seedPending(self::REQ_A);
        $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'A', 1500,
        );
        $row = $this->repo->approve(
            self::REQ_A, 'sync', "\x00\xff\x00\xffwrapped",
            self::ADMIN, 1700,
        );
        self::assertNotNull($row);
        self::assertSame('approved', $row['state']);
        self::assertSame('sync', $row['approved_role']);
        self::assertSame("\x00\xff\x00\xffwrapped", $row['wrapped_vault_grant']);
        self::assertSame(self::ADMIN, $row['granted_by_device_id']);
        self::assertSame(1700, (int)$row['approved_at']);
    }

    public function test_approve_refuses_pending_request(): void
    {
        $this->seedPending(self::REQ_A);
        // Skip claim — try to approve directly.
        $row = $this->repo->approve(
            self::REQ_A, 'sync', "wrapped", self::ADMIN, 1700,
        );
        self::assertNull(
            $row,
            "approve must require state=claimed (it cannot skip the claim step)",
        );
    }

    public function test_approve_refuses_already_approved(): void
    {
        $this->seedPending(self::REQ_A);
        $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'A', 1500,
        );
        $this->repo->approve(self::REQ_A, 'sync', "g1", self::ADMIN, 1700);
        $second = $this->repo->approve(
            self::REQ_A, 'admin', "g2", self::ADMIN, 1800,
        );
        self::assertNull($second);
    }

    // ---------------------------------------------------------------- reject

    public function test_reject_works_from_pending(): void
    {
        $this->seedPending(self::REQ_A);
        self::assertTrue($this->repo->reject(self::REQ_A, 1500));
        $row = $this->repo->get(self::REQ_A);
        self::assertSame('rejected', $row['state']);
        self::assertSame(1500, (int)$row['rejected_at']);
    }

    public function test_reject_works_from_claimed(): void
    {
        $this->seedPending(self::REQ_A);
        $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'A', 1500,
        );
        self::assertTrue($this->repo->reject(self::REQ_A, 1700));
        $row = $this->repo->get(self::REQ_A);
        self::assertSame('rejected', $row['state']);
    }

    public function test_reject_no_op_after_approved(): void
    {
        $this->seedPending(self::REQ_A);
        $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'A', 1500,
        );
        $this->repo->approve(self::REQ_A, 'sync', "g", self::ADMIN, 1700);
        self::assertFalse($this->repo->reject(self::REQ_A, 1800));
    }

    // ---------------------------------------------------------------- expirePastDue

    public function test_expirePastDue_flips_pending_rows(): void
    {
        $this->seedPending(self::REQ_A, expiresAt: 1000);
        $this->seedPending(self::REQ_B, expiresAt: 9999);
        $count = $this->repo->expirePastDue(2000);
        self::assertSame(1, $count);
        self::assertSame('expired', $this->repo->get(self::REQ_A)['state']);
        self::assertSame('pending', $this->repo->get(self::REQ_B)['state']);
    }

    public function test_expirePastDue_does_not_revert_terminal_states(): void
    {
        $this->seedPending(self::REQ_A, expiresAt: 1000);
        $this->repo->reject(self::REQ_A, 1500);
        $count = $this->repo->expirePastDue(9999);
        self::assertSame(0, $count);
        self::assertSame('rejected', $this->repo->get(self::REQ_A)['state']);
    }

    // ---------------------------------------------------------------- countPending

    public function test_countPending_includes_pending_and_claimed(): void
    {
        $this->seedPending(self::REQ_A);
        $this->seedPending(self::REQ_B);
        // One of them moves to claimed; both still count toward the
        // F-S08 rate limit.
        $this->repo->claim(
            self::REQ_A, self::CLAIMANT, $this->claimantPubkey(),
            'A', 1500,
        );
        self::assertSame(2, $this->repo->countPending(self::VAULT_ID));
    }

    public function test_countPending_excludes_terminal_states(): void
    {
        $this->seedPending(self::REQ_A);
        $this->seedPending(self::REQ_B);
        $this->repo->reject(self::REQ_A, 1500);
        self::assertSame(1, $this->repo->countPending(self::VAULT_ID));
    }
}
