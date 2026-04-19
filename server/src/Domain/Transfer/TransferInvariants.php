<?php

/**
 * Centralized transfer invariants over persisted transfer fields.
 */
class TransferInvariants
{
    public static function assertValid(array $row): void
    {
        $chunkCount = (int)($row['chunk_count'] ?? 0);
        $chunksReceived = (int)($row['chunks_received'] ?? 0);
        $complete = (int)($row['complete'] ?? 0);
        $downloaded = (int)($row['downloaded'] ?? 0);
        $chunksDownloaded = (int)($row['chunks_downloaded'] ?? 0);
        $deliveredAt = (int)($row['delivered_at'] ?? 0);

        self::ensure($chunkCount >= 1, 'Invalid transfer invariant: chunk_count must be >= 1');

        self::ensure(
            $chunksReceived >= 0 && $chunksReceived <= $chunkCount,
            'Invalid transfer invariant: chunks_received out of range'
        );
        self::ensure(
            $chunksDownloaded >= 0 && $chunksDownloaded <= $chunkCount,
            'Invalid transfer invariant: chunks_downloaded out of range'
        );

        if ($complete === 1) {
            self::ensure(
                $chunksReceived === $chunkCount,
                'Invalid transfer invariant: complete=1 requires chunks_received == chunk_count'
            );
        }

        if ($downloaded === 1) {
            self::ensure(
                $chunksDownloaded === $chunkCount,
                'Invalid transfer invariant: downloaded=1 requires chunks_downloaded == chunk_count'
            );
            self::ensure(
                $deliveredAt > 0,
                'Invalid transfer invariant: downloaded=1 requires delivered_at > 0'
            );
        }

        // Safety invariant: final delivery equivalence only after ACK.
        if ($chunksDownloaded === $chunkCount) {
            self::ensure(
                $downloaded === 1,
                'Invalid transfer invariant: chunks_downloaded == chunk_count requires downloaded=1 (ACKed)'
            );
        }
    }

    private static function ensure(bool $condition, string $message): void
    {
        if (!$condition) {
            throw new ApiError(500, $message);
        }
    }
}
