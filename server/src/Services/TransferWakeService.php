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
        // Silent return when FCM isn't configured — nothing to wake with, no
        // operational signal lost (the transfer will be picked up on the next
        // regular poll and logged at `transfer.pending.found` on the recipient).
        if (!FcmSender::isAvailable()) {
            return;
        }

        $recipient = '-';
        $result = 'failed';
        try {
            $transfer = (new TransferRepository($db))->findById($transferId);
            if (!$transfer) {
                $result = 'no_transfer';
                return;
            }
            $recipient = AppLog::shortId($transfer['recipient_id']);

            $token = (new DeviceRepository($db))->findFcmToken($transfer['recipient_id']);
            if ($token === null) {
                $result = 'no_token';
                return;
            }

            $ok = FcmSender::sendDataMessage($token, [
                'type' => 'transfer_ready',
                'transfer_id' => $transferId,
            ]);
            $result = $ok ? 'sent' : 'failed';
        } catch (\Throwable $e) {
            // FCM failure must never break the transfer flow.
            $result = 'failed';
        } finally {
            AppLog::log('Transfer', sprintf(
                'transfer.wake.sent transfer_id=%s recipient=%s fcm_result=%s',
                AppLog::shortId($transferId), $recipient, $result
            ));
        }
    }
}
