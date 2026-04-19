<?php

/**
 * Maps internal transfer lifecycle state to protocol status payload.
 */
class TransferStatusMapper
{
    /**
     * @return array{status: string, delivery_state: string}
     */
    public static function toProtocol(string $state, array $row): array
    {
        if ($state === TransferLifecycle::STATE_DELIVERED) {
            return ['status' => 'delivered', 'delivery_state' => 'delivered'];
        }
        if ($state === TransferLifecycle::STATE_PENDING) {
            return [
                'status' => 'pending',
                'delivery_state' => ((int)($row['chunks_downloaded'] ?? 0) > 0)
                    ? 'in_progress'
                    : 'not_started',
            ];
        }
        return ['status' => 'uploading', 'delivery_state' => 'not_started'];
    }
}
