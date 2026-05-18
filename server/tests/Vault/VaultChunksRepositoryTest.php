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

    // ---------------------------------------------------------------- §4.M1 listIds

    public function test_listIds_returns_sorted_user_visible_chunks(): void
    {
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_B, str_repeat('b', 64), 200,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_B), self::NOW);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('a', 64), 100,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A), self::NOW);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_C, str_repeat('c', 64), 300,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_C), self::NOW);

        $ids = $this->chunksRepo->listIds(self::VAULT_ID);
        self::assertSame(
            [self::CHUNK_A, self::CHUNK_B, self::CHUNK_C],
            $ids,
            'listIds must return chunk_ids sorted ascending so the cursor anchor is stable',
        );
    }

    public function test_listIds_excludes_gc_pending_and_purged_states(): void
    {
        // §4.M1: the reaper would otherwise see in-flight server-side
        // state as "still present", scheduling already-half-deleted
        // chunks for a redundant delete.
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('a', 64), 100,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A), self::NOW);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_B, str_repeat('b', 64), 200,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_B), self::NOW);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_C, str_repeat('c', 64), 300,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_C), self::NOW);
        $this->chunksRepo->setState(self::VAULT_ID, self::CHUNK_B, VaultChunksRepository::STATE_GC_PENDING);
        $this->chunksRepo->setState(self::VAULT_ID, self::CHUNK_C, VaultChunksRepository::STATE_PURGED);

        // Only A remains in a user-visible (active/retained) state.
        $ids = $this->chunksRepo->listIds(self::VAULT_ID);
        self::assertSame([self::CHUNK_A], $ids);
    }

    public function test_listIds_paginates_via_cursor(): void
    {
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_A, str_repeat('a', 64), 100,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A), self::NOW);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_B, str_repeat('b', 64), 200,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_B), self::NOW);
        $this->chunksRepo->put(self::VAULT_ID, self::CHUNK_C, str_repeat('c', 64), 300,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_C), self::NOW);

        $page1 = $this->chunksRepo->listIds(self::VAULT_ID, '', 2);
        self::assertSame([self::CHUNK_A, self::CHUNK_B], $page1);

        // Cursor = last id of page 1 → page 2 starts AFTER that.
        $page2 = $this->chunksRepo->listIds(self::VAULT_ID, self::CHUNK_B, 2);
        self::assertSame([self::CHUNK_C], $page2);

        // Cursor past last id → empty page (signals end-of-stream).
        $page3 = $this->chunksRepo->listIds(self::VAULT_ID, self::CHUNK_C, 2);
        self::assertSame([], $page3);
    }

    public function test_listIds_rejects_out_of_range_limit(): void
    {
        $this->expectException(\InvalidArgumentException::class);
        $this->chunksRepo->listIds(self::VAULT_ID, '', 2048);
    }

    public function test_listIds_min_age_seconds_excludes_recent_chunks(): void
    {
        // B2 (post-§4.M1 review): the desktop reaper races with
        // concurrent uploads. ``min_age_seconds`` excludes chunks
        // whose ``created_at`` is within the grace window so a
        // mid-upload chunk (PUT'd but not yet referenced by a
        // published shard) doesn't get misclassified as orphan.
        $oldHash = str_repeat('a', 64);
        $recentHash = str_repeat('b', 64);
        $this->chunksRepo->put(
            self::VAULT_ID, self::CHUNK_A, $oldHash, 100,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_A),
            self::NOW,
        );
        $this->chunksRepo->put(
            self::VAULT_ID, self::CHUNK_B, $recentHash, 200,
            VaultChunksRepository::storagePath(self::VAULT_ID, self::CHUNK_B),
            self::NOW + 30,
        );

        // No filter: both visible.
        $ids = $this->chunksRepo->listIds(
            self::VAULT_ID, '', 1024, 0, self::NOW + 100,
        );
        self::assertSame([self::CHUNK_A, self::CHUNK_B], $ids);

        // 60-second grace at NOW + 100 → CHUNK_B (created at NOW+30,
        // age 70) is visible; CHUNK_A (age 100) too. Both visible.
        $ids = $this->chunksRepo->listIds(
            self::VAULT_ID, '', 1024, 60, self::NOW + 100,
        );
        self::assertSame([self::CHUNK_A, self::CHUNK_B], $ids);

        // 60-second grace at NOW + 60 → CHUNK_B (age 30) below grace;
        // CHUNK_A (age 60, exactly at boundary) visible. Only CHUNK_A.
        $ids = $this->chunksRepo->listIds(
            self::VAULT_ID, '', 1024, 60, self::NOW + 60,
        );
        self::assertSame([self::CHUNK_A], $ids);
    }

    public function test_listIds_rejects_negative_min_age_seconds(): void
    {
        $this->expectException(\InvalidArgumentException::class);
        $this->chunksRepo->listIds(self::VAULT_ID, '', 1024, -1);
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
