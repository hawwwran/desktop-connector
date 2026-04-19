<?php

/**
 * Maps internal lifecycle states to protocol-level status values.
 */
class TransferStatusMapper
{
    public static function toProtocolStatus(string $internalState): array
    {
        return match ($internalState) {
            TransferState::INITIALIZED,
            TransferState::UPLOADING => [
                'status' => 'uploading',
                'delivery_state' => 'not_started',
            ],
            TransferState::UPLOADED => [
                'status' => 'pending',
                'delivery_state' => 'not_started',
            ],
            TransferState::DELIVERING => [
                'status' => 'pending',
                'delivery_state' => 'in_progress',
            ],
            TransferState::DELIVERED => [
                'status' => 'delivered',
                'delivery_state' => 'delivered',
            ],
            default => throw new ApiError(500, sprintf(
                'Unknown internal transfer state for protocol mapping: %s',
                $internalState,
            )),
        };
    }
}
