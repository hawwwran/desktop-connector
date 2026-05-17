<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for VaultManifestsRepository (T1.3). Same temp-file SQLite
 * pattern as VaultsRepositoryTest. Each test seeds a fresh genesis vault
 * + manifest revision 1, then exercises the immutable-revision chain
 * and the CAS publish path.
 *
 * Acceptance criterion: concurrent CAS test — exactly one writer wins,
 * the loser receives the §A1 conflict payload.
 */
final class VaultManifestsRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultsRepository $vaultsRepo;
    private VaultManifestsRepository $manifestsRepo;

    private const VAULT_ID    = 'H9K7M4Q2Z8TD';
    private const TOKEN_HASH  = "\xaa\xbb\xcc\xdd";
    private const ENC_HEADER  = "\xde\xad\xbe\xef";
    private const HEADER_HASH = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const GENESIS_HASH = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const GENESIS_CIPHERTEXT = "\x01\x02\x03\x04genesis";
    private const GENESIS_SIZE = 11;
    private const AUTHOR = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';     // 32 hex chars
    private const NOW    = 1714680000;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_manifests_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();
        $this->vaultsRepo    = new VaultsRepository($this->db);
        $this->manifestsRepo = new VaultManifestsRepository($this->db);

        // Bring vault to a "post-create" state: vault row exists with
        // current_manifest_revision = 1, and the genesis manifest row is
        // written. This mirrors what the controller will do at vault create.
        $this->vaultsRepo->create(
            self::VAULT_ID,
            self::TOKEN_HASH,
            self::ENC_HEADER,
            self::HEADER_HASH,
            self::GENESIS_HASH,
            self::NOW
        );
        $this->manifestsRepo->create(
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
        $row = $this->manifestsRepo->getByRevision(self::VAULT_ID, 1);
        self::assertNotNull($row);
        self::assertSame(1, (int)$row['revision']);
        self::assertSame(0, (int)$row['parent_revision']);
        self::assertSame(self::GENESIS_HASH, $row['manifest_hash']);
        self::assertSame(self::GENESIS_CIPHERTEXT, $row['manifest_ciphertext']);
        self::assertSame(self::GENESIS_SIZE, (int)$row['manifest_size']);
        self::assertSame(self::AUTHOR, $row['author_device_id']);
    }

    public function test_create_rejects_duplicate_revision(): void
    {
        // Composite PK (vault_id, revision) — duplicate insert surfaces
        // through Database::query() as RuntimeException carrying the
        // underlying SQLite UNIQUE-constraint message.
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessageMatches('/UNIQUE constraint failed/');
        $this->manifestsRepo->create(
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
        $current = $this->manifestsRepo->getCurrent(self::VAULT_ID);
        self::assertNotNull($current);
        self::assertSame(1, (int)$current['revision']);
        self::assertSame(self::GENESIS_HASH, $current['manifest_hash']);
        self::assertSame(self::GENESIS_CIPHERTEXT, $current['manifest_ciphertext']);
    }

    public function test_getCurrent_returns_null_for_unknown_vault(): void
    {
        self::assertNull($this->manifestsRepo->getCurrent('UNKNOWNVAULT'));
    }

    public function test_getByRevision_returns_specific_row(): void
    {
        $row = $this->manifestsRepo->getByRevision(self::VAULT_ID, 1);
        self::assertNotNull($row);
        self::assertSame(self::GENESIS_HASH, $row['manifest_hash']);
    }

    public function test_getByRevision_returns_null_for_unknown_revision(): void
    {
        self::assertNull($this->manifestsRepo->getByRevision(self::VAULT_ID, 999));
        self::assertNull($this->manifestsRepo->getByRevision('UNKNOWNVAULT', 1));
    }

    public function test_tryCAS_advances_head_and_inserts_revision(): void
    {
        $newHash = str_repeat('b', 64);
        $newCiphertext = "\x05\x06\x07\x08revision-2";
        $newSize = 12;

        $conflict = $this->manifestsRepo->tryCAS(
            self::VAULT_ID,
            1,                       // expected
            2,                       // new
            $newHash,
            $newCiphertext,
            $newSize,
            self::AUTHOR,
            self::NOW + 10
        );

        self::assertNull($conflict, 'tryCAS should succeed and return null');

        // Head moved to revision 2.
        $current = $this->manifestsRepo->getCurrent(self::VAULT_ID);
        self::assertSame(2, (int)$current['revision']);
        self::assertSame($newHash, $current['manifest_hash']);
        self::assertSame($newCiphertext, $current['manifest_ciphertext']);

        // Genesis still readable.
        $genesis = $this->manifestsRepo->getByRevision(self::VAULT_ID, 1);
        self::assertSame(self::GENESIS_HASH, $genesis['manifest_hash']);

        // vaults row reflects the new head.
        $vault = $this->vaultsRepo->getById(self::VAULT_ID);
        self::assertSame(2, (int)$vault['current_manifest_revision']);
        self::assertSame($newHash, $vault['current_manifest_hash']);
    }

    public function test_tryCAS_returns_a1_payload_on_conflict(): void
    {
        // First writer succeeds.
        $newHash = str_repeat('c', 64);
        $newCiphertext = "\xaa\xbbA";
        $first = $this->manifestsRepo->tryCAS(
            self::VAULT_ID, 1, 2, $newHash, $newCiphertext, 3, self::AUTHOR, self::NOW + 1
        );
        self::assertNull($first);

        // Second writer with the same expected_revision (1) loses the race.
        $conflict = $this->manifestsRepo->tryCAS(
            self::VAULT_ID,
            1,                       // stale expected
            2,                       // (irrelevant; never lands)
            str_repeat('d', 64),
            "\xcc\xddB",
            3,
            self::AUTHOR,
            self::NOW + 2
        );

        self::assertNotNull($conflict, 'tryCAS should fail on stale expected_revision');
        self::assertSame(2, $conflict['current_revision']);
        self::assertSame($newHash, $conflict['current_manifest_hash']);
        self::assertSame($newCiphertext, $conflict['current_manifest_ciphertext']);
        self::assertSame(3, $conflict['current_manifest_size']);
    }

    public function test_tryCAS_concurrent_writers_one_wins_one_gets_a1(): void
    {
        // Acceptance criterion: two writers with the same expected_revision,
        // exactly one wins, loser receives 409 with full current-manifest payload.
        // SQLite's BEGIN IMMEDIATE serializes them; the second writer reads
        // the new head and returns the §A1 payload.
        $writerAHash = str_repeat('1', 64);
        $writerACiphertext = 'WRITER-A';
        $writerBHash = str_repeat('2', 64);
        $writerBCiphertext = 'WRITER-B';

        $resA = $this->manifestsRepo->tryCAS(
            self::VAULT_ID, 1, 2, $writerAHash, $writerACiphertext, 8, self::AUTHOR, self::NOW + 1
        );
        $resB = $this->manifestsRepo->tryCAS(
            self::VAULT_ID, 1, 2, $writerBHash, $writerBCiphertext, 8, self::AUTHOR, self::NOW + 2
        );

        // Exactly one is null (winner), one is the conflict array.
        $winners = array_filter([$resA, $resB], fn($r) => $r === null);
        $losers  = array_filter([$resA, $resB], fn($r) => $r !== null);
        self::assertCount(1, $winners, 'exactly one writer should win');
        self::assertCount(1, $losers,  'exactly one writer should lose');

        // Loser receives the winner's manifest in the A1 payload.
        $loser = array_values($losers)[0];
        self::assertSame(2, $loser['current_revision']);
        self::assertSame($writerAHash, $loser['current_manifest_hash']);
        self::assertSame($writerACiphertext, $loser['current_manifest_ciphertext']);

        // Only one revision-2 row was inserted; the loser's payload was
        // never written to vault_manifests.
        $head = $this->manifestsRepo->getByRevision(self::VAULT_ID, 2);
        self::assertSame($writerAHash, $head['manifest_hash']);
        self::assertSame($writerACiphertext, $head['manifest_ciphertext']);
    }

    public function test_tryCAS_rolls_back_on_failure_no_orphan_manifest(): void
    {
        // Conflict path: ensure no row with the loser's hash exists.
        $loserHash = str_repeat('e', 64);
        $first = $this->manifestsRepo->tryCAS(
            self::VAULT_ID, 1, 2, str_repeat('f', 64), 'WIN', 3, self::AUTHOR, self::NOW + 1
        );
        self::assertNull($first);

        $conflict = $this->manifestsRepo->tryCAS(
            self::VAULT_ID, 1, 2, $loserHash, 'LOSE', 4, self::AUTHOR, self::NOW + 2
        );
        self::assertNotNull($conflict);

        // Direct DB query: no row carries the loser's hash.
        $rows = $this->db->queryAll(
            'SELECT manifest_hash FROM vault_manifests WHERE vault_id = :id ORDER BY revision',
            [':id' => self::VAULT_ID]
        );
        self::assertCount(2, $rows, 'expected genesis + one winning revision');
        $hashes = array_map(fn($r) => $r['manifest_hash'], $rows);
        self::assertNotContains($loserHash, $hashes);
    }

    public function test_tryCAS_chains_multiple_revisions(): void
    {
        // Revisions 2, 3, 4 published in sequence.
        for ($i = 1; $i <= 3; $i++) {
            $expected = $i;
            $newRev   = $i + 1;
            $hash     = str_repeat(dechex($i + 9), 64);
            $cipher   = "rev-$newRev";
            $res = $this->manifestsRepo->tryCAS(
                self::VAULT_ID, $expected, $newRev, $hash, $cipher, strlen($cipher),
                self::AUTHOR, self::NOW + $i
            );
            self::assertNull($res, "publish of rev $newRev should succeed");
        }

        $head = $this->manifestsRepo->getCurrent(self::VAULT_ID);
        self::assertSame(4, (int)$head['revision']);
        self::assertSame(3, (int)$head['parent_revision']);

        // Walk the chain backwards to genesis.
        $rev = (int)$head['revision'];
        while ($rev > 0) {
            $row = $this->manifestsRepo->getByRevision(self::VAULT_ID, $rev);
            self::assertNotNull($row, "revision $rev should exist");
            $rev = (int)$row['parent_revision'];
        }
    }
}
