<?php

/**
 * HTTP adapter for /api/transfers/*. Validates input off the RequestContext,
 * dispatches to a service in src/Services/, and serializes the result via
 * Router::json / Router::binary. Business-level errors surface as ApiError
 * exceptions from the service layer and are caught by the Router.
 */
class TransferController
{
    public static function init(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $transferId = Validators::requireSafeTransferId($body, 'transfer_id');
        $recipientId = Validators::requireNonEmptyString($body, 'recipient_id');
        $encryptedMeta = Validators::requireNonEmptyString($body, 'encrypted_meta');
        $chunkCount = Validators::requireInt($body, 'chunk_count');
        // `mode` is optional — absent / empty / "classic" → classic for
        // every old client. Any unknown value is rejected by the service
        // with 400 so a typo in a new client doesn't silently degrade.
        $mode = null;
        if (array_key_exists('mode', $body) && $body['mode'] !== null) {
            if (!is_string($body['mode'])) {
                throw new ValidationError('Invalid mode');
            }
            $mode = $body['mode'];
        }

        $result = TransferService::init(
            $db,
            $ctx->deviceId,
            $transferId,
            $recipientId,
            $encryptedMeta,
            $chunkCount,
            $mode,
        );
        Router::json($result, 201);
    }

    public static function uploadChunk(Database $db, RequestContext $ctx): void
    {
        $transferId = Validators::requireSafeTransferId($ctx->params);
        $chunkIndex = Validators::requireIntParam($ctx->params, 'chunk_index', min: 0);

        $result = TransferService::uploadChunk(
            $db,
            $ctx->deviceId,
            $transferId,
            $chunkIndex,
            $ctx->rawBody(),
        );
        Router::json($result);
    }

    public static function pending(Database $db, RequestContext $ctx): void
    {
        // 1-in-20 sampling of cleanup attached to an HTTP request path; policy
        // stays here so the cleanup service itself is deterministic/reusable.
        // Wrapped: opportunistic cleanup must never poison the response —
        // /pending is on the desktop's connection-state hot path, so a 500
        // here flips the UI to disconnected on every sampling hit.
        if (random_int(1, 20) === 1) {
            try {
                TransferCleanupService::run($db);
            } catch (\Throwable $e) {
                AppLog::log('Transfer', 'transfer.cleanup.failed reason=' . $e->getMessage(), 'warning');
            }
        }
        Router::json(['transfers' => TransferService::listPending($db, $ctx->deviceId)]);
    }

    public static function downloadChunk(Database $db, RequestContext $ctx): void
    {
        $transferId = Validators::requireSafeTransferId($ctx->params);
        $chunkIndex = Validators::requireIntParam($ctx->params, 'chunk_index', min: 0);

        $bytes = TransferService::downloadChunk($db, $ctx->deviceId, $transferId, $chunkIndex);
        Router::binary($bytes);
    }

    public static function ack(Database $db, RequestContext $ctx): void
    {
        $transferId = Validators::requireSafeTransferId($ctx->params);
        Router::json(TransferService::ack($db, $ctx->deviceId, $transferId));
    }

    public static function ackChunk(Database $db, RequestContext $ctx): void
    {
        $transferId = Validators::requireSafeTransferId($ctx->params);
        $chunkIndex = Validators::requireIntParam($ctx->params, 'chunk_index', min: 0);
        Router::json(TransferService::ackChunk($db, $ctx->deviceId, $transferId, $chunkIndex));
    }

    /**
     * DELETE /api/transfers/{id} — unified sender/recipient abort.
     *
     * Default reason is the caller-role-appropriate one ('sender_abort'
     * or 'recipient_abort'). Clients MAY pass a `reason` in the JSON
     * body to override to 'sender_failed' (sender gave up after retry
     * exhaustion). Sender callers flow through the `cancel()` alias
     * which preserves the old `status: "cancelled"` on-wire shape so
     * pre-streaming release builds keep parsing the response.
     *
     * Reason validation is explicit at the HTTP boundary so a typoed
     * or cross-role reason ("sender passing recipient_abort") surfaces
     * as a 400 instead of being silently coerced. `abort()` revalidates
     * in the service layer.
     */
    public static function cancel(Database $db, RequestContext $ctx): void
    {
        $transferId = Validators::requireSafeTransferId($ctx->params);
        $transfer = (new TransferRepository($db))->findById($transferId);
        if (!$transfer) {
            throw new NotFoundError('Transfer not found');
        }
        $deviceId = $ctx->deviceId;
        $body = $ctx->jsonBody();
        $reason = isset($body['reason']) && is_string($body['reason']) ? $body['reason'] : null;

        if ($transfer['recipient_id'] === $deviceId) {
            if ($reason !== null && $reason !== 'recipient_abort') {
                throw new ValidationError('Invalid reason for recipient abort');
            }
            Router::json(TransferService::abort($db, $deviceId, $transferId, 'recipient_abort'));
            return;
        }
        // Sender-side (or unknown caller — abort() 404s the latter).
        if ($reason !== null && !in_array($reason, ['sender_abort', 'sender_failed'], true)) {
            throw new ValidationError('Invalid reason for sender abort');
        }
        if ($reason === 'sender_failed') {
            Router::json(TransferService::abort($db, $deviceId, $transferId, 'sender_failed'));
            return;
        }
        Router::json(TransferService::cancel($db, $deviceId, $transferId));
    }

    public static function sentStatus(Database $db, RequestContext $ctx): void
    {
        $rows = TransferStatusService::loadSentForDevice($db, $ctx->deviceId);
        Router::json(['transfers' => array_map(
            [TransferStatusService::class, 'formatSent'],
            $rows
        )]);
    }

    public static function notify(Database $db, RequestContext $ctx): void
    {
        $since = isset($ctx->query['since']) ? (int)$ctx->query['since'] : 0;
        $isTest = !empty($ctx->query['test']);
        Router::json(TransferNotifyService::longPoll($db, $ctx->deviceId, $since, $isTest));
    }
}
