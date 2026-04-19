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

            $transfer = (new TransferRepository($db))->findById($transferId);
            if (!$transfer) {
                return;
            }

            $token = (new DeviceRepository($db))->findFcmToken($transfer['recipient_id']);
            if ($token === null) {
                return;
            }

            FcmSender::sendDataMessage($token, [
                'type' => 'transfer_ready',
                'transfer_id' => $transferId,
            ]);
        } catch (\Throwable $e) {
            // FCM failure must never break the transfer flow
        }
    }
}
