<?php

/**
 * Sends a silent FCM wake to the recipient when a transfer becomes complete.
 * Data-only payload, fire-and-forget — any failure is swallowed so the
 * transfer upload flow is never broken by FCM problems (service unavailable,
 * missing token, network error).
 */
class TransferWakeService
{
    public static function wake(Database $db, string $transferId): void
    {
        try {
            if (!FcmSender::isAvailable()) {
                return;
            }

            $transfer = $db->querySingle(
                'SELECT recipient_id FROM transfers WHERE id = :id',
                [':id' => $transferId]
            );
            if (!$transfer) {
                return;
            }

            $device = $db->querySingle(
                'SELECT fcm_token FROM devices WHERE device_id = :id',
                [':id' => $transfer['recipient_id']]
            );
            if (!$device || empty($device['fcm_token'])) {
                return;
            }

            FcmSender::sendDataMessage($device['fcm_token'], [
                'type' => 'transfer_ready',
                'transfer_id' => $transferId,
            ]);
        } catch (\Throwable $e) {
            // FCM failure must never break the transfer flow
        }
    }
}
