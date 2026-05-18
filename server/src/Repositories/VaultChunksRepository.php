<?php

/**
 * Owns SQL touching the `vault_chunks` table. File I/O against
 * `server/storage/vaults/<vault_id>/<prefix>/<chunk_id>` (T0 §D13)
 * stays with the calling service — this repository handles the row
 * metadata only, mirroring the existing ChunkRepository pattern.
 *
 * Two pieces of policy live here:
 *
 *   - Chunk-id format gate (T0 §A19). Both the wire-layer validator
 *     and this repo enforce `^ch_v1_[a-z2-7]{24}$`. Defense-in-depth:
 *     a malformed id never reaches storage even if a future caller
 *     forgets to validate.
 *
 *   - Idempotent PUT (vault-v1.md §6.8). Same `chunk_id` + same hash
 *     + same size: `put()` returns `'already_exists'` and the caller
 *     skips the disk write (the existing blob is byte-identical).
 *     Same `chunk_id` + different hash/size: `put()` throws the
 *     matching VaultChunkConflict exception so the controller can
 *     map to 422 `vault_chunk_size_mismatch` or `vault_chunk_tampered`.
 */
class VaultChunksRepository
{
    public const STATE_ACTIVE      = 'active';
    public const STATE_RETAINED    = 'retained';
    public const STATE_GC_PENDING  = 'gc_pending';
    public const STATE_PURGED      = 'purged';

    private const VALID_STATES = [
        self::STATE_ACTIVE,
        self::STATE_RETAINED,
        self::STATE_GC_PENDING,
        self::STATE_PURGED,
    ];

    private const CHUNK_ID_REGEX = '/^ch_v1_[a-z2-7]{24}$/';

    public function __construct(private Database $db) {}

    /** Strict chunk-id format gate per T0 §A19. */
    public static function isValidChunkId(string $chunkId): bool
    {
        return preg_match(self::CHUNK_ID_REGEX, $chunkId) === 1;
    }

    /**
     * Per D13 the on-disk path is
     * `server/storage/vaults/<vault_id>/<prefix>/<chunk_id>` where
     * `<prefix>` is the first two characters of the chunk-id's random
     * portion (the 24 chars after the literal `ch_v1_`). 32^2 = 1024
     * shards per vault — keeps any single directory reasonably small
     * even at multi-million-chunk vault sizes.
     */
    public static function storagePath(string $vaultId, string $chunkId): string
    {
        // F-S19 defense in depth: every caller currently normalizes
        // vaultId, but a future caller forgetting would let `..` slip
        // through and traverse out of the storage root.
        if (preg_match('/^[A-Z2-7]{12}$/', $vaultId) !== 1) {
            throw new VaultChunkInvalidIdException(
                "vault_id '{$vaultId}' fails ^[A-Z2-7]{12}\$"
            );
        }
        if (!self::isValidChunkId($chunkId)) {
            throw new VaultChunkInvalidIdException(
                "chunk_id '{$chunkId}' fails ^ch_v1_[a-z2-7]{24}\$"
            );
        }
        $prefix = substr($chunkId, 6, 2);   // skip 'ch_v1_'
        return "vaults/{$vaultId}/{$prefix}/{$chunkId}";
    }

