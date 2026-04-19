<?php

/**
 * Derives the internal lifecycle state from a transfers row.
 */
class TransferLifecycle
{
    public const STATE_UPLOADING = 'uploading';
    public const STATE_PENDING = 'pending';
    public const STATE_DELIVERED = 'delivered';

    /**
     * Required row fields: complete, downloaded.
     */
    public static function deriveState(array $row): string
    {
        if ((int)($row['downloaded'] ?? 0) === 1) {
            return self::STATE_DELIVERED;
        }
        if ((int)($row['complete'] ?? 0) === 1) {
            return self::STATE_PENDING;
        }
        return self::STATE_UPLOADING;
    }
}
