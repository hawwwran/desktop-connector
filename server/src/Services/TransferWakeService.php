<?php

/**
 * Sends silent FCM wakes on transfer-lifecycle events.
 *
 * Three flavors, one per wake reason:
 *   - wake (classic): upload completed — recipient should start downloading
 *   - wakeStreamReady: streaming transfer has its first chunk stored
 *   - wakeAbort: either party aborted; wake the *opposite* party
 *
 * All three are fire-and-forget: any FCM failure is swallowed so the
 * transfer flow is never broken by FCM problems (service unavailable,
 * missing token, network error). The opaque envelope keeps the "no
 * content leaked" rule — `type` is the only hint about what fired.
 */
class TransferWakeService
{
    public static function wake(Database $db, string $transferId): void
    {
        self::sendWake(
            $db,
            $transferId,
            targetRecipient: true,
            fcmType: 'transfer_ready',
            logEvent: 'transfer.wake.sent',
        );
    }

    /**
     * Streaming-only. Fires `stream_ready` to the recipient so it can
     * begin pulling chunks before the sender finishes uploading. Meant
     * to be called exactly once per streaming transfer — the caller
     * (uploadChunk) gates on markStreamReady()'s return value so a
     * restart or concurrent upload doesn't re-fire.
     */
    public static function wakeStreamReady(Database $db, string $transferId): void
    {
        self::sendWake(
            $db,
            $transferId,
            targetRecipient: true,
            fcmType: 'stream_ready',
            logEvent: 'transfer.stream.ready',
        );
    }

    /**
     * Wake the party who did NOT call DELETE so their long-poll /
     * download loop notices the abort immediately instead of waiting
     * up to 25s for the next /notify tick. `abortedBy` is the caller's
     * role ('sender'|'recipient'); we wake the other side.
     */
    public static function wakeAbort(Database $db, string $transferId, string $abortedBy): void
    {
        $targetRecipient = $abortedBy === 'sender';
        self::sendWake(
            $db,
            $transferId,
            targetRecipient: $targetRecipient,
            fcmType: 'abort',
            logEvent: 'transfer.abort.wake.sent',
        );
    }

    private static function sendWake(
        Database $db,
        string $transferId,
        bool $targetRecipient,
        string $fcmType,
        string $logEvent,
    ): void {
        // Silent return when FCM isn't configured — nothing to wake with, no
        // operational signal lost (the transfer will be picked up on the next
        // regular poll and logged at `transfer.pending.found` on the recipient).
        if (!FcmSender::isAvailable()) {
            return;
        }

        $targetLog = '-';
        $result = 'failed';
        try {
            $transfer = (new TransferRepository($db))->findById($transferId);
            if (!$transfer) {
                $result = 'no_transfer';
                return;
            }
            $targetId = $targetRecipient ? $transfer['recipient_id'] : $transfer['sender_id'];
            $targetLog = AppLog::shortId($targetId);

            $deviceRepo = new DeviceRepository($db);
            $token = $deviceRepo->findFcmToken($targetId);
            if ($token === null) {
                $result = 'no_token';
                return;
            }

            $ok = FcmSender::sendDataMessage($token, [
                'type' => $fcmType,
                'transfer_id' => $transferId,
            ]);
            if ($ok) {
                $deviceRepo->bumpFcmLastSuccessAt($targetId, time());
            }
            $result = $ok ? 'sent' : 'failed';
        } catch (\Throwable $e) {
            // FCM failure must never break the transfer flow.
            $result = 'failed';
        } finally {
            AppLog::log('Transfer', sprintf(
                '%s transfer_id=%s target=%s fcm_result=%s fcm_type=%s',
                $logEvent,
                AppLog::shortId($transferId),
                $targetLog,
                $result,
                $fcmType,
            ));
        }
    }
}
