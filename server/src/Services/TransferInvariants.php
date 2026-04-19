<?php

/**
 * Central transfer-state invariant checker.
 *
 * Violations are logged and converted to TransferInvariantViolation so callers
 * fail fast with a controlled 500 response instead of silently accepting
 * impossible states.
 */
class TransferInvariants
{
    public static function assertRow(array $row, string $location): void
    {
        $transferId = (string)($row['transfer_id'] ?? $row['id'] ?? '-');
        $chunkCount = (int)($row['chunk_count'] ?? 0);
        $chunksReceived = (int)($row['chunks_received'] ?? 0);
        $chunksDownloaded = (int)($row['chunks_downloaded'] ?? 0);
        $complete = (int)($row['complete'] ?? 0);
        $downloaded = (int)($row['downloaded'] ?? 0);
        $deliveredAt = (int)($row['delivered_at'] ?? 0);

        self::require($chunkCount >= 0, 'chunk_count must be >= 0', $location, $transferId, $row);
        self::require(
            $chunksReceived >= 0 && $chunksReceived <= $chunkCount,
            '0 <= chunks_received <= chunk_count',
            $location,
            $transferId,
            $row
        );
        self::require(
            $chunksDownloaded >= 0 && $chunksDownloaded <= $chunkCount,
            '0 <= chunks_downloaded <= chunk_count',
            $location,
            $transferId,
            $row
        );
        self::require(
            $complete !== 1 || $chunksReceived === $chunkCount,
            'complete == 1 -> chunks_received == chunk_count',
            $location,
            $transferId,
            $row
        );
        self::require(
            $downloaded !== 1 || $chunksDownloaded === $chunkCount,
            'downloaded == 1 -> chunks_downloaded == chunk_count',
            $location,
            $transferId,
            $row
        );
        self::require(
            $downloaded !== 1 || $deliveredAt > 0,
            'downloaded == 1 -> delivered_at > 0',
            $location,
            $transferId,
            $row
        );
        self::require(
            $downloaded === 1 || ($chunksDownloaded < $chunkCount && $deliveredAt <= 0),
            'no delivered-equivalent state before ACK',
            $location,
            $transferId,
            $row
        );
    }

    private static function require(
        bool $ok,
        string $rule,
        string $location,
        string $transferId,
        array $row
    ): void {
        if ($ok) {
            return;
        }

        AppLog::log('TransferInvariant', sprintf(
            'transfer.invariant.failed location=%s transfer_id=%s rule="%s" snapshot=%s',
            $location,
            AppLog::shortId($transferId),
            $rule,
            self::compactSnapshot($row)
        ), 'error');

        throw new TransferInvariantViolation();
    }

    private static function compactSnapshot(array $row): string
    {
        $allowed = ['id', 'transfer_id', 'chunk_count', 'chunks_received', 'chunks_downloaded', 'complete', 'downloaded', 'delivered_at'];
        $out = [];
        foreach ($allowed as $k) {
            if (array_key_exists($k, $row)) {
                $out[] = $k . '=' . (is_scalar($row[$k]) ? (string)$row[$k] : '[non-scalar]');
            }
        }
        return implode(',', $out);
    }
}
