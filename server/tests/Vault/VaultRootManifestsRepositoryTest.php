<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for VaultRootManifestsRepository (Phase B manifest
 * sharding). Same temp-file SQLite pattern as VaultsRepositoryTest.
 * Each test seeds a fresh genesis vault + root revision 1, then
 * exercises the immutable-revision chain and the CAS publish path.
 *
 * Acceptance criterion: concurrent CAS test — exactly one writer
 * wins, the loser receives the §A1-root conflict payload.
 */
final class VaultRootManifestsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultsRepository $vaultsRepo;
    private VaultRootManifestsRepository $rootRepo;

    private const VAULT_ID         = 'H9K7M4Q2Z8TD';
    private const TOKEN_HASH       = "\xaa\xbb\xcc\xdd";
    private const ENC_HEADER       = "\xde\xad\xbe\xef";
    private const HEADER_HASH      = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const GENESIS_HASH     = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const GENESIS_CIPHERTEXT = "\x01\x02\x03\x04genesis";
    private const GENESIS_SIZE     = 11;
    private const AUTHOR           = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
    private const NOW              = 1714680000;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_root_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->vaultsRepo = new VaultsRepository($this->db);
        $this->rootRepo   = new VaultRootManifestsRepository($this->db);

        // Bring vault to a "post-create" state: vault row exists with
        // current_root_revision = 1, and the genesis root row is
        // written. Mirrors VaultController::create.
        $this->vaultsRepo->create(
            self::VAULT_ID,
            self::TOKEN_HASH,
            self::ENC_HEADER,
            self::HEADER_HASH,
            self::GENESIS_HASH,
            self::NOW
        );
        $this->rootRepo->create(
            self::VAULT_ID,
            1,
            0,
            self::GENESIS_HASH,
            self::GENESIS_CIPHERTEXT,
            self::GENESIS_SIZE,
            self::AUTHOR,
            self::NOW
        );
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    public function test_create_genesis_writes_revision_1(): void
    {
        $row = $this->rootRepo->getByRevision(self::VAULT_ID, 1);
        self::assertNotNull($row);
        self::assertSame(1, (int)$row['root_revision']);
        self::assertSame(0, (int)$row['parent_root_revision']);
        self::assertSame(self::GENESIS_HASH, $row['root_hash']);
        self::assertSame(self::GENESIS_CIPHERTEXT, $row['root_ciphertext']);
        self::assertSame(self::GENESIS_SIZE, (int)$row['root_size']);
        self::assertSame(self::AUTHOR, $row['author_device_id']);
    }

    public function test_create_rejects_duplicate_revision(): void
    {
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessageMatches('/UNIQUE constraint failed/');
        $this->rootRepo->create(
            self::VAULT_ID,
            1,
            0,
            'duplicate'.str_repeat('a', 56),
            "\xff",
            1,
            self::AUTHOR,
            self::NOW
        );
    }

    public function test_getCurrent_returns_head(): void
    {
        $current = $this->rootRepo->getCurrent(self::VAULT_ID);
        self::assertNotNull($current);
        self::assertSame(1, (int)$current['root_revision']);
        self::assertSame(self::GENESIS_HASH, $current['root_hash']);
        self::assertSame(self::GENESIS_CIPHERTEXT, $current['root_ciphertext']);
    }

    public function test_getCurrent_returns_null_for_unknown_vault(): void
    {
        self::assertNull($this->rootRepo->getCurrent('UNKNOWNVAULT'));
    }

    public function test_getByRevision_returns_null_for_unknown_revision(): void
    {
        self::assertNull($this->rootRepo->getByRevision(self::VAULT_ID, 999));
        self::assertNull($this->rootRepo->getByRevision('UNKNOWNVAULT', 1));
    }

    public function test_tryCAS_advances_head_and_inserts_revision(): void
    {
        $newHash = str_repeat('b', 64);
        $newCiphertext = "\x05\x06\x07\x08root-2";
        $newSize = 11;

        $conflict = $this->rootRepo->tryCAS(
            self::VAULT_ID,
            1,
            2,
            $newHash,
            $newCiphertext,
            $newSize,
            self::AUTHOR,
            self::NOW + 10
        );

        self::assertNull($conflict, 'tryCAS should succeed and return null');

        $current = $this->rootRepo->getCurrent(self::VAULT_ID);
        self::assertSame(2, (int)$current['root_revision']);
        self::assertSame($newHash, $current['root_hash']);
        self::assertSame($newCiphertext, $current['root_ciphertext']);

        $genesis = $this->rootRepo->getByRevision(self::VAULT_ID, 1);
        self::assertSame(self::GENESIS_HASH, $genesis['root_hash']);

        $vault = $this->vaultsRepo->getById(self::VAULT_ID);
        self::assertSame(2, (int)$vault['current_root_revision']);
        self::assertSame($newHash, $vault['current_root_hash']);
    }

    public function test_tryCAS_returns_a1_root_payload_on_conflict(): void
    {
        $newHash = str_repeat('c', 64);
        $newCiphertext = "\xaa\xbbA";
        $first = $this->rootRepo->tryCAS(
            self::VAULT_ID, 1, 2, $newHash, $newCiphertext, 3, self::AUTHOR, self::NOW + 1
        );
        self::assertNull($first);

        $conflict = $this->rootRepo->tryCAS(
            self::VAULT_ID,
            1,
            2,
            str_repeat('d', 64),
            "\xcc\xddB",
            3,
            self::AUTHOR,
            self::NOW + 2
        );

        self::assertNotNull($conflict);
        self::assertSame(2, $conflict['current_root_revision']);
        self::assertSame($newHash, $conflict['current_root_hash']);
        self::assertSame($newCiphertext, $conflict['current_root_ciphertext']);
        self::assertSame(3, $conflict['current_root_size']);
    }

    public function test_tryCAS_concurrent_writers_one_wins_one_gets_a1_root(): void
    {
        $writerAHash = str_repeat('1', 64);
        $writerACiphertext = 'WRITER-A';
        $writerBHash = str_repeat('2', 64);
        $writerBCiphertext = 'WRITER-B';

        $resA = $this->rootRepo->tryCAS(
            self::VAULT_ID, 1, 2, $writerAHash, $writerACiphertext, 8, self::AUTHOR, self::NOW + 1
        );
        $resB = $this->rootRepo->tryCAS(
            self::VAULT_ID, 1, 2, $writerBHash, $writerBCiphertext, 8, self::AUTHOR, self::NOW + 2
        );

        $winners = array_filter([$resA, $resB], fn($r) => $r === null);
        $losers  = array_filter([$resA, $resB], fn($r) => $r !== null);
        self::assertCount(1, $winners);
        self::assertCount(1, $losers);

        $loser = array_values($losers)[0];
        self::assertSame(2, $loser['current_root_revision']);
        self::assertSame($writerAHash, $loser['current_root_hash']);
        self::assertSame($writerACiphertext, $loser['current_root_ciphertext']);

        $head = $this->rootRepo->getByRevision(self::VAULT_ID, 2);
        self::assertSame($writerAHash, $head['root_hash']);
    }

    public function test_tryCAS_rolls_back_on_failure_no_orphan_root(): void
    {
        $loserHash = str_repeat('e', 64);
        $first = $this->rootRepo->tryCAS(
            self::VAULT_ID, 1, 2, str_repeat('f', 64), 'WIN', 3, self::AUTHOR, self::NOW + 1
        );
        self::assertNull($first);

        $conflict = $this->rootRepo->tryCAS(
            self::VAULT_ID, 1, 2, $loserHash, 'LOSE', 4, self::AUTHOR, self::NOW + 2
        );
        self::assertNotNull($conflict);

        $rows = $this->db->queryAll(
            'SELECT root_hash FROM vault_root_manifests WHERE vault_id = :id ORDER BY root_revision',
            [':id' => self::VAULT_ID]
        );
        self::assertCount(2, $rows);
        $hashes = array_map(fn($r) => $r['root_hash'], $rows);
        self::assertNotContains($loserHash, $hashes);
    }

    public function test_tryCAS_chains_multiple_revisions(): void
    {
        for ($i = 1; $i <= 3; $i++) {
            $expected = $i;
            $newRev   = $i + 1;
            $hash     = str_repeat(dechex($i + 9), 64);
            $cipher   = "rev-$newRev";
            $res = $this->rootRepo->tryCAS(
                self::VAULT_ID, $expected, $newRev, $hash, $cipher, strlen($cipher),
                self::AUTHOR, self::NOW + $i
            );
            self::assertNull($res);
        }

        $head = $this->rootRepo->getCurrent(self::VAULT_ID);
        self::assertSame(4, (int)$head['root_revision']);
        self::assertSame(3, (int)$head['parent_root_revision']);

        $rev = (int)$head['root_revision'];
        while ($rev > 0) {
            $row = $this->rootRepo->getByRevision(self::VAULT_ID, $rev);
            self::assertNotNull($row);
            $rev = (int)$row['parent_root_revision'];
        }
    }
}
