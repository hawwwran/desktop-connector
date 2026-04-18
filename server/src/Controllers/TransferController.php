<?php

class TransferController
{
    private const MAX_PENDING_BYTES = 500 * 1024 * 1024; // 500 MB per recipient

    public static function init(Database $db, string $deviceId): void
    {
        $body = Router::getJsonBody();
        if (!$body || empty($body['transfer_id']) || empty($body['recipient_id'])
            || empty($body['encrypted_meta']) || !isset($body['chunk_count'])) {
            Router::json(['error' => 'Missing required fields'], 400);
            return;
        }

        $transferId = $body['transfer_id'];
        $recipientId = $body['recipient_id'];
        $encryptedMeta = $body['encrypted_meta'];
        $chunkCount = (int)$body['chunk_count'];

        if ($chunkCount < 1 || $chunkCount > 500) {
            Router::json(['error' => 'Invalid chunk_count'], 400);
            return;
        }

        // Check storage limit for recipient
        $usage = $db->querySingle(
            'SELECT COALESCE(SUM(c.blob_size), 0) as total_bytes
             FROM chunks c
             JOIN transfers t ON c.transfer_id = t.id
             WHERE t.recipient_id = :rid AND t.downloaded = 0',
            [':rid' => $recipientId]
        );
        if ($usage && $usage['total_bytes'] >= self::MAX_PENDING_BYTES) {
            Router::json(['error' => 'Recipient storage limit exceeded'], 507);
            return;
        }

        // Check for duplicate transfer_id
        $existing = $db->querySingle(
            'SELECT id FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if ($existing) {
            Router::json(['error' => 'Transfer ID already exists'], 409);
            return;
        }

        $db->execute(
            'INSERT INTO transfers (id, sender_id, recipient_id, encrypted_meta, chunk_count, created_at)
             VALUES (:id, :sender, :recipient, :meta, :chunks, :now)',
            [
                ':id' => $transferId,
                ':sender' => $deviceId,
                ':recipient' => $recipientId,
                ':meta' => $encryptedMeta,
                ':chunks' => $chunkCount,
                ':now' => time(),
            ]
        );

        Router::json(['transfer_id' => $transferId, 'status' => 'awaiting_chunks'], 201);
    }

    public static function uploadChunk(Database $db, string $deviceId, array $params): void
    {
        $transferId = $params['transfer_id'];
        $chunkIndex = (int)$params['chunk_index'];

        // Verify transfer exists and belongs to sender
        $transfer = $db->querySingle(
            'SELECT id, sender_id, chunk_count, chunks_received, complete
             FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );

        if (!$transfer) {
            Router::json(['error' => 'Transfer not found'], 404);
            return;
        }
        if ($transfer['sender_id'] !== $deviceId) {
            Router::json(['error' => 'Not the sender of this transfer'], 403);
            return;
        }
        if ($chunkIndex < 0 || $chunkIndex >= $transfer['chunk_count']) {
            Router::json(['error' => 'Invalid chunk_index'], 400);
            return;
        }

        // Read raw body
        $blobData = Router::getRawBody();
        if (empty($blobData)) {
            Router::json(['error' => 'Empty chunk data'], 400);
            return;
        }

        // Store on disk
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

        // Check if chunk already exists (idempotent upload)
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

        // Check if complete
        $updated = $db->querySingle(
            'SELECT chunks_received, chunk_count FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        $complete = $updated['chunks_received'] >= $updated['chunk_count'];

        if ($complete) {
            $db->execute('UPDATE transfers SET complete = 1 WHERE id = :id', [':id' => $transferId]);
            TransferWakeService::wake($db, $transferId);
        }

        Router::json([
            'chunks_received' => (int)$updated['chunks_received'],
            'complete' => $complete,
        ]);
    }

    public static function pending(Database $db, string $deviceId): void
    {
        // Run garbage collection ~5% of requests (expiry is hours/days, no rush)
        if (random_int(1, 20) === 1) {
            TransferCleanupService::run($db);
        }

        $transfers = $db->queryAll(
            'SELECT id as transfer_id, sender_id, encrypted_meta, chunk_count, created_at
             FROM transfers
             WHERE recipient_id = :rid AND complete = 1 AND downloaded = 0
             ORDER BY created_at ASC',
            [':rid' => $deviceId]
        );

        Router::json(['transfers' => $transfers]);
    }

    public static function downloadChunk(Database $db, string $deviceId, array $params): void
    {
        $transferId = $params['transfer_id'];
        $chunkIndex = (int)$params['chunk_index'];

        // Verify transfer is for this recipient
        $transfer = $db->querySingle(
            'SELECT recipient_id, chunk_count FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            Router::json(['error' => 'Transfer not found or not for you'], 404);
            return;
        }

        $chunk = $db->querySingle(
            'SELECT blob_path FROM chunks WHERE transfer_id = :tid AND chunk_index = :idx',
            [':tid' => $transferId, ':idx' => $chunkIndex]
        );
        if (!$chunk) {
            Router::json(['error' => 'Chunk not found'], 404);
            return;
        }

        $fullPath = __DIR__ . '/../../storage/' . $chunk['blob_path'];
        if (!file_exists($fullPath)) {
            Router::json(['error' => 'Chunk file missing from storage'], 500);
            return;
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

        Router::binary(file_get_contents($fullPath));
    }

    public static function ack(Database $db, string $deviceId, array $params): void
    {
        $transferId = $params['transfer_id'];

        $transfer = $db->querySingle(
            'SELECT id, sender_id, recipient_id FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            Router::json(['error' => 'Transfer not found'], 404);
            return;
        }

        // Update pairing stats
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

        // Delete chunk files and chunk rows; transfer row is preserved so the
        // sender's /sent-status still reports delivered status.
        TransferCleanupService::deleteChunkFilesAndRows($db, $transferId);

        // Mark downloaded with timestamp.
        // chunks_downloaded reaches chunk_count only here (on ack), not during serving.
        $db->execute(
            'UPDATE transfers SET downloaded = 1, delivered_at = :now, chunks_downloaded = chunk_count WHERE id = :id',
            [':now' => time(), ':id' => $transferId]
        );

        Router::json(['status' => 'deleted']);
    }

    public static function sentStatus(Database $db, string $deviceId): void
    {
        $rows = TransferStatusService::loadSentForDevice($db, $deviceId);
        Router::json(['transfers' => array_map(
            [TransferStatusService::class, 'formatSent'],
            $rows
        )]);
    }

    public static function notify(Database $db, string $deviceId): void
    {
        $since = isset($_GET['since']) ? (int)$_GET['since'] : 0;
        $isTest = !empty($_GET['test']);
        Router::json(TransferNotifyService::longPoll($db, $deviceId, $since, $isTest));
    }

}
