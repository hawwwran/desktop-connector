<?php

/**
 * Single point of truth for transfer lifecycle semantics:
 * state derivation, transition legality, and named transition helpers.
 */
class TransferLifecycle
{
    /**
     * Derive an internal state from a persisted row. Never returns EXPIRED —
     * that is a terminal state set only by cleanup paths via onTransferExpired().
     */
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

    /** Transition helper for "chunk stored" upload events. */
    public static function onChunkStored(array $before, array $after): array
    {
        TransferInvariants::assertValid($before);
        TransferInvariants::assertValid($after);
        $from = self::deriveState($before);
        $to = self::deriveState($after);

        self::assertTransitionAllowed($from, $to);

        return [
            'from' => $from,
            'to' => $to,
            'is_complete' => (int)($after['complete'] ?? 0) === 1,
            'chunks_received' => (int)($after['chunks_received'] ?? 0),
        ];
    }

    /** Compute recipient download progress target (capped below ack-complete). */
    public static function recipientProgressTarget(array $transfer, int $chunkIndex): int
    {
        $chunkCount = (int)$transfer['chunk_count'];
        $cap = max(0, $chunkCount - 1);
        $requestedProgress = min($chunkIndex + 1, $cap);
        return max((int)$transfer['chunks_downloaded'], $requestedProgress);
    }

    /** Transition helper for recipient download progress updates. */
    public static function onRecipientProgress(array $before, array $after): array
    {
        TransferInvariants::assertValid($before);
        TransferInvariants::assertValid($after);
        $from = self::deriveState($before);
        $to = self::deriveState($after);

        self::assertTransitionAllowed($from, $to);

        return [
            'from' => $from,
            'to' => $to,
            'next_progress' => (int)($after['chunks_downloaded'] ?? 0),
        ];
    }

    /** Transition helper for final ACK. */
    public static function onAckReceived(array $before, array $after): array
    {
        TransferInvariants::assertValid($before);
        TransferInvariants::assertValid($after);
        $from = self::deriveState($before);
        $to = self::deriveState($after);
        if ($to !== TransferState::DELIVERED) {
            throw new ApiError(500, 'Invalid transfer lifecycle transition: ACK must end in delivered state');
        }
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

        // Streaming mode can legitimately bypass the UPLOADED "idle"
        // phase: the recipient starts pulling as soon as chunk 0 is
        // stored, so by the time the sender's LAST chunk upload flips
        // complete=1, chunks_downloaded may already be >0 — which
        // derives to DELIVERING, not UPLOADED. Classic transfers still
        // take INITIALIZED -> UPLOADING -> UPLOADED -> DELIVERING ->
        // DELIVERED because the recipient can't download until
        // complete=1. See streaming-improvement.md §3.
        $allowed = [
            TransferState::INITIALIZED => [TransferState::UPLOADING, TransferState::UPLOADED, TransferState::DELIVERING, TransferState::EXPIRED],
            TransferState::UPLOADING => [TransferState::UPLOADING, TransferState::UPLOADED, TransferState::DELIVERING, TransferState::EXPIRED],
            TransferState::UPLOADED => [TransferState::DELIVERING, TransferState::DELIVERED, TransferState::EXPIRED],
            TransferState::DELIVERING => [TransferState::DELIVERING, TransferState::DELIVERED, TransferState::EXPIRED],
            TransferState::DELIVERED => [TransferState::EXPIRED],
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
