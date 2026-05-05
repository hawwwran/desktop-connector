<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * F-T05 — unit tests for ``VaultGcJobsRepository``.
 *
 * Pin: row shape on insert, target_chunk_ids JSON round-trip,
 * markCompleted / markCancelled state-transition gates (filtering on
 * ``state IN (planned, executing)``), and the idempotent no-op when
 * the job is already in a terminal state.
 */
final class VaultGcJobsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultGcJobsRepository $repo;

    private const VAULT_ID = 'H9K7M4Q2Z8TD';
    private const DEVICE   = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const JOB_A    = 'jb_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
    private const JOB_B    = 'jb_v1_bbbbbbbbbbbbbbbbbbbbbbbb';

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_gc_test_');
        $this->db = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->repo = new VaultGcJobsRepository($this->db);

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

    private function createJob(string $jobId, array $targets = [], string $kind = 'sync_plan'): void
    {
        $this->repo->create(
            $jobId, self::VAULT_ID, $kind, $targets,
            null, 9999, self::DEVICE, 1000,
        );
    }

    public function test_create_writes_row_in_planned_state(): void
    {
        $this->createJob(self::JOB_A, ['ch_v1_a', 'ch_v1_b']);
        $row = $this->repo->getById(self::JOB_A);
        self::assertNotNull($row);
        self::assertSame('planned', $row['state']);
        self::assertSame('sync_plan', $row['kind']);
        self::assertSame(['ch_v1_a', 'ch_v1_b'], $row['target_chunk_ids']);
        self::assertSame(1000, (int)$row['created_at']);
        self::assertNull($row['completed_at']);
        self::assertNull($row['cancelled_at']);
    }

    public function test_create_with_empty_targets_round_trips(): void
    {
        $this->createJob(self::JOB_A, []);
        $row = $this->repo->getById(self::JOB_A);
        self::assertSame([], $row['target_chunk_ids']);
    }

    public function test_getById_returns_null_for_unknown_job(): void
    {
        self::assertNull($this->repo->getById('jb_v1_unknown'));
    }

    // ---------------------------------------------------------------- markCompleted

    public function test_markCompleted_flips_state_and_records_counts(): void
    {
        $this->createJob(self::JOB_A);
        $hit = $this->repo->markCompleted(self::JOB_A, 7, 12345, 1500);
        self::assertTrue($hit);
        $row = $this->repo->getById(self::JOB_A);
        self::assertSame('completed', $row['state']);
        self::assertSame(1500, (int)$row['completed_at']);
        self::assertSame(7, (int)$row['deleted_count']);
        self::assertSame(12345, (int)$row['freed_bytes']);
    }

    public function test_markCompleted_repeat_is_no_op(): void
    {
        $this->createJob(self::JOB_A);
        $this->repo->markCompleted(self::JOB_A, 7, 100, 1500);
        // Already completed — second call must NOT clobber the
        // recorded counts.
        $hit = $this->repo->markCompleted(self::JOB_A, 99, 999, 9999);
        self::assertFalse($hit);
        $row = $this->repo->getById(self::JOB_A);
        self::assertSame(1500, (int)$row['completed_at']);
        self::assertSame(7, (int)$row['deleted_count']);
        self::assertSame(100, (int)$row['freed_bytes']);
    }

    public function test_markCompleted_no_op_after_cancel(): void
    {
        $this->createJob(self::JOB_A);
        $this->repo->markCancelled(self::JOB_A, 1500);
        // Cancelled → terminal. Marking completed must NOT win.
        $hit = $this->repo->markCompleted(self::JOB_A, 99, 999, 1700);
        self::assertFalse($hit);
        $row = $this->repo->getById(self::JOB_A);
        self::assertSame('cancelled', $row['state']);
    }

    // ---------------------------------------------------------------- markCancelled

    public function test_markCancelled_flips_state_and_stamps_time(): void
    {
        $this->createJob(self::JOB_A);
        $hit = $this->repo->markCancelled(self::JOB_A, 1500);
        self::assertTrue($hit);
        $row = $this->repo->getById(self::JOB_A);
        self::assertSame('cancelled', $row['state']);
        self::assertSame(1500, (int)$row['cancelled_at']);
    }

    public function test_markCancelled_repeat_is_no_op(): void
    {
        $this->createJob(self::JOB_A);
        $this->repo->markCancelled(self::JOB_A, 1500);
        $hit = $this->repo->markCancelled(self::JOB_A, 9999);
        self::assertFalse($hit);
        $row = $this->repo->getById(self::JOB_A);
        self::assertSame(1500, (int)$row['cancelled_at']);
    }

    public function test_markCancelled_no_op_after_completed(): void
    {
        $this->createJob(self::JOB_A);
        $this->repo->markCompleted(self::JOB_A, 1, 1, 1500);
        $hit = $this->repo->markCancelled(self::JOB_A, 1700);
        self::assertFalse($hit);
        $row = $this->repo->getById(self::JOB_A);
        self::assertSame('completed', $row['state']);
    }

    // ---------------------------------------------------------------- isolation

    public function test_independent_jobs_do_not_interfere(): void
    {
        $this->createJob(self::JOB_A);
        $this->createJob(self::JOB_B);
        $this->repo->markCompleted(self::JOB_A, 1, 1, 1500);
        $row_b = $this->repo->getById(self::JOB_B);
        self::assertSame('planned', $row_b['state']);
    }
}
