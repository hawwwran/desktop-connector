<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Repo-level tests for VaultAuthAttemptsRepository (review §1.H1).
 *
 * The repo's contract:
 *   - recordAndRead increments the per-(device, scope, kind) counter
 *     atomically and returns the post-write state.
 *   - The window resets after ``window_seconds`` has elapsed since
 *     ``window_start``.
 *   - Distinct (device, scope, kind) tuples have independent
 *     counters.
 */
final class VaultAuthAttemptsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultAuthAttemptsRepository $repo;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_attempts_test_');
        $this->db = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->repo = new VaultAuthAttemptsRepository($this->db);
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    public function test_first_attempt_returns_attempts_one(): void
    {
        $state = $this->repo->recordAndRead(
            'dev-a', 'vault-a',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_000,
        );
        self::assertSame(1, $state['attempts']);
        self::assertSame(1_000_000, $state['window_start']);
        self::assertSame(1_000_060, $state['window_end']);
    }

    public function test_subsequent_attempts_in_window_increment(): void
    {
        for ($i = 0; $i < 3; $i++) {
            $state = $this->repo->recordAndRead(
                'dev-a', 'vault-a',
                VaultAuthAttemptsRepository::KIND_AUTH,
                60, 1_000_000 + $i,
            );
        }
        self::assertSame(3, $state['attempts']);
        // Window stays anchored at the first attempt's clock.
        self::assertSame(1_000_000, $state['window_start']);
    }

    public function test_attempt_after_window_resets_to_one(): void
    {
        $this->repo->recordAndRead(
            'dev-a', 'vault-a',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_000,
        );
        // Jump past the window.
        $state = $this->repo->recordAndRead(
            'dev-a', 'vault-a',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_100,
        );
        self::assertSame(1, $state['attempts']);
        self::assertSame(1_000_100, $state['window_start']);
    }

    public function test_counter_is_per_device(): void
    {
        $this->repo->recordAndRead(
            'dev-a', 'vault-a',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_000,
        );
        $state = $this->repo->recordAndRead(
            'dev-b', 'vault-a',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_001,
        );
        self::assertSame(1, $state['attempts']);
    }

    public function test_counter_is_per_scope(): void
    {
        $this->repo->recordAndRead(
            'dev-a', 'vault-a',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_000,
        );
        $state = $this->repo->recordAndRead(
            'dev-a', 'vault-b',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_001,
        );
        self::assertSame(1, $state['attempts']);
    }

    public function test_counter_is_per_kind(): void
    {
        $this->repo->recordAndRead(
            'dev-a', 'vault-a',
            VaultAuthAttemptsRepository::KIND_AUTH,
            60, 1_000_000,
        );
        $state = $this->repo->recordAndRead(
            'dev-a', '',
            VaultAuthAttemptsRepository::KIND_CREATE,
            3600, 1_000_001,
        );
        // Different kind → fresh counter.
        self::assertSame(1, $state['attempts']);
    }

    public function test_rejects_unknown_kind(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->repo->recordAndRead(
            'dev-a', 'vault-a', 'banana', 60, 1_000_000,
        );
    }
}