    /**
     * Idempotent insert. Returns:
     *   - `'created'`         — new row inserted; caller writes the on-disk blob.
     *   - `'already_exists'`  — same id + same hash + same size already stored;
     *                           caller skips the disk write (blob is byte-identical).
     *
     * Throws:
     *   - VaultChunkSizeMismatchException — same id, different `ciphertext_size`.
     *   - VaultChunkTamperedException     — same id + same size, different `chunk_hash`.
     *   - VaultChunkInvalidIdException    — chunk_id fails A19 regex.
     *
     * Does NOT update `vaults.used_ciphertext_bytes` — that's the service's
     * concern via VaultsRepository::incUsedBytes(). The two writes happen
     * inside the controller's transaction so quota accounting stays
     * consistent across crash points.
     */
    public function put(
        string $vaultId,
        string $chunkId,
        string $chunkHash,
        int $ciphertextSize,
        string $storagePath,
        int $now
    ): string {
        if (!self::isValidChunkId($chunkId)) {
            throw new VaultChunkInvalidIdException(
                "chunk_id '{$chunkId}' fails ^ch_v1_[a-z2-7]{24}\$"
            );
        }

        $existing = $this->head($vaultId, $chunkId);
        if ($existing !== null) {
            if ((int)$existing['ciphertext_size'] !== $ciphertextSize) {
                throw new VaultChunkSizeMismatchException(
                    sprintf(
                        'chunk_id %s already stored with size %d; refusing %d',
                        $chunkId,
                        (int)$existing['ciphertext_size'],
                        $ciphertextSize
                    )
                );
            }
            if ((string)$existing['chunk_hash'] !== $chunkHash) {
                throw new VaultChunkTamperedException(
                    "chunk_id {$chunkId} already stored with a different hash"
                );
            }
            $existingState = (string)$existing['state'];
            // F-S01 fix (review §1.C1): purged rows have lost their on-disk
            // blob (gcExecute unlinked it); gc_pending rows are scheduled to
            // lose it. Reviving means flipping back to active and signalling
            // the controller to perform the disk write so the manifest
            // reference doesn't dangle. Returning 'already_exists' here
            // would 200 the caller with no blob on disk → silent corruption.
            if (
                $existingState === self::STATE_PURGED
                || $existingState === self::STATE_GC_PENDING
            ) {
                $this->db->execute(
                    'UPDATE vault_chunks
                     SET state = :state,
                         storage_path = :path,
                         last_referenced_at = :now
                     WHERE vault_id = :vid AND chunk_id = :cid',
                    [
                        ':state' => self::STATE_ACTIVE,
                        ':path'  => $storagePath,
                        ':now'   => $now,
                        ':vid'   => $vaultId,
                        ':cid'   => $chunkId,
                    ]
                );
                return 'created';
            }
            // Byte-identical re-upload of an active/retained row. Blob
            // is still on disk; bump last_referenced_at so the GC sweep
            // doesn't evict it as cold.
            $this->db->execute(
                'UPDATE vault_chunks
                 SET last_referenced_at = :now
                 WHERE vault_id = :vid AND chunk_id = :cid',
                [':now' => $now, ':vid' => $vaultId, ':cid' => $chunkId]
            );
            return 'already_exists';
        }

        $this->db->execute(
            'INSERT INTO vault_chunks (
                vault_id,
                chunk_id,
                ciphertext_size,
                chunk_hash,
                storage_path,
                state,
                created_at,
                last_referenced_at
             ) VALUES (
                :vid, :cid, :size, :hash, :path, :state, :now, :now
             )',
            [
                ':vid'   => $vaultId,
                ':cid'   => $chunkId,
                ':size'  => $ciphertextSize,
                ':hash'  => $chunkHash,
                ':path'  => $storagePath,
                ':state' => self::STATE_ACTIVE,
                ':now'   => $now,
            ]
        );
        return 'created';
    }

    /**
     * Full-row read for chunk download. Includes `storage_path` so the
     * service layer can locate the on-disk blob without recomputing.
     */
    public function get(string $vaultId, string $chunkId): ?array
    {
        return $this->db->querySingle(
            'SELECT vault_id, chunk_id, ciphertext_size, chunk_hash, storage_path,
                    state, created_at, last_referenced_at
             FROM vault_chunks
             WHERE vault_id = :vid AND chunk_id = :cid',
            [':vid' => $vaultId, ':cid' => $chunkId]
        );
    }

    /**
     * Metadata-only read for HEAD requests. Same row shape as get() minus
     * `storage_path` (which is internal — clients don't need it).
     */
    public function head(string $vaultId, string $chunkId): ?array
    {
        return $this->db->querySingle(
            'SELECT vault_id, chunk_id, ciphertext_size, chunk_hash,
                    state, created_at, last_referenced_at
             FROM vault_chunks
             WHERE vault_id = :vid AND chunk_id = :cid',
            [':vid' => $vaultId, ':cid' => $chunkId]
        );
    }

    /**
     * Bulk presence check for `POST /chunks/batch-head`. Returns a map
     * keyed by chunk_id; missing entries map to null. Caller is
     * responsible for the §10 size cap (1024 ids per request).
     *
     * @param string[] $chunkIds
     * @return array<string, ?array{ciphertext_size: int, chunk_hash: string, state: string}>
     */
    public function batchHead(string $vaultId, array $chunkIds): array
    {
        $result = array_fill_keys($chunkIds, null);
        if (empty($chunkIds)) {
            return $result;
        }

        // Validate every id up-front. Caller is expected to have
        // pre-filtered, but defense-in-depth.
        foreach ($chunkIds as $cid) {
            if (!self::isValidChunkId($cid)) {
                throw new VaultChunkInvalidIdException(
                    "batch contains invalid chunk_id '{$cid}'"
                );
            }
        }

        // SQLite parameterized IN-list. We build named placeholders so
        // the prepared statement still binds typed values.
        $placeholders = [];
        $params = [':vid' => $vaultId];
        foreach (array_values($chunkIds) as $i => $cid) {
            $key = ":c{$i}";
            $placeholders[] = $key;
            $params[$key] = $cid;
        }
        $sql = 'SELECT chunk_id, ciphertext_size, chunk_hash, state
                FROM vault_chunks
                WHERE vault_id = :vid
                  AND chunk_id IN (' . implode(',', $placeholders) . ')';

        foreach ($this->db->queryAll($sql, $params) as $row) {
            $result[$row['chunk_id']] = [
                'ciphertext_size' => (int)$row['ciphertext_size'],
                'chunk_hash'      => (string)$row['chunk_hash'],
                'state'           => (string)$row['state'],
            ];
        }
        return $result;
    }

    /**
     * Move a chunk between lifecycle states. Used by GC plan/execute
     * (active → gc_pending → purged) and by tombstone-retention promotion
     * (active → retained when the only references are tombstones whose
     * `recoverable_until` hasn't elapsed).
     *
     * Throws RuntimeException for unknown states — callers should pass
     * one of the STATE_* constants.
     */
    public function setState(string $vaultId, string $chunkId, string $newState): void
    {
        if (!in_array($newState, self::VALID_STATES, true)) {
            throw new RuntimeException("invalid chunk state: {$newState}");
        }
        $this->db->execute(
            'UPDATE vault_chunks
             SET state = :state
             WHERE vault_id = :vid AND chunk_id = :cid',
            [':state' => $newState, ':vid' => $vaultId, ':cid' => $chunkId]
        );
    }

    /**
     * Delete the chunk row outright. Used when a chunk PUT's disk write
     * fails after row insert — the row+counters reset together, so the
     * next upload can succeed (F-S02).
     */
    public function deleteRow(string $vaultId, string $chunkId): void
    {
        $this->db->execute(
            'DELETE FROM vault_chunks
             WHERE vault_id = :vid AND chunk_id = :cid',
            [':vid' => $vaultId, ':cid' => $chunkId]
        );
    }

    /**
     * Enumerate every chunk row still in ``purged`` state for ``$vaultId``.
     * Used by the residual-unlink reaper invoked at the start of
     * ``gcExecute`` (review §1.C2): a gc/execute that crashed or hit
     * EBUSY/EIO between the state flip and the unlink left orphan blobs
     * on disk with no further retry path; this enumerator gives the
     * reaper the storage_paths it needs.
     *
     * @return list<array{chunk_id: string, storage_path: string}>
     */
    public function listPurged(string $vaultId, int $limit = 1024): array
    {
        $rows = $this->db->queryAll(
            'SELECT chunk_id, storage_path
             FROM vault_chunks
             WHERE vault_id = :vid AND state = :state
             LIMIT :lim',
            [
                ':vid'   => $vaultId,
                ':state' => self::STATE_PURGED,
                ':lim'   => $limit,
            ]
        );
        $out = [];
        foreach ($rows as $row) {
            $out[] = [
                'chunk_id'     => (string)$row['chunk_id'],
                'storage_path' => (string)$row['storage_path'],
            ];
        }
        return $out;
    }

    /**
     * §4.M1 — enumerate every user-visible chunk_id for ``$vaultId``,
     * sorted ascending, with cursor pagination.
     *
     * Used by the desktop's orphan-chunk reaper: it lists all server-
     * side chunks, subtracts the set referenced by the live manifest,
     * and DELETEs the diff via the existing admin-gated gc/execute
     * path. Only ``active`` + ``retained`` states are user-visible —
     * ``gc_pending`` / ``purged`` rows are mid-flight server state
     * the client mustn't reason about.
     *
     * Cursor convention: ``$cursor`` is the last chunk_id returned by
     * the previous page (exclusive lower bound). Pass an empty string
     * for the first page. Caller stops when fewer than ``$limit``
     * rows come back.
     *
     * @return list<string>
     */
    public function listIds(
        string $vaultId, string $cursor = '', int $limit = 1024,
    ): array {
        if ($limit < 1 || $limit > 1024) {
            throw new \InvalidArgumentException(
                "limit must be in [1, 1024]; got {$limit}"
            );
        }
        $rows = $this->db->queryAll(
            'SELECT chunk_id
             FROM vault_chunks
             WHERE vault_id = :vid
               AND state IN (:active, :retained)
               AND chunk_id > :cursor
             ORDER BY chunk_id ASC
             LIMIT :lim',
            [
                ':vid'      => $vaultId,
                ':active'   => self::STATE_ACTIVE,
                ':retained' => self::STATE_RETAINED,
                ':cursor'   => $cursor,
                ':lim'      => $limit,
            ]
        );
        $out = [];
        foreach ($rows as $row) {
            $out[] = (string)$row['chunk_id'];
        }
        return $out;
    }

    /**
     * Conditional row delete used by the §1.C2 residual reaper. Returns
     * true iff a row in state ``purged`` was deleted. The state guard
     * defends against the §1.C1 revival race: between the reaper reading
     * a purged row and the reaper deciding the unlink succeeded, a
     * concurrent ``putChunk`` may have flipped the same row back to
     * ``active``. An unconditional delete in that window would erase a
     * legitimately-revived row's metadata while its blob is on disk.
     */
    public function deleteIfPurged(string $vaultId, string $chunkId): bool
    {
        $this->db->execute(
            'DELETE FROM vault_chunks
             WHERE vault_id = :vid AND chunk_id = :cid AND state = :state',
            [
                ':vid'   => $vaultId,
                ':cid'   => $chunkId,
                ':state' => self::STATE_PURGED,
            ]
        );
        return $this->db->changes() === 1;
    }
}

/**
 * Domain exceptions for chunk-write conflicts. Controllers catch these
 * and map to T0 vault_v1 error codes:
 *
 *   - VaultChunkInvalidIdException     → 400 vault_invalid_request (field=chunk_id)
 *   - VaultChunkSizeMismatchException  → 422 vault_chunk_size_mismatch
 *   - VaultChunkTamperedException      → 422 vault_chunk_tampered
 */
class VaultChunkInvalidIdException extends RuntimeException {}
class VaultChunkSizeMismatchException extends RuntimeException {}
class VaultChunkTamperedException extends RuntimeException {}
