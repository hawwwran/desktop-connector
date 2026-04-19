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

        if ((new ChunkRepository($db))->sumPendingBytesForRecipient($recipientId) >= self::MAX_PENDING_BYTES) {
            throw new StorageLimitError('Recipient storage limit exceeded');
        }

        $transfers = new TransferRepository($db);
        if ($transfers->existsById($transferId)) {
            throw new ConflictError('Transfer ID already exists');
        }

        $transfers->insertTransfer($transferId, $senderId, $recipientId, $encryptedMeta, $chunkCount, time());
        AppLog::log('Transfer', sprintf(
            'transfer.init.accepted transfer_id=%s sender=%s recipient=%s chunks=%d',
            AppLog::shortId($transferId), AppLog::shortId($senderId), AppLog::shortId($recipientId), $chunkCount
        ));

        return ['transfer_id' => $transferId, 'status' => 'awaiting_chunks'];
    }

    public static function uploadChunk(
        Database $db,
        string $deviceId,
        string $transferId,
        int $chunkIndex,
        string $blobData,
    ): array {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
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

        $chunks = new ChunkRepository($db);
        $storedNewChunk = false;
        if (!$chunks->chunkExists($transferId, $chunkIndex)) {
            $chunks->insertChunk($transferId, $chunkIndex, $blobPath, strlen($blobData), time());
            $transfers->incrementChunksReceived($transferId);
            $storedNewChunk = true;
            AppLog::log('Transfer', sprintf(
                'transfer.chunk.uploaded transfer_id=%s chunk_index=%d size=%d',
                AppLog::shortId($transferId), $chunkIndex, strlen($blobData)
            ), 'debug');
        }

        $transition = TransferLifecycle::onChunkStored($transfer, $storedNewChunk);
        $complete = $transition['is_complete'];

        if ($complete) {
            $transfers->markComplete($transferId);
            AppLog::log('Transfer', sprintf(
                'transfer.upload.completed transfer_id=%s sender=%s recipient=%s chunks=%d',
                AppLog::shortId($transferId),
                AppLog::shortId($transfer['sender_id']),
                AppLog::shortId($transfer['recipient_id']),
                (int)$transfer['chunk_count']
            ));
            TransferWakeService::wake($db, $transferId);
        }

        return [
            'chunks_received' => $transition['chunks_received'],
            'complete' => $complete,
        ];
    }

    public static function listPending(Database $db, string $deviceId): array
    {
        return (new TransferRepository($db))->listPendingForRecipient($deviceId);
    }

    /** Returns the chunk's raw bytes. Controller emits them via Router::binary. */
    public static function downloadChunk(Database $db, string $deviceId, string $transferId, int $chunkIndex): string
    {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            throw new NotFoundError('Transfer not found or not for you');
        }
        $chunk = (new ChunkRepository($db))->findChunk($transferId, $chunkIndex);
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
        $transition = TransferLifecycle::onRecipientProgress($transfer, $chunkIndex);
        $newProgress = $transition['next_progress'];
        $transfers->updateDownloadProgress($transferId, $newProgress);
        AppLog::log('Transfer', sprintf(
            'transfer.chunk.served transfer_id=%s chunk_index=%d progress=%d/%d',
            AppLog::shortId($transferId), $chunkIndex, $newProgress, (int)$transfer['chunk_count']
        ), 'debug');

        return file_get_contents($fullPath);
    }

    public static function ack(Database $db, string $deviceId, string $transferId): array
    {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            throw new NotFoundError('Transfer not found');
        }
        TransferLifecycle::onAckReceived($transfer);

        // Pairing-stats SUM must run BEFORE chunk deletion (chunks table still holds sizes here).
        $senderId = $transfer['sender_id'];
        $totalBytes = (new ChunkRepository($db))->sumChunkBytesForTransfer($transferId);

        $ids = [$senderId, $deviceId];
        sort($ids);
        (new PairingRepository($db))->incrementPairingStats($ids[0], $ids[1], $totalBytes);

        TransferCleanupService::deleteChunkFilesAndRows($db, $transferId);

        // chunks_downloaded reaches chunk_count only here (on ack), not during serving.
        $transfers->markDelivered($transferId, time());
        AppLog::log('Delivery', sprintf(
            'delivery.acked transfer_id=%s recipient=%s total_bytes=%d',
            AppLog::shortId($transferId), AppLog::shortId($deviceId), $totalBytes
        ));

        return ['status' => 'deleted'];
    }
}
