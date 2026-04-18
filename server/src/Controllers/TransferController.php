<?php

class TransferController
{
    public static function init(Database $db, string $deviceId): void
    {
        $body = Router::getJsonBody() ?? [];
        [$resp, $status] = TransferService::init($db, $deviceId, $body);
        Router::json($resp, $status);
    }

    public static function uploadChunk(Database $db, string $deviceId, array $params): void
    {
        [$resp, $status] = TransferService::uploadChunk(
            $db,
            $deviceId,
            $params['transfer_id'],
            (int)$params['chunk_index'],
            Router::getRawBody()
        );
        Router::json($resp, $status);
    }

    public static function pending(Database $db, string $deviceId): void
    {
        // 1-in-20 sampling of cleanup attached to an HTTP request path; policy
        // stays here so the cleanup service itself is deterministic/reusable.
        if (random_int(1, 20) === 1) {
            TransferCleanupService::run($db);
        }
        Router::json(['transfers' => TransferService::listPending($db, $deviceId)]);
    }

    public static function downloadChunk(Database $db, string $deviceId, array $params): void
    {
        [$resp, $status] = TransferService::downloadChunk(
            $db,
            $deviceId,
            $params['transfer_id'],
            (int)$params['chunk_index']
        );
        if (isset($resp['binary'])) {
            Router::binary($resp['binary'], $status);
        } else {
            Router::json($resp, $status);
        }
    }

    public static function ack(Database $db, string $deviceId, array $params): void
    {
        [$resp, $status] = TransferService::ack($db, $deviceId, $params['transfer_id']);
        Router::json($resp, $status);
    }

    public static function sentStatus(Database $db, string $deviceId): void
    {
        $rows = TransferStatusService::loadSentForDevice($db, $deviceId);
        Router::json(['transfers' => array_map(
            [TransferStatusService::class, 'formatSent'],
            $rows
        )]);
    }

    public static function notify(Database $db, string $deviceId): void
    {
        $since = isset($_GET['since']) ? (int)$_GET['since'] : 0;
        $isTest = !empty($_GET['test']);
        Router::json(TransferNotifyService::longPoll($db, $deviceId, $since, $isTest));
    }
}
