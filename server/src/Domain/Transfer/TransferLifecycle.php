<?php

class TransferLifecycle
{
    /**
     * Derive the internal TransferState from persisted transfer row fields.
     *
     * Expected row fields: complete, downloaded, chunks_received,
     * chunk_count, chunks_downloaded, delivered_at.
     */
    public static function deriveState(array $transferRow): string
    {
        $complete = (int)($transferRow['complete'] ?? 0);
        $downloaded = (int)($transferRow['downloaded'] ?? 0);
        $chunksReceived = (int)($transferRow['chunks_received'] ?? 0);
        $chunkCount = (int)($transferRow['chunk_count'] ?? 0);
        $chunksDownloaded = (int)($transferRow['chunks_downloaded'] ?? 0);
        $deliveredAt = (int)($transferRow['delivered_at'] ?? 0);

        if ($downloaded === 1 || ($deliveredAt > 0 && $chunksDownloaded >= $chunkCount && $chunkCount > 0)) {
            return TransferState::DELIVERED;
        }

        if ($complete === 1) {
            return $chunksDownloaded > 0 ? TransferState::DELIVERING : TransferState::UPLOADED;
        }

        return $chunksReceived > 0 ? TransferState::UPLOADING : TransferState::INITIALIZED;
    }
}
