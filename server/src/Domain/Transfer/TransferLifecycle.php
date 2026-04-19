<?php

/**
 * Single point of truth for transfer lifecycle semantics:
 * state derivation, transition legality, and named transition helpers.
 */
class TransferLifecycle
{
    public static function deriveState(array $transfer): string
    {
        $complete = (int)($transfer['complete'] ?? 0);
        $downloaded = (int)($transfer['downloaded'] ?? 0);
        $chunksReceived = (int)($transfer['chunks_received'] ?? 0);
        $chunksDownloaded = (int)($transfer['chunks_downloaded'] ?? 0);

        if ($downloaded === 1) {
            return TransferState::DELIVERED;
        }
        if ($complete === 1) {
            return $chunksDownloaded > 0 ? TransferState::DELIVERING : TransferState::UPLOADED;
        }
        return $chunksReceived > 0 ? TransferState::UPLOADING : TransferState::INITIALIZED;
    }

    /**
     * Transition helper for "chunk stored" upload events.
     * $storedNewChunk indicates whether chunks_received was incremented.
     */
    public static function onChunkStored(array $transfer, bool $storedNewChunk): array
    {
        TransferInvariants::assertValid($transfer);
        $from = self::deriveState($transfer);
        $chunkCount = (int)$transfer['chunk_count'];
        $chunksReceived = (int)$transfer['chunks_received'] + ($storedNewChunk ? 1 : 0);

        $isComplete = $chunksReceived >= $chunkCount;
        $to = $isComplete
            ? TransferState::UPLOADED
            : ($chunksReceived > 0 ? TransferState::UPLOADING : TransferState::INITIALIZED);

        self::assertTransitionAllowed($from, $to);

        return [
            'from' => $from,
            'to' => $to,
            'is_complete' => $isComplete,
            'chunks_received' => $chunksReceived,
        ];
    }

    /** Transition helper for recipient download progress updates. */
    public static function onRecipientProgress(array $transfer, int $chunkIndex): array
    {
        TransferInvariants::assertValid($transfer);
        $from = self::deriveState($transfer);
        $chunkCount = (int)$transfer['chunk_count'];
        $cap = max(0, $chunkCount - 1);
        $requestedProgress = min($chunkIndex + 1, $cap);
        $nextProgress = max((int)$transfer['chunks_downloaded'], $requestedProgress);

        $to = $nextProgress > 0 ? TransferState::DELIVERING : TransferState::UPLOADED;
        self::assertTransitionAllowed($from, $to);

        return [
            'from' => $from,
            'to' => $to,
            'next_progress' => $nextProgress,
        ];
    }

    /** Transition helper for final ACK. */
    public static function onAckReceived(array $transfer): array
    {
        TransferInvariants::assertValid($transfer);
        $from = self::deriveState($transfer);
        $to = TransferState::DELIVERED;
        self::assertTransitionAllowed($from, $to);
        return ['from' => $from, 'to' => $to];
    }

    /** Conceptual expiry transition (cleanup typically deletes rows). */
    public static function onTransferExpired(array $transfer): array
    {
        TransferInvariants::assertValid($transfer);
        $from = self::deriveState($transfer);
        $to = TransferState::EXPIRED;
        self::assertTransitionAllowed($from, $to);
        return ['from' => $from, 'to' => $to];
    }

    private static function assertTransitionAllowed(string $from, string $to): void
    {
        if ($from === $to) {
            return;
        }

        $allowed = [
            TransferState::INITIALIZED => [TransferState::UPLOADING, TransferState::UPLOADED, TransferState::EXPIRED],
            TransferState::UPLOADING => [TransferState::UPLOADING, TransferState::UPLOADED, TransferState::EXPIRED],
            TransferState::UPLOADED => [TransferState::DELIVERING, TransferState::DELIVERED, TransferState::EXPIRED],
            TransferState::DELIVERING => [TransferState::DELIVERING, TransferState::DELIVERED, TransferState::EXPIRED],
            TransferState::DELIVERED => [],
            TransferState::EXPIRED => [],
        ];

        if (!in_array($to, $allowed[$from] ?? [], true)) {
            throw new ApiError(500, sprintf(
                'Invalid transfer lifecycle transition: %s -> %s',
                $from,
                $to,
            ));
        }
    }
}
