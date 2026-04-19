<?php

/**
 * Core transfer orchestration: init, upload, pending list, download, ack.
 *
 * Business-level failures throw ApiError subclasses (NotFoundError,
 * ForbiddenError, …); the Router catches these and serializes uniformly.
 * Success paths return plain data — downloadChunk returns raw bytes so
 * the controller can emit them via Router::binary.
 *
 * Input-shape and path-safety validation happens at the HTTP boundary
 * (Validators::requireSafeTransferId, requireNonEmptyString, requireInt)
 * before calling these methods; the service assumes its inputs are shaped.
 *
 * Side effects route through sibling services: upload completion fires
 * TransferWakeService::wake; ack removes chunk storage via
 * TransferCleanupService::deleteChunkFilesAndRows.
 */
class TransferService
{
    private const MAX_PENDING_BYTES = 500 * 1024 * 1024;   // 500 MB per recipient
    private const MAX_CHUNK_COUNT = 500;

    public static function init(
        Database $db,
        string $senderId,
        string $transferId,
        string $recipientId,
        string $encryptedMeta,
        int $chunkCount,
    ): array {
        if ($chunkCount < 1 || $chunkCount > self::MAX_CHUNK_COUNT) {
            throw new ValidationError('Invalid chunk_count');
        }

        $usage = $db->querySingle(
            'SELECT COALESCE(SUM(c.blob_size), 0) as total_bytes
             FROM chunks c
             JOIN transfers t ON c.transfer_id = t.id
             WHERE t.recipient_id = :rid AND t.downloaded = 0',
            [':rid' => $recipientId]
        );
        if ($usage && $usage['total_bytes'] >= self::MAX_PENDING_BYTES) {
            throw new StorageLimitError('Recipient storage limit exceeded');
        }

        $existing = $db->querySingle(
            'SELECT id FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if ($existing) {
            throw new ConflictError('Transfer ID already exists');
        }

        $db->execute(
            'INSERT INTO transfers (id, sender_id, recipient_id, encrypted_meta, chunk_count, created_at)
             VALUES (:id, :sender, :recipient, :meta, :chunks, :now)',
            [
                ':id' => $transferId,
                ':sender' => $senderId,
                ':recipient' => $recipientId,
                ':meta' => $encryptedMeta,
                ':chunks' => $chunkCount,
                ':now' => time(),
            ]
        );

        return ['transfer_id' => $transferId, 'status' => 'awaiting_chunks'];
    }

