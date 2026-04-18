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
        $baseline = self::sumSentChunksDownloaded($db, $deviceId);
        $state = ['pending' => false, 'delivered' => false, 'downloadProgress' => false];
        $start = time();

        do {
            $state = self::sampleState($db, $deviceId, $since, $baseline);
            if ($isTest || $state['pending'] || $state['delivered'] || $state['downloadProgress']) {
                break;
            }
            usleep(self::TICK_MICROSECONDS);
        } while (time() - $start < self::LONG_POLL_TIMEOUT);

        return self::buildResponse($db, $deviceId, $state, $isTest);
    }

    private static function sumSentChunksDownloaded(Database $db, string $deviceId): int
    {
        $row = $db->querySingle(
            'SELECT COALESCE(SUM(chunks_downloaded), 0) as total FROM transfers
             WHERE sender_id = :sid AND complete = 1 AND downloaded = 0',
            [':sid' => $deviceId]
        );
        return (int)($row['total'] ?? 0);
    }

    private static function sampleState(Database $db, string $deviceId, int $since, int $baseline): array
    {
        $pending = $db->querySingle(
            'SELECT COUNT(*) as count FROM transfers
             WHERE recipient_id = :rid AND complete = 1 AND downloaded = 0',
            [':rid' => $deviceId]
        );
        $delivered = $db->querySingle(
            'SELECT COUNT(*) as count FROM transfers
             WHERE sender_id = :sid AND delivered_at >= :since',
            [':sid' => $deviceId, ':since' => $since]
        );
        $currentProgress = self::sumSentChunksDownloaded($db, $deviceId);

        return [
            'pending' => ($pending['count'] ?? 0) > 0,
            'delivered' => ($delivered['count'] ?? 0) > 0,
            'downloadProgress' => $currentProgress !== $baseline,
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
