<?php

/**
 * Single source of truth for transfer status / delivery_state mapping.
 * Shared by /api/transfers/sent-status and the inline sent_status payload
 * emitted by /api/transfers/notify — prevents the two paths from drifting.
 */
class TransferStatusService
{
    /**
     * Map a transfers row to {status, delivery_state}.
     * Required row fields: chunk_count, complete, downloaded, chunks_downloaded.
     */
    public static function computeStatus(array $row): array
    {
        $complete = (int)($row['complete'] ?? 0);
        $downloaded = (int)($row['downloaded'] ?? 0);
        $chunksDownloaded = (int)($row['chunks_downloaded'] ?? 0);

        if ($downloaded) {
            return ['status' => TransferState::DELIVERED, 'delivery_state' => 'delivered'];
        }
        if ($complete) {
            return [
                'status' => $chunksDownloaded > 0 ? TransferState::DELIVERING : TransferState::UPLOADED,
                'delivery_state' => $chunksDownloaded > 0 ? 'in_progress' : 'not_started',
            ];
        }
        return ['status' => TransferState::UPLOADING, 'delivery_state' => 'not_started'];
    }

    /** Full per-transfer dict for /sent-status (includes created_at). */
    public static function formatSent(array $row): array
    {
        $s = self::computeStatus($row);
        return [
            'transfer_id' => $row['transfer_id'],
            'status' => $s['status'],
            'delivery_state' => $s['delivery_state'],
            'chunks_downloaded' => (int)($row['chunks_downloaded'] ?? 0),
            'chunk_count' => (int)$row['chunk_count'],
            'created_at' => (int)$row['created_at'],
        ];
    }

    /** Trimmed per-transfer dict for /notify inline payload (no created_at). */
    public static function formatSentBrief(array $row): array
    {
        $full = self::formatSent($row);
        unset($full['created_at']);
        return $full;
    }

    /**
     * Load the last $limit sent transfers for a device.
     * $onlyComplete=true matches /notify's inline query (complete = 1 filter);
     * $onlyComplete=false matches /sent-status (all transfers including in-flight uploads).
     */
    public static function loadSentForDevice(Database $db, string $deviceId, int $limit = 50, bool $onlyComplete = false): array
    {
        return (new TransferRepository($db))->loadSentForDevice($deviceId, $limit, $onlyComplete);
    }
}
