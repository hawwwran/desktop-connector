<?php

/**
 * Long-poll service for /api/transfers/notify.
 *
 * Blocks up to LONG_POLL_TIMEOUT seconds, waking on any of:
 *   - new pending (incoming, complete, not yet downloaded)
 *   - new delivered (outgoing, delivered_at >= $since)
 *   - download progress (recipient has pulled more chunks of our sent transfers)
 *
 * The initial progress snapshot is taken ONCE before the loop so each tick
 * compares against a fixed baseline — recomputing the baseline inside the
 * loop would mask progress that happens during the poll.
 */
class TransferNotifyService
{
    private const LONG_POLL_TIMEOUT = 25;
    private const TICK_MICROSECONDS = 500000;

    public static function longPoll(Database $db, string $deviceId, int $since, bool $isTest): array
    {
        $transfers = new TransferRepository($db);
        $baseline = $transfers->sumSentChunksDownloaded($deviceId);
        $state = ['pending' => false, 'delivered' => false, 'downloadProgress' => false];
        $start = time();

        do {
            $state = self::sampleState($transfers, $deviceId, $since, $baseline);
            if ($isTest || $state['pending'] || $state['delivered'] || $state['downloadProgress']) {
                break;
            }
            usleep(self::TICK_MICROSECONDS);
        } while (time() - $start < self::LONG_POLL_TIMEOUT);

        return self::buildResponse($db, $deviceId, $state, $isTest);
    }

    private static function sampleState(TransferRepository $transfers, string $deviceId, int $since, int $baseline): array
    {
        return [
            'pending' => $transfers->countPendingForRecipient($deviceId) > 0,
            'delivered' => $transfers->countDeliveredSinceForSender($deviceId, $since) > 0,
            'downloadProgress' => $transfers->sumSentChunksDownloaded($deviceId) !== $baseline,
        ];
    }

    private static function buildResponse(Database $db, string $deviceId, array $state, bool $isTest): array
    {
        $response = [
            'pending' => $state['pending'],
            'delivered' => $state['delivered'],
            'download_progress' => $state['downloadProgress'],
            'time' => time(),
        ];
        if ($isTest) {
            $response['test'] = true;
        }
        if ($state['downloadProgress'] || $state['delivered']) {
            $sent = TransferStatusService::loadSentForDevice($db, $deviceId, 50, true);
            $response['sent_status'] = array_map(
                [TransferStatusService::class, 'formatSentBrief'],
                $sent
            );
        }
        return $response;
    }
}
