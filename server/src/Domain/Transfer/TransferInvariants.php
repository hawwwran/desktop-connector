<?php

/**
 * Runtime invariants for rows in `transfers`.
 *
 * Keep these checks strict and side-effect free. They are intended to run
 * right after state-changing operations so we fail loudly on impossible state.
 */
class TransferInvariants
{
    public static function assertValid(array $transferRow): void
    {
        $chunkCount = (int)($transferRow['chunk_count'] ?? 0);
        $chunksReceived = (int)($transferRow['chunks_received'] ?? 0);
        $chunksDownloaded = (int)($transferRow['chunks_downloaded'] ?? 0);
        $complete = (int)($transferRow['complete'] ?? 0) === 1;
        $downloaded = (int)($transferRow['downloaded'] ?? 0) === 1;
        $deliveredAt = $transferRow['delivered_at'] ?? null;

        if ($chunkCount < 1) {
            throw new ValidationError('Invariant violation: chunk_count must be >= 1');
        }
        if ($chunksReceived < 0 || $chunksReceived > $chunkCount) {
            throw new ValidationError('Invariant violation: chunks_received out of range');
        }
        if ($chunksDownloaded < 0 || $chunksDownloaded > $chunkCount) {
            throw new ValidationError('Invariant violation: chunks_downloaded out of range');
        }
        if ($complete && $chunksReceived < $chunkCount) {
            throw new ValidationError('Invariant violation: complete transfer missing chunks');
        }
        if (!$complete && $chunksReceived >= $chunkCount) {
            throw new ValidationError('Invariant violation: transfer has all chunks but is not complete');
        }
        if ($downloaded !== ($chunksDownloaded === $chunkCount)) {
            throw new ValidationError('Invariant violation: downloaded/chunks_downloaded mismatch');
        }
        if ($downloaded && ($deliveredAt === null || (int)$deliveredAt <= 0)) {
            throw new ValidationError('Invariant violation: delivered transfer missing delivered_at');
        }
        if (!$downloaded && $deliveredAt !== null) {
            throw new ValidationError('Invariant violation: undelivered transfer has delivered_at');
        }
    }

    public static function assertUploadMutation(array $transferRow): void
    {
        self::assertValid($transferRow);

        $chunkCount = (int)$transferRow['chunk_count'];
        $chunksReceived = (int)$transferRow['chunks_received'];
        $complete = (int)$transferRow['complete'] === 1;

        if ($chunksReceived === $chunkCount && !$complete) {
            throw new ValidationError('Invariant violation: upload reached all chunks without completion');
        }
    }

    public static function assertDownloadProgress(array $transferRow): void
    {
        self::assertValid($transferRow);

        $chunkCount = (int)$transferRow['chunk_count'];
        $chunksDownloaded = (int)$transferRow['chunks_downloaded'];
        $downloaded = (int)$transferRow['downloaded'] === 1;

        if (!$downloaded && $chunksDownloaded >= $chunkCount) {
            throw new ValidationError('Invariant violation: progress reached completion before ack');
        }
    }

    public static function assertAckTransition(array $transferRow): void
    {
        self::assertValid($transferRow);

        if ((int)$transferRow['downloaded'] !== 1) {
            throw new ApiError(500, 'Invariant violation: ack did not mark transfer as downloaded');
        }
    }
}
