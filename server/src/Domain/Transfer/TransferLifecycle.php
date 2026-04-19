<?php

final class TransferLifecycle
{
    /**
     * Canonical transfer state derivation from persisted transfer fields.
     *
     * Expected row fields: complete, downloaded, chunks_received, chunk_count,
     * chunks_downloaded, delivered_at.
     */
    public static function deriveState(array $transferRow): TransferState
    {
        $complete = (int)($transferRow['complete'] ?? 0);
        $downloaded = (int)($transferRow['downloaded'] ?? 0);
        $chunksReceived = (int)($transferRow['chunks_received'] ?? 0);
        $chunkCount = (int)($transferRow['chunk_count'] ?? 0);
        $chunksDownloaded = (int)($transferRow['chunks_downloaded'] ?? 0);
        $deliveredAt = (int)($transferRow['delivered_at'] ?? 0);

        if ($chunkCount < 0) {
            throw new InvalidArgumentException('Invalid transfer row: chunk_count < 0');
        }
        if ($chunksReceived < 0 || $chunksReceived > $chunkCount) {
            throw new InvalidArgumentException('Invalid transfer row: chunks_received out of range');
        }
        if ($chunksDownloaded < 0 || $chunksDownloaded > $chunkCount) {
            throw new InvalidArgumentException('Invalid transfer row: chunks_downloaded out of range');
        }
        if ($complete === 1 && $chunksReceived !== $chunkCount) {
            throw new InvalidArgumentException('Invalid transfer row: complete requires full chunks_received');
        }
        if ($downloaded === 1 && $chunksDownloaded !== $chunkCount) {
            throw new InvalidArgumentException('Invalid transfer row: downloaded requires full chunks_downloaded');
        }
        if ($downloaded === 1 && $deliveredAt <= 0) {
            throw new InvalidArgumentException('Invalid transfer row: downloaded requires delivered_at > 0');
        }

        if ($downloaded === 1) {
            return TransferState::from(TransferState::DELIVERED);
        }
        if ($complete === 1) {
            if ($chunksDownloaded > 0) {
                return TransferState::from(TransferState::DELIVERING);
            }
            return TransferState::from(TransferState::UPLOADED);
        }
        if ($chunksReceived > 0) {
            return TransferState::from(TransferState::UPLOADING);
        }
        return TransferState::from(TransferState::INITIALIZED);
    }
}
