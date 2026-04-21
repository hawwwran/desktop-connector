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
     * Adds an `aborted` short-circuit ahead of the classic state derivation
     * so aborted rows always report as aborted regardless of the pre-abort
     * progress counters that stay frozen in the row.
     */
    public static function computeStatus(array $row): array
    {
        if ((int)($row['aborted'] ?? 0) === 1) {
            return [
                'status' => 'aborted',
                'delivery_state' => 'aborted',
            ];
        }
        $internalState = TransferLifecycle::deriveState($row);
        return TransferStatusMapper::toProtocolStatus($internalState);
    }

    /** Full per-transfer dict for /sent-status (includes created_at). */
    public static function formatSent(array $row): array
    {
        $s = self::computeStatus($row);
        $out = [
            'transfer_id' => $row['transfer_id'],
            'status' => $s['status'],
            'delivery_state' => $s['delivery_state'],
            'chunks_downloaded' => (int)($row['chunks_downloaded'] ?? 0),
            'chunk_count' => (int)$row['chunk_count'],
            'created_at' => (int)$row['created_at'],
        ];
        // Additive streaming fields. Old clients ignore unknown JSON
        // keys; new clients use these to paint "Sending X→Y" / "Aborted".
        $mode = $row['mode'] ?? 'classic';
        $out['mode'] = $mode;
        if ($mode === 'streaming') {
            $out['chunks_uploaded'] = (int)($row['chunks_uploaded'] ?? 0);
        }
        if ((int)($row['aborted'] ?? 0) === 1 && !empty($row['abort_reason'])) {
            $out['abort_reason'] = (string)$row['abort_reason'];
        }
        return $out;
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
