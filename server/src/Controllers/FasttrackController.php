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

        $deviceId = $ctx->deviceId;

        // Validate pairing exists (check both orderings)
        $pairing = $db->querySingle(
            'SELECT id FROM pairings
             WHERE (device_a_id = :a AND device_b_id = :b)
                OR (device_a_id = :b2 AND device_b_id = :a2)',
            [':a' => $deviceId, ':b' => $recipientId,
             ':a2' => $deviceId, ':b2' => $recipientId]
        );
        if (!$pairing) {
            throw new ForbiddenError('Devices are not paired');
        }

        // Clean up expired messages for this recipient
        $db->execute(
            'DELETE FROM fasttrack_messages WHERE recipient_id = :rid AND created_at < :cutoff',
            [':rid' => $recipientId, ':cutoff' => time() - self::MESSAGE_EXPIRY]
        );

        // Enforce max pending limit
        $count = $db->querySingle(
            'SELECT COUNT(*) as cnt FROM fasttrack_messages WHERE recipient_id = :rid',
            [':rid' => $recipientId]
        );
        if ($count && $count['cnt'] >= self::MAX_PENDING) {
            // Plain 429 without Retry-After to match the pre-refactor wire shape;
            // ping's 429 is the one that carries the header.
            throw new ApiError(429, 'Too many pending messages');
        }

        AppLog::log('Fasttrack', "send from={$deviceId} to={$recipientId} size=" . strlen($encryptedData));

        $db->execute(
            'INSERT INTO fasttrack_messages (sender_id, recipient_id, encrypted_data, created_at)
             VALUES (:sid, :rid, :data, :now)',
            [':sid' => $deviceId, ':rid' => $recipientId,
             ':data' => $encryptedData, ':now' => time()]
        );
        $messageId = $db->lastInsertId();

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

        AppLog::log('Fasttrack', "stored message_id={$messageId}, FCM={$fcmSent}");
        Router::json(['message_id' => $messageId], 201);
    }

    /**
     * GET /api/fasttrack/pending — fetch all pending messages for this device.
     * Returns: {messages: [{id, sender_id, encrypted_data, created_at}, ...]}
     */
    public static function pending(Database $db, RequestContext $ctx): void
    {
        $deviceId = $ctx->deviceId;

        // Clean up expired messages first
        $db->execute(
            'DELETE FROM fasttrack_messages WHERE recipient_id = :rid AND created_at < :cutoff',
            [':rid' => $deviceId, ':cutoff' => time() - self::MESSAGE_EXPIRY]
        );

        $messages = $db->queryAll(
            'SELECT id, sender_id, encrypted_data, created_at
             FROM fasttrack_messages
             WHERE recipient_id = :rid
             ORDER BY created_at ASC',
            [':rid' => $deviceId]
        );

        if (!empty($messages)) {
            AppLog::log('Fasttrack', "pending for={$deviceId} count=" . count($messages));
        }
        Router::json(['messages' => $messages]);
    }

    /**
     * POST /api/fasttrack/{id}/ack — acknowledge and delete a message.
     * Validates that the caller is the recipient.
     */
    public static function ack(Database $db, RequestContext $ctx): void
    {
        $messageId = Validators::requireIntParam($ctx->params, 'id');

        $msg = $db->querySingle(
            'SELECT recipient_id FROM fasttrack_messages WHERE id = :id',
            [':id' => $messageId]
        );
        if (!$msg) {
            throw new NotFoundError('Message not found');
        }
        if ($msg['recipient_id'] !== $ctx->deviceId) {
            throw new ForbiddenError('Not authorized');
        }

        $db->execute(
            'DELETE FROM fasttrack_messages WHERE id = :id',
            [':id' => $messageId]
        );

        AppLog::log('Fasttrack', "ack id={$messageId} by={$ctx->deviceId}");
        Router::json(['status' => 'ok']);
    }
}
