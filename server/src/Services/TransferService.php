<?php

/**
 * Core transfer orchestration: init, upload, pending list, download, ack.
 *
 * Each method returns a [data, httpStatus] tuple so the controller stays a
 * thin HTTP adapter and the service is free of HTTP concerns. Downloads
 * return ['binary' => bytes] for the happy path so the adapter knows to
 * emit a binary response instead of JSON.
 *
 * Side effects route through sibling services: upload completion fires
 * TransferWakeService::wake; ack removes chunk storage via
 * TransferCleanupService::deleteChunkFilesAndRows.
 */
class TransferService
{
    private const MAX_PENDING_BYTES = 500 * 1024 * 1024;   // 500 MB per recipient
    private const MAX_CHUNK_COUNT = 500;
    // transfer_id is concatenated into a filesystem path (server/storage/{id}/...).
    // Restrict to alphanumeric + hyphen with a length cap so "../" and friends
    // can never escape the storage directory. Matches Android's defense-in-depth
    // check in PollService.SAFE_TRANSFER_ID; both desktop and Android generate
    // UUIDs which fit comfortably under 64 chars.
    private const TRANSFER_ID_PATTERN = '/^[a-zA-Z0-9-]{1,64}$/';

    public static function init(Database $db, string $senderId, array $body): array
    {
        if (empty($body) || empty($body['transfer_id']) || empty($body['recipient_id'])
            || empty($body['encrypted_meta']) || !isset($body['chunk_count'])) {
            return [['error' => 'Missing required fields'], 400];
        }

        $transferId = $body['transfer_id'];
        $recipientId = $body['recipient_id'];
        $encryptedMeta = $body['encrypted_meta'];
        $chunkCount = (int)$body['chunk_count'];

        if ($err = self::validateTransferId($transferId)) {
            return $err;
        }
        if ($chunkCount < 1 || $chunkCount > self::MAX_CHUNK_COUNT) {
            return [['error' => 'Invalid chunk_count'], 400];
        }

        $usage = $db->querySingle(
            'SELECT COALESCE(SUM(c.blob_size), 0) as total_bytes
             FROM chunks c
             JOIN transfers t ON c.transfer_id = t.id
             WHERE t.recipient_id = :rid AND t.downloaded = 0',
            [':rid' => $recipientId]
        );
        if ($usage && $usage['total_bytes'] >= self::MAX_PENDING_BYTES) {
            return [['error' => 'Recipient storage limit exceeded'], 507];
        }

        $existing = $db->querySingle(
            'SELECT id FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if ($existing) {
            return [['error' => 'Transfer ID already exists'], 409];
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

        return [['transfer_id' => $transferId, 'status' => 'awaiting_chunks'], 201];
    }

    public static function uploadChunk(Database $db, string $deviceId, string $transferId, int $chunkIndex, string $blobData): array
    {
        if ($err = self::validateTransferId($transferId)) {
            return $err;
        }
        $transfer = $db->querySingle(
            'SELECT id, sender_id, chunk_count, chunks_received, complete
             FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );

        if (!$transfer) {
            return [['error' => 'Transfer not found'], 404];
        }
        if ($transfer['sender_id'] !== $deviceId) {
            return [['error' => 'Not the sender of this transfer'], 403];
        }
        if ($chunkIndex < 0 || $chunkIndex >= $transfer['chunk_count']) {
            return [['error' => 'Invalid chunk_index'], 400];
        }
        if ($blobData === '') {
            return [['error' => 'Empty chunk data'], 400];
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

        return [[
            'chunks_received' => (int)$updated['chunks_received'],
            'complete' => $complete,
        ], 200];
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

    public static function downloadChunk(Database $db, string $deviceId, string $transferId, int $chunkIndex): array
    {
        if ($err = self::validateTransferId($transferId)) {
            return $err;
        }
        $transfer = $db->querySingle(
            'SELECT recipient_id, chunk_count FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            return [['error' => 'Transfer not found or not for you'], 404];
        }

        $chunk = $db->querySingle(
            'SELECT blob_path FROM chunks WHERE transfer_id = :tid AND chunk_index = :idx',
            [':tid' => $transferId, ':idx' => $chunkIndex]
        );
        if (!$chunk) {
            return [['error' => 'Chunk not found'], 404];
        }

        $fullPath = __DIR__ . '/../../storage/' . $chunk['blob_path'];
        if (!file_exists($fullPath)) {
            return [['error' => 'Chunk file missing from storage'], 500];
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

        return [['binary' => file_get_contents($fullPath)], 200];
    }

    public static function ack(Database $db, string $deviceId, string $transferId): array
    {
        if ($err = self::validateTransferId($transferId)) {
            return $err;
        }
        $transfer = $db->querySingle(
            'SELECT id, sender_id, recipient_id FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            return [['error' => 'Transfer not found'], 404];
        }

        // Pairing-stats SUM must run BEFORE chunk deletion (chunks table still holds sizes here).
        $senderId = $transfer['sender_id'];
        $totalBytes = $db->querySingle(
            'SELECT COALESCE(SUM(blob_size), 0) as total FROM chunks WHERE transfer_id = :tid',
            [':tid' => $transferId]
        );

        $ids = [$senderId, $deviceId];
        sort($ids);
        $db->execute(
            'UPDATE pairings SET bytes_transferred = bytes_transferred + :bytes,
             transfer_count = transfer_count + 1
             WHERE device_a_id = :a AND device_b_id = :b',
            [':bytes' => $totalBytes['total'], ':a' => $ids[0], ':b' => $ids[1]]
        );

        TransferCleanupService::deleteChunkFilesAndRows($db, $transferId);

        // chunks_downloaded reaches chunk_count only here (on ack), not during serving.
        $db->execute(
            'UPDATE transfers SET downloaded = 1, delivered_at = :now, chunks_downloaded = chunk_count WHERE id = :id',
            [':now' => time(), ':id' => $transferId]
        );

        return [['status' => 'deleted'], 200];
    }

    /** Returns [errorResponse, 400] if the id is unsafe, or null if OK. */
    private static function validateTransferId(string $transferId): ?array
    {
        if (!preg_match(self::TRANSFER_ID_PATTERN, $transferId)) {
            return [['error' => 'Invalid transfer_id format'], 400];
        }
        return null;
    }
}
