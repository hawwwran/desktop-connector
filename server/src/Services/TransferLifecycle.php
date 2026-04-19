<?php

/**
 * Central transfer lifecycle transitions.
 *
 * Responsibilities:
 * - derive internal state from persisted transfer fields
 * - validate allowed state transition edges
 * - apply repository mutations for lifecycle events
 * - assert key invariants before and after transitions
 */
class TransferLifecycle
{
    public const STATE_INITIALIZED = 'initialized';
    public const STATE_UPLOADING = 'uploading';
    public const STATE_UPLOADED = 'uploaded';
    public const STATE_DELIVERING = 'delivering';
    public const STATE_DELIVERED = 'delivered';

    private const ALLOWED_EDGES = [
        self::STATE_INITIALIZED => [self::STATE_UPLOADING, self::STATE_UPLOADED],
        self::STATE_UPLOADING => [self::STATE_UPLOADING, self::STATE_UPLOADED],
        self::STATE_UPLOADED => [self::STATE_DELIVERING, self::STATE_DELIVERED],
        self::STATE_DELIVERING => [self::STATE_DELIVERING, self::STATE_DELIVERED],
    ];

    public static function deriveState(array $transfer): string
    {
        self::assertInvariants($transfer);

        $complete = (int)($transfer['complete'] ?? 0);
        $downloaded = (int)($transfer['downloaded'] ?? 0);
        $chunksReceived = (int)($transfer['chunks_received'] ?? 0);
        $chunksDownloaded = (int)($transfer['chunks_downloaded'] ?? 0);

        if ($downloaded === 1) {
            return self::STATE_DELIVERED;
        }
        if ($complete === 1) {
            return $chunksDownloaded > 0 ? self::STATE_DELIVERING : self::STATE_UPLOADED;
        }
        return $chunksReceived > 0 ? self::STATE_UPLOADING : self::STATE_INITIALIZED;
    }

    public static function onChunkStored(TransferRepository $transfers, string $transferId): array
    {
        $before = self::requireTransfer($transfers, $transferId);
        $from = self::deriveState($before);

        if ($from !== self::STATE_INITIALIZED && $from !== self::STATE_UPLOADING) {
            throw new ApiError(500, 'Invalid chunk-stored transition source state');
        }

        $transfers->incrementChunksReceived($transferId);

        $after = self::requireTransfer($transfers, $transferId);
        self::assertInvariants($after);

        $to = self::deriveState($after);
        self::assertAllowedEdge($from, $to, 'onChunkStored');

        return $after;
    }

    public static function onUploadCompleted(TransferRepository $transfers, string $transferId): array
    {
        $before = self::requireTransfer($transfers, $transferId);
        $from = self::deriveState($before);

        if ($from !== self::STATE_INITIALIZED && $from !== self::STATE_UPLOADING) {
            throw new ApiError(500, 'Invalid upload-completed transition source state');
        }

        $transfers->markComplete($transferId);

        $after = self::requireTransfer($transfers, $transferId);
        self::assertInvariants($after);

        $to = self::deriveState($after);
        self::assertAllowedEdge($from, $to, 'onUploadCompleted');

        return $after;
    }

    public static function onRecipientProgress(TransferRepository $transfers, string $transferId, int $progress): array
    {
        $before = self::requireTransfer($transfers, $transferId);
        $from = self::deriveState($before);

        if ($from !== self::STATE_UPLOADED && $from !== self::STATE_DELIVERING) {
            throw new ApiError(500, 'Invalid recipient-progress transition source state');
        }

        $currentProgress = (int)($before['chunks_downloaded'] ?? 0);
        if ($progress > $currentProgress) {
            $transfers->updateDownloadProgress($transferId, $progress);
        }

        $after = self::requireTransfer($transfers, $transferId);
        self::assertInvariants($after);

        if ((int)($after['chunks_downloaded'] ?? 0) > $currentProgress) {
            $to = self::deriveState($after);
            self::assertAllowedEdge($from, $to, 'onRecipientProgress');
        }

        return $after;
    }

    public static function onAckReceived(TransferRepository $transfers, string $transferId, int $now): array
    {
        $before = self::requireTransfer($transfers, $transferId);
        $from = self::deriveState($before);

        if ($from !== self::STATE_UPLOADED && $from !== self::STATE_DELIVERING) {
            throw new ApiError(500, 'Invalid ack transition source state');
        }

        $transfers->markDelivered($transferId, $now);

        $after = self::requireTransfer($transfers, $transferId);
        self::assertInvariants($after);

        $to = self::deriveState($after);
        self::assertAllowedEdge($from, $to, 'onAckReceived');

        return $after;
    }

    public static function assertInvariants(array $transfer): void
    {
        $chunkCount = (int)($transfer['chunk_count'] ?? 0);
        $chunksReceived = (int)($transfer['chunks_received'] ?? 0);
        $chunksDownloaded = (int)($transfer['chunks_downloaded'] ?? 0);
        $complete = (int)($transfer['complete'] ?? 0);
        $downloaded = (int)($transfer['downloaded'] ?? 0);
        $deliveredAt = (int)($transfer['delivered_at'] ?? 0);

        if ($chunkCount < 1) {
            throw new ApiError(500, 'Invariant violated: chunk_count must be >= 1');
        }
        if ($chunksReceived < 0 || $chunksReceived > $chunkCount) {
            throw new ApiError(500, 'Invariant violated: chunks_received out of range');
        }
        if ($chunksDownloaded < 0 || $chunksDownloaded > $chunkCount) {
            throw new ApiError(500, 'Invariant violated: chunks_downloaded out of range');
        }
        if ($complete === 1 && $chunksReceived !== $chunkCount) {
            throw new ApiError(500, 'Invariant violated: complete requires all chunks received');
        }
        if ($downloaded === 1 && $chunksDownloaded !== $chunkCount) {
            throw new ApiError(500, 'Invariant violated: downloaded requires full recipient progress');
        }
        if ($downloaded === 1 && $deliveredAt <= 0) {
            throw new ApiError(500, 'Invariant violated: downloaded requires delivered_at timestamp');
        }
    }

    private static function assertAllowedEdge(string $from, string $to, string $event): void
    {
        $allowedTargets = self::ALLOWED_EDGES[$from] ?? [];
        if (!in_array($to, $allowedTargets, true)) {
            throw new ApiError(500, sprintf(
                'Invalid lifecycle edge for %s: %s -> %s',
                $event,
                $from,
                $to
            ));
        }
    }

    private static function requireTransfer(TransferRepository $transfers, string $transferId): array
    {
        $row = $transfers->findById($transferId);
        if (!$row) {
            throw new NotFoundError('Transfer not found');
        }
        self::assertInvariants($row);
        return $row;
    }
}
