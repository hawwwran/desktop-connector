<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for VaultFolderShardsRepository (Phase B manifest
 * sharding). Each test seeds a fresh vault + root, then exercises
 * per-folder shard CAS (``tryCAS``) and the atomic shard-with-root
 * publish path (``tryAtomicShardWithRootCAS``).
 *
 * Acceptance criteria:
 *   - Shard CAS is per-folder: a conflict in folder A doesn't affect
 *     folder B's CAS chain.
 *   - Atomic shard-with-root: either both writes land or neither.
 *   - 409 payload shape distinguishes shard-only / root-only /
 *     shard_root conflicts so the controller can emit the right
 *     error code.
 */
final class VaultFolderShardsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultsRepository $vaultsRepo;
    private VaultRootManifestsRepository $rootRepo;
    private VaultFolderShardsRepository $shardsRepo;

    private const VAULT_ID    = 'H9K7M4Q2Z8TD';
    private const FOLDER_A    = 'rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
    private const FOLDER_B    = 'rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb';
    private const TOKEN_HASH  = "\xaa\xbb\xcc\xdd";
    private const ENC_HEADER  = "\xde\xad\xbe\xef";
    private const HEADER_HASH = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const ROOT_HASH   = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const AUTHOR      = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
    private const NOW         = 1714680000;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_shards_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->vaultsRepo = new VaultsRepository($this->db);
        $this->rootRepo   = new VaultRootManifestsRepository($this->db);
        $this->shardsRepo = new VaultFolderShardsRepository($this->db);

        $this->vaultsRepo->create(
            self::VAULT_ID,
            self::TOKEN_HASH,
            self::ENC_HEADER,
            self::HEADER_HASH,
            self::ROOT_HASH,
            self::NOW,
        );
        $this->rootRepo->create(
            self::VAULT_ID, 1, 0, self::ROOT_HASH, 'ROOT-1', 6, self::AUTHOR, self::NOW,
        );
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    public function test_tryCAS_genesis_for_new_folder(): void
    {
        $conflict = $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A,
            0,                        // expected (genesis bootstrap)
            1,                        // new
            str_repeat('a', 64),
            'SHARD-A-1',
            9,
            self::AUTHOR,
            self::NOW + 1,
        );
        self::assertNull($conflict);

        $current = $this->shardsRepo->getCurrent(self::VAULT_ID, self::FOLDER_A);
        self::assertNotNull($current);
        self::assertSame(1, (int)$current['shard_revision']);
        self::assertSame('SHARD-A-1', $current['shard_ciphertext']);
    }

    public function test_tryCAS_advances_existing_folder(): void
    {
        $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 0, 1, str_repeat('a', 64), 'GEN', 3, self::AUTHOR, self::NOW + 1,
        );
        $conflict = $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 1, 2, str_repeat('b', 64), 'NXT', 3, self::AUTHOR, self::NOW + 2,
        );
        self::assertNull($conflict);

        $current = $this->shardsRepo->getCurrent(self::VAULT_ID, self::FOLDER_A);
        self::assertSame(2, (int)$current['shard_revision']);
        self::assertSame('NXT', $current['shard_ciphertext']);
    }

    public function test_tryCAS_returns_a1_shard_payload_on_conflict(): void
    {
        $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 0, 1, str_repeat('a', 64), 'GEN', 3, self::AUTHOR, self::NOW + 1,
        );
        $first = $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 1, 2, str_repeat('w', 64), 'WIN', 3, self::AUTHOR, self::NOW + 2,
        );
        self::assertNull($first);

        // Second writer with stale expected — should lose.
        $conflict = $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 1, 2, str_repeat('l', 64), 'LOSE', 4, self::AUTHOR, self::NOW + 3,
        );
        self::assertNotNull($conflict);
        self::assertSame(self::FOLDER_A, $conflict['remote_folder_id']);
        self::assertSame(2, $conflict['current_shard_revision']);
        self::assertSame(str_repeat('w', 64), $conflict['current_shard_hash']);
        self::assertSame('WIN', $conflict['current_shard_ciphertext']);
    }

    public function test_tryCAS_per_folder_isolation(): void
    {
        // Acceptance criterion: a conflict on folder A doesn't lock
        // folder B's CAS chain.
        $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 0, 1, str_repeat('a', 64), 'A-GEN', 5, self::AUTHOR, self::NOW + 1,
        );

        // Two writers on different folders should both succeed.
        $resA = $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 1, 2, str_repeat('a', 64), 'A-2', 3, self::AUTHOR, self::NOW + 2,
        );
        $resB = $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_B, 0, 1, str_repeat('b', 64), 'B-GEN', 5, self::AUTHOR, self::NOW + 3,
        );
        self::assertNull($resA);
        self::assertNull($resB);

        self::assertSame(2, (int)$this->shardsRepo->getCurrent(self::VAULT_ID, self::FOLDER_A)['shard_revision']);
        self::assertSame(1, (int)$this->shardsRepo->getCurrent(self::VAULT_ID, self::FOLDER_B)['shard_revision']);
    }

    public function test_atomic_shard_with_root_both_fresh_commits_both(): void
    {
        $res = $this->shardsRepo->tryAtomicShardWithRootCAS(
            vaultId: self::VAULT_ID,
            remoteFolderId: self::FOLDER_A,
            expectedCurrentShardRevision: 0,
            newShardRevision: 1,
            shardHash: str_repeat('a', 64),
            shardCiphertext: 'SHARD-A-1',
            shardSize: 9,
            expectedCurrentRootRevision: 1,
            newRootRevision: 2,
            rootHash: str_repeat('r', 64),
            rootCiphertext: 'ROOT-2',
            rootSize: 6,
            authorDeviceId: self::AUTHOR,
            now: self::NOW + 1,
            rootRepo: $this->rootRepo,
        );
        self::assertNull($res);

        // Both committed.
        self::assertSame(1, (int)$this->shardsRepo->getCurrent(self::VAULT_ID, self::FOLDER_A)['shard_revision']);
        self::assertSame(2, (int)$this->rootRepo->getCurrent(self::VAULT_ID)['root_revision']);
    }

    public function test_atomic_shard_stale_returns_shard_conflict_only(): void
    {
        // Advance shard alone first.
        $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 0, 1, str_repeat('x', 64), 'X', 1, self::AUTHOR, self::NOW + 1,
        );
        // Atomic publish thinks shard is at 0 (stale).
        $res = $this->shardsRepo->tryAtomicShardWithRootCAS(
            self::VAULT_ID, self::FOLDER_A,
            0, 1, str_repeat('a', 64), 'A1', 2,
            1, 2, str_repeat('r', 64), 'R2', 2,
            self::AUTHOR, self::NOW + 2, $this->rootRepo,
        );
        self::assertNotNull($res);
        self::assertSame('shard', $res['kind']);
        self::assertSame(1, $res['shard']['current_shard_revision']);

        // Root revision unchanged.
        self::assertSame(1, (int)$this->rootRepo->getCurrent(self::VAULT_ID)['root_revision']);
    }

    public function test_atomic_root_stale_returns_root_conflict_only(): void
    {
        // Advance root alone first.
        $this->rootRepo->tryCAS(
            self::VAULT_ID, 1, 2, str_repeat('z', 64), 'Z', 1, self::AUTHOR, self::NOW + 1,
        );
        // Atomic publish thinks root is at 1 (stale).
        $res = $this->shardsRepo->tryAtomicShardWithRootCAS(
            self::VAULT_ID, self::FOLDER_A,
            0, 1, str_repeat('a', 64), 'A1', 2,
            1, 2, str_repeat('r', 64), 'R2', 2,
            self::AUTHOR, self::NOW + 2, $this->rootRepo,
        );
        self::assertNotNull($res);
        self::assertSame('root', $res['kind']);
        self::assertSame(2, $res['root']['current_root_revision']);

        // Shard unchanged (no head row yet).
        self::assertNull($this->shardsRepo->getCurrent(self::VAULT_ID, self::FOLDER_A));
    }

    public function test_atomic_both_stale_returns_shard_root_conflict(): void
    {
        // Advance both independently.
        $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 0, 1, str_repeat('s', 64), 'S', 1, self::AUTHOR, self::NOW + 1,
        );
        $this->rootRepo->tryCAS(
            self::VAULT_ID, 1, 2, str_repeat('r', 64), 'R', 1, self::AUTHOR, self::NOW + 2,
        );

        // Atomic publish with both stale expectations.
        $res = $this->shardsRepo->tryAtomicShardWithRootCAS(
            self::VAULT_ID, self::FOLDER_A,
            0, 1, str_repeat('a', 64), 'A1', 2,
            1, 2, str_repeat('z', 64), 'Z2', 2,
            self::AUTHOR, self::NOW + 3, $this->rootRepo,
        );
        self::assertNotNull($res);
        self::assertSame('shard_root', $res['kind']);
        self::assertSame(1, $res['shard']['current_shard_revision']);
        self::assertSame(2, $res['root']['current_root_revision']);
    }

    public function test_atomic_failure_rolls_back_both_no_orphan_rows(): void
    {
        // Pre-advance shard so the atomic call's shard expectation is stale.
        $this->shardsRepo->tryCAS(
            self::VAULT_ID, self::FOLDER_A, 0, 1, str_repeat('x', 64), 'X', 1, self::AUTHOR, self::NOW + 1,
        );

        $res = $this->shardsRepo->tryAtomicShardWithRootCAS(
            self::VAULT_ID, self::FOLDER_A,
            0, 1, str_repeat('a', 64), 'A1', 2,
            1, 2, str_repeat('r', 64), 'R2', 2,
            self::AUTHOR, self::NOW + 2, $this->rootRepo,
        );
        self::assertNotNull($res);

        // No new root revision row got inserted (root_revision = 1 only).
        $rootRows = $this->db->queryAll(
            'SELECT root_revision FROM vault_root_manifests WHERE vault_id = :id ORDER BY root_revision',
            [':id' => self::VAULT_ID],
        );
        self::assertCount(1, $rootRows);
        self::assertSame(1, (int)$rootRows[0]['root_revision']);
    }
}
