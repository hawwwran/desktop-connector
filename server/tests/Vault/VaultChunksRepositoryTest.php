<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for VaultChunksRepository (T1.4). Acceptance criteria:
 * idempotent PUT (same id+ciphertext = 200), conflict PUT (same id +
 * different size/hash = 422 size_mismatch / tampered), regex rejection
 * for non-conforming chunk_ids.
 */
final class VaultChunksRepositoryTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultsRepository $vaultsRepo;
    private VaultChunksRepository $chunksRepo;

    private const VAULT_ID    = 'HAKMQ2ZTDABC';
    private const TOKEN_HASH  = "\xaa\xbb";
    private const ENC_HEADER  = "\xde\xad";
    private const HEADER_HASH = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const MFST_HASH   = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const NOW         = 1714680000;

    private const CHUNK_A = 'ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
    private const CHUNK_B = 'ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb';
    private const CHUNK_C = 'ch_v1_cccccccccccccccccccccccc';

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_chunks_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();

        $this->vaultsRepo = new VaultsRepository($this->db);
        $this->chunksRepo = new VaultChunksRepository($this->db);

        $this->vaultsRepo->create(
            self::VAULT_ID, self::TOKEN_HASH, self::ENC_HEADER,
            self::HEADER_HASH, self::MFST_HASH, self::NOW
        );
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    // ---------------------------------------------------------------- format gate

    public function test_isValidChunkId_accepts_canonical_form(): void
    {
        self::assertTrue(VaultChunksRepository::isValidChunkId(self::CHUNK_A));
        self::assertTrue(VaultChunksRepository::isValidChunkId(self::CHUNK_C));
    }

    public function test_isValidChunkId_rejects_wrong_prefix(): void
    {
        self::assertFalse(VaultChunksRepository::isValidChunkId('ch_v2_aaaaaaaaaaaaaaaaaaaaaaaa'));
        self::assertFalse(VaultChunksRepository::isValidChunkId('cv_v1_aaaaaaaaaaaaaaaaaaaaaaaa'));
        self::assertFalse(VaultChunksRepository::isValidChunkId('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'));
    }

    public function test_isValidChunkId_rejects_wrong_alphabet_or_length(): void
    {
        self::assertFalse(VaultChunksRepository::isValidChunkId('ch_v1_AAAAAAAAAAAAAAAAAAAAAAAA'));   // uppercase
        self::assertFalse(VaultChunksRepository::isValidChunkId('ch_v1_111111111111111111111111'));  // '1' not in base32
        self::assertFalse(VaultChunksRepository::isValidChunkId('ch_v1_aaaa'));                       // too short
        self::assertFalse(VaultChunksRepository::isValidChunkId('ch_v1_aaaaaaaaaaaaaaaaaaaaaaaaaaa')); // too long
    }

    public function test_storagePath_uses_d13_layout(): void
    {
        $path = VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A);
        self::assertSame('vaults/HAKMQ2ZTDABC/aa/ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa', $path);

        $pathB = VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_B);
        self::assertSame('vaults/HAKMQ2ZTDABC/bb/ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb', $pathB);
    }

    public function test_storagePath_throws_for_invalid_id(): void
    {
        $this->expectException(VaultChunkInvalidIdException::class);
        VaultChunksRepository::storagePath(self::VAULT_ID, 'bogus-id');
    }

    // ---------------------------------------------------------------- put / idempotency

    public function test_put_creates_new_chunk_row(): void
    {
        $hash = str_repeat('1', 64);
        $path = VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A);

        $result = $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, $hash, 2_097_168, $path, self::NOW);
        self::assertSame('created', $result);

        $row = $this->chunksRepo->get(self::VAULT_ID, self::CHUNK_A);
        self::assertNotNull($row);
        self::assertSame(self::CHUNK_A, $row['chunk_id']);
        self::assertSame(2_097_168, (int)$row['ciphertext_size']);
        self::assertSame($hash, $row['chunk_hash']);
        self::assertSame($path, $row['storage_path']);
        self::assertSame(VaultChunksRepository::STATE_ACTIVE, $row['state']);
    }

    public function test_put_idempotent_same_hash_and_size(): void
    {
        $hash = str_repeat('2', 64);
        $path = VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A);

        $first = $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, $hash, 1024, $path, self::NOW);
        self::assertSame('created', $first);

        $second = $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, $hash, 1024, $path, self::NOW + 60);
        self::assertSame('already_exists', $second);

        // last_referenced_at bumped on idempotent re-upload.
        $row = $this->chunksRepo->get(self::VAULT_ID, self::CHUNK_A);
        self::assertSame(self::NOW + 60, (int)$row['last_referenced_at']);
    }

    public function test_put_size_mismatch_throws(): void
    {
        $hash = str_repeat('3', 64);
        $path = VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, $hash, 1024, $path, self::NOW);

        $this->expectException(VaultChunkSizeMismatchException::class);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, $hash, 2048, $path, self::NOW + 1);
    }

    public function test_put_hash_mismatch_throws_tampered(): void
    {
        $path = VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('4', 64), 1024, $path, self::NOW);

        $this->expectException(VaultChunkTamperedException::class);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('5', 64), 1024, $path, self::NOW + 1);
    }

    public function test_put_rejects_invalid_chunk_id_format(): void
    {
        $this->expectException(VaultChunkInvalidIdException::class);
        $this->chunksRepo->put(self::VAULT_ID, 'ch_v2_invalid', str_repeat('6', 64), 100, 'whatever', self::NOW);
    }

    // ---------------------------------------------------------------- get / head / batchHead

    public function test_get_returns_full_row(): void
    {
        $hash = str_repeat('a', 64);
        $path = VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, $hash, 100, $path, self::NOW);

        $got = $this->chunksRepo->get(self::VAULT_ID, self::CHUNK_A);
        self::assertNotNull($got);
        self::assertArrayHasKey('storage_path', $got);
        self::assertSame($path, $got['storage_path']);
    }

    public function test_get_returns_null_for_unknown_chunk(): void
    {
        self::assertNull($this->chunksRepo->get(self::VAULT_ID, self::CHUNK_C));
    }

    public function test_head_omits_storage_path(): void
    {
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('a', 64), 100,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A), self::NOW);

        $head = $this->chunksRepo->head(self::VAULT_ID, self::CHUNK_A);
        self::assertNotNull($head);
        self::assertArrayNotHasKey('storage_path', $head);
        self::assertSame(100, (int)$head['ciphertext_size']);
    }

    public function test_batchHead_returns_present_and_missing(): void
    {
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('a', 64), 100,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A), self::NOW);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_C, str_repeat('c', 64), 300,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_C), self::NOW);

        $batch = $this->chunksRepo->batchHead(self::VAULT_ID, [self::CHUNK_A, self::CHUNK_B, self::CHUNK_C]);

        self::assertNotNull($batch[self::CHUNK_A]);
        self::assertSame(100, $batch[self::CHUNK_A]['ciphertext_size']);
        self::assertNull($batch[self::CHUNK_B]);
        self::assertNotNull($batch[self::CHUNK_C]);
        self::assertSame(300, $batch[self::CHUNK_C]['ciphertext_size']);
    }

    public function test_batchHead_empty_input_returns_empty(): void
    {
        self::assertSame([], $this->chunksRepo->batchHead(self::VAULT_ID, []));
    }

    public function test_batchHead_rejects_invalid_id_in_batch(): void
    {
        $this->expectException(VaultChunkInvalidIdException::class);
        $this->chunksRepo->batchHead(self::VAULT_ID, [self::CHUNK_A, 'not-a-chunk']);
    }

    public function test_batchHead_filters_by_vault_id(): void
    {
        // Insert into one vault; query with a different vault id; should not leak.
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('a', 64), 100,
            'vaults/X/aa/' . self::CHUNK_A, self::NOW);

        $batch = $this->chunksRepo->batchHead('OTHERVAULT00', [self::CHUNK_A]);
        self::assertNull($batch[self::CHUNK_A]);
    }

    // ---------------------------------------------------------------- setState

    public function test_setState_transitions_active_to_gc_pending(): void
    {
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('a', 64), 100,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A), self::NOW);

        $this->chunksRepo->setState(self::VAULT_ID, self::CHUNK_A, VaultChunksRepository::STATE_GC_PENDING);
        self::assertSame(
            VaultChunksRepository::STATE_GC_PENDING,
            $this->chunksRepo->get(self::VAULT_ID, self::CHUNK_A)['state']
        );

        $this->chunksRepo->setState(self::VAULT_ID, self::CHUNK_A, VaultChunksRepository::STATE_PURGED);
        self::assertSame(
            VaultChunksRepository::STATE_PURGED,
            $this->chunksRepo->get(self::VAULT_ID, self::CHUNK_A)['state']
        );
    }

    public function test_setState_rejects_unknown_state(): void
    {
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessageMatches('/invalid chunk state/');
        $this->chunksRepo->setState(self::VAULT_ID, self::CHUNK_A, 'wonky');
    }
}
