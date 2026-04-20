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

        $result = TransferService::init(
            $db,
            $ctx->deviceId,
            $transferId,
            $recipientId,
            $encryptedMeta,
            $chunkCount,
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
        if (random_int(1, 20) === 1) {
            TransferCleanupService::run($db);
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

    public static function cancel(Database $db, RequestContext $ctx): void
    {
        $transferId = Validators::requireSafeTransferId($ctx->params);
        Router::json(TransferService::cancel($db, $ctx->deviceId, $transferId));
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
