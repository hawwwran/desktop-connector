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
        AppLog::log('Poll', sprintf(
            'poll.notify.started device_id=%s since=%d is_test=%s',
            AppLog::shortId($deviceId), $since, $isTest ? 'true' : 'false'
        ), 'debug');

        do {
            $state = self::sampleState($transfers, $deviceId, $since, $baseline);
            if ($isTest || $state['pending'] || $state['delivered'] || $state['downloadProgress']) {
                break;
            }
            usleep(self::TICK_MICROSECONDS);
        } while (time() - $start < self::LONG_POLL_TIMEOUT);

        $elapsed = time() - $start;
        $woken = $state['pending'] || $state['delivered'] || $state['downloadProgress'];
        if (!$isTest) {
            if ($woken) {
                AppLog::log('Poll', sprintf(
                    'poll.notify.event device_id=%s elapsed=%ds pending=%s delivered=%s progress=%s',
                    AppLog::shortId($deviceId), $elapsed,
                    $state['pending'] ? 'true' : 'false',
                    $state['delivered'] ? 'true' : 'false',
                    $state['downloadProgress'] ? 'true' : 'false'
                ));
            } elseif ($elapsed >= self::LONG_POLL_TIMEOUT) {
                AppLog::log('Poll', sprintf(
                    'poll.notify.timeout device_id=%s',
                    AppLog::shortId($deviceId)
                ));
            }
        }

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