    public static function uploadChunk(
        Database $db,
        string $deviceId,
        string $transferId,
        int $chunkIndex,
        string $blobData,
    ): array {
        $transfer = $db->querySingle(
            'SELECT id, sender_id, chunk_count, chunks_received, complete
             FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if (!$transfer) {
            throw new NotFoundError('Transfer not found');
        }
        if ($transfer['sender_id'] !== $deviceId) {
            throw new ForbiddenError('Not the sender of this transfer');
        }
        if ($chunkIndex < 0 || $chunkIndex >= $transfer['chunk_count']) {
            throw new ValidationError('Invalid chunk_index');
        }
        if ($blobData === '') {
            throw new ValidationError('Empty chunk data');
        }

        $storageDir = __DIR__ . '/../../storage/' . $transferId;
        if (!is_dir($storageDir)) {
            mkdir($storageDir, 0700, true);
        }
        // Atomic write: temp file + rename so a concurrent downloader cannot
        // observe a partially-written chunk (AES-GCM would fail auth on short bytes).
        $blobPath = $transferId . '/' . $chunkIndex . '.bin';
        $fullPath = __DIR__ . '/../../storage/' . $blobPath;
        $tmpPath = $fullPath . '.tmp';
        file_put_contents($tmpPath, $blobData);
        rename($tmpPath, $fullPath);

        $existingChunk = $db->querySingle(
            'SELECT chunk_index FROM chunks WHERE transfer_id = :tid AND chunk_index = :idx',
            [':tid' => $transferId, ':idx' => $chunkIndex]
        );

        if (!$existingChunk) {
            $db->execute(
                'INSERT INTO chunks (transfer_id, chunk_index, blob_path, blob_size, created_at)
                 VALUES (:tid, :idx, :path, :size, :now)',
                [
                    ':tid' => $transferId,
                    ':idx' => $chunkIndex,
                    ':path' => $blobPath,
                    ':size' => strlen($blobData),
                    ':now' => time(),
                ]
            );

            $db->execute(
                'UPDATE transfers SET chunks_received = chunks_received + 1 WHERE id = :id',
                [':id' => $transferId]
            );
        }

        $updated = $db->querySingle(
            'SELECT chunks_received, chunk_count FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        $complete = $updated['chunks_received'] >= $updated['chunk_count'];

        if ($complete) {
            $db->execute('UPDATE transfers SET complete = 1 WHERE id = :id', [':id' => $transferId]);
            TransferWakeService::wake($db, $transferId);
        }

        return [
            'chunks_received' => (int)$updated['chunks_received'],
            'complete' => $complete,
        ];
    }

    public static function listPending(Database $db, string $deviceId): array
    {
        return $db->queryAll(
            'SELECT id as transfer_id, sender_id, encrypted_meta, chunk_count, created_at
             FROM transfers
             WHERE recipient_id = :rid AND complete = 1 AND downloaded = 0
             ORDER BY created_at ASC',
            [':rid' => $deviceId]
        );
    }

    /** Returns the chunk's raw bytes. Controller emits them via Router::binary. */
    public static function downloadChunk(Database $db, string $deviceId, string $transferId, int $chunkIndex): string
    {
        $transfer = $db->querySingle(
            'SELECT recipient_id, chunk_count FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            throw new NotFoundError('Transfer not found or not for you');
        }

        $chunk = $db->querySingle(
            'SELECT blob_path FROM chunks WHERE transfer_id = :tid AND chunk_index = :idx',
            [':tid' => $transferId, ':idx' => $chunkIndex]
        );
        if (!$chunk) {
            throw new NotFoundError('Chunk not found');
        }

        $fullPath = __DIR__ . '/../../storage/' . $chunk['blob_path'];
        if (!file_exists($fullPath)) {
            throw new ApiError(500, 'Chunk file missing from storage');
        }

        // Track download progress, capped at chunk_count - 1 until ack.
        // chunks_downloaded == chunk_count iff downloaded == 1 — gives the sender's
        // delivery tracker a rock-solid "done" signal that can't be faked by serving
        // the last chunk (which might still fail client-side before ack).
        $cap = (int)$transfer['chunk_count'] - 1;
        $newProgress = min($chunkIndex + 1, max(0, $cap));
        $db->execute(
            'UPDATE transfers SET chunks_downloaded = MAX(chunks_downloaded, :progress) WHERE id = :id',
            [':progress' => $newProgress, ':id' => $transferId]
        );

        return file_get_contents($fullPath);
    }

    public static function ack(Database $db, string $deviceId, string $transferId): array
    {
        $transfer = $db->querySingle(
            'SELECT id, sender_id, recipient_id FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            throw new NotFoundError('Transfer not found');
        }

        // Pairing-stats SUM must run BEFORE chunk deletion (chunks table still holds sizes here).
        $senderId = $transfer['sender_id'];
        $totalBytes = $db->querySingle(
            'SELECT COALESCE(SUM(blob_size), 0) as total FROM chunks WHERE transfer_id = :tid',
            [':tid' => $transferId]
        );

        $ids = [$senderId, $deviceId];
        sort($ids);
        (new PairingRepository($db))->incrementPairingStats($ids[0], $ids[1], (int)$totalBytes['total']);

        TransferCleanupService::deleteChunkFilesAndRows($db, $transferId);

        // chunks_downloaded reaches chunk_count only here (on ack), not during serving.
        $db->execute(
            'UPDATE transfers SET downloaded = 1, delivered_at = :now, chunks_downloaded = chunk_count WHERE id = :id',
            [':now' => time(), ':id' => $transferId]
        );

        return ['status' => 'deleted'];
    }
}
