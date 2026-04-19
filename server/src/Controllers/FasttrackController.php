<?php

/**
 * Fasttrack: lightweight encrypted message relay between paired devices.
 * The server never sees message content — all payloads are E2E encrypted.
 * Function-agnostic: used for find-phone, and any future lightweight commands.
 */
class FasttrackController
{
    private const MESSAGE_EXPIRY = 600; // 10 minutes
    private const MAX_PENDING = 100;    // max messages per recipient

    /**
     * POST /api/fasttrack/send — send an encrypted message to a paired device.
     * Body: {recipient_id, encrypted_data}
     * Validates pairing, stores message, sends FCM wake.
     */
    public static function send(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $recipientId = Validators::requireNonEmptyString($body, 'recipient_id');
        $encryptedData = Validators::requireNonEmptyString($body, 'encrypted_data');
        $payloadSize = strlen($encryptedData);

        $deviceId = $ctx->deviceId;

        // Validate pairing exists (order-independent)
        if (!(new PairingRepository($db))->findPairing($deviceId, $recipientId)) {
            throw new ForbiddenError('Devices are not paired');
        }

        $messages = new FasttrackRepository($db);
        // Clean up expired messages for this recipient
        $messages->deleteExpiredForRecipient($recipientId, time() - self::MESSAGE_EXPIRY);

        // Enforce max pending limit
        $pendingCount = $messages->countPendingForRecipient($recipientId);
        if ($pendingCount >= self::MAX_PENDING) {
            AppLog::log('Fasttrack', sprintf(
                'fasttrack.message.rate_limited sender=%s recipient=%s pending_count=%d',
                AppLog::shortId($deviceId), AppLog::shortId($recipientId), $pendingCount
            ), 'warning');
            // Plain 429 without Retry-After to match the pre-refactor wire shape;
            // ping's 429 is the one that carries the header.
            throw new ApiError(429, 'Too many pending messages');
        }

        if (!MessageTransportPolicy::isFasttrackPayloadSizeAllowed($payloadSize)) {
            throw new ValidationError(sprintf(
                'Encrypted payload too large for fasttrack (max %d bytes)',
                MessageTransportPolicy::fasttrackMaxEncryptedBytes()
            ));
        }

        AppLog::log('Fasttrack', sprintf(
            'fasttrack.message.send_received sender=%s recipient=%s size=%d',
            AppLog::shortId($deviceId), AppLog::shortId($recipientId), $payloadSize
        ));

        $messageId = $messages->insertMessage($deviceId, $recipientId, $encryptedData, time());

        // Send FCM wake to recipient (fire-and-forget, no content leaked)
        $fcmSent = 'no_fcm';
        if (FcmSender::isAvailable()) {
            $token = (new DeviceRepository($db))->findFcmToken($recipientId);
            if ($token !== null) {
                $ok = FcmSender::sendDataMessage($token, ['type' => 'fasttrack']);
                $fcmSent = $ok ? 'sent' : 'failed';
            } else {
                $fcmSent = 'no_token';
            }
        }

        AppLog::log('Fasttrack', sprintf(
            'fasttrack.message.stored message_id=%d fcm_result=%s',
            $messageId, $fcmSent
        ));
        Router::json(['message_id' => $messageId], 201);
    }

    /**
     * GET /api/fasttrack/pending — fetch all pending messages for this device.
     * Returns: {messages: [{id, sender_id, encrypted_data, created_at}, ...]}
     */
    public static function pending(Database $db, RequestContext $ctx): void
    {
        $deviceId = $ctx->deviceId;
        $messages = new FasttrackRepository($db);

        // Clean up expired messages first
        $messages->deleteExpiredForRecipient($deviceId, time() - self::MESSAGE_EXPIRY);
        $pending = $messages->listPendingForRecipient($deviceId);

        if (!empty($pending)) {
            AppLog::log('Fasttrack', sprintf(
                'fasttrack.message.pending_listed recipient=%s count=%d',
                AppLog::shortId($deviceId), count($pending)
            ), 'debug');
        }
        Router::json(['messages' => $pending]);
    }

    /**
     * POST /api/fasttrack/{id}/ack — acknowledge and delete a message.
     * Validates that the caller is the recipient.
     */
    public static function ack(Database $db, RequestContext $ctx): void
    {
        $messageId = Validators::requireIntParam($ctx->params, 'id');
        $messages = new FasttrackRepository($db);

        $msg = $messages->findById($messageId);
        if (!$msg) {
            throw new NotFoundError('Message not found');
        }
        if ($msg['recipient_id'] !== $ctx->deviceId) {
            throw new ForbiddenError('Not authorized');
        }

        $messages->deleteById($messageId);

        AppLog::log('Fasttrack', sprintf(
            'fasttrack.message.acked message_id=%d by=%s',
            $messageId, AppLog::shortId($ctx->deviceId)
        ));
        Router::json(['status' => 'ok']);
    }
}
