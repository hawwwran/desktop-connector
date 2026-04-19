<?php

class DeviceController
{
    public static function register(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $publicKey = Validators::requireNonEmptyString($body, 'public_key');
        $deviceType = isset($body['device_type']) && is_string($body['device_type'])
            ? $body['device_type']
            : 'unknown';

        // Compute device_id: first 32 hex chars of SHA-256 of raw public key bytes
        $rawKey = base64_decode($publicKey, true);
        if ($rawKey === false || strlen($rawKey) !== 32) {
            throw new ValidationError('Invalid public_key: must be 32 bytes base64-encoded');
        }

        $deviceId = substr(hash('sha256', $rawKey), 0, 32);
        $devices = new DeviceRepository($db);

        // Check if already registered — return existing credentials
        $existing = $devices->findById($deviceId);
        if ($existing) {
            Router::json([
                'device_id' => $existing['device_id'],
                'auth_token' => $existing['auth_token'],
            ]);
            return;
        }

        $authToken = bin2hex(random_bytes(32));
        $now = time();

        $devices->insertDevice($deviceId, $publicKey, $authToken, $deviceType, $now);

        Router::json([
            'device_id' => $deviceId,
            'auth_token' => $authToken,
        ], 201);
    }

    public static function stats(Database $db, RequestContext $ctx): void
    {
        $deviceId = $ctx->deviceId;
        $devices = new DeviceRepository($db);

        $device = $devices->findById($deviceId);

        $pairings = (new PairingRepository($db))->listPairingsForDevice($deviceId);

        $pairedDevices = [];
        foreach ($pairings as $p) {
            $otherId = $p['device_a_id'] === $deviceId ? $p['device_b_id'] : $p['device_a_id'];
            $other = $devices->findById($otherId);
            $pairedDevices[] = [
                'device_id' => $otherId,
                'device_type' => $other ? $other['device_type'] : 'unknown',
                'last_seen' => $other ? (int)$other['last_seen_at'] : 0,
                'online' => $other && (time() - $other['last_seen_at']) < 120,
                'transfers' => (int)$p['transfer_count'],
                'bytes_transferred' => (int)$p['bytes_transferred'],
                'paired_since' => (int)$p['created_at'],
            ];
        }

        // Pending transfers — narrow to the currently paired device when caller supplies it.
        $pairedId = $ctx->query['paired_with'] ?? null;
        $transfers = new TransferRepository($db);

        if ($pairedId) {
            $pendingIn = $transfers->countPendingIncomingForPair($deviceId, $pairedId);
            $pendingOutCount = $transfers->countPendingOutgoingForPair($deviceId, $pairedId);
        } else {
            $pendingIn = $transfers->countPendingIncomingForDevice($deviceId);
            $pendingOutCount = $transfers->countPendingOutgoingForDevice($deviceId);
        }

        Router::json([
            'device_id' => $deviceId,
            'device_type' => $device ? $device['device_type'] : 'unknown',
            'registered_at' => $device ? (int)$device['created_at'] : 0,
            'last_seen_at' => $device ? (int)$device['last_seen_at'] : 0,
            'paired_devices' => $pairedDevices,
            'pending_incoming' => (int)($pendingIn['count'] ?? 0),
            'pending_outgoing' => $pendingOutCount,
        ]);
    }

    public static function updateFcmToken(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        // Null is a valid value — clears the stored token.
        $token = Validators::requireNullableString($body, 'fcm_token');

        (new DeviceRepository($db))->updateFcmToken($ctx->deviceId, $token);

        Router::json(['status' => 'ok']);
    }

    /**
     * POST /api/devices/ping — probe whether a paired device is online via FCM.
     * Body: {recipient_id}
     * Sends HIGH-priority FCM ping; polls recipient's last_seen_at for up to 5s
     * waiting for their pong. Returns {online, last_seen_at, rtt_ms, via}.
     *
     * Rate-limited per (sender, recipient) pair via an atomic UPSERT on
     * ping_rate. PING_COOLDOWN_SEC exceeds PING_MAX_WAIT_SEC, so concurrent
     * pings for the same pair are always rejected (debounce) AND callers are
     * capped to 1 ping per 30s (overload / battery protection).
     * Note: same single-PHP-worker caveat as /api/transfers/notify.
     */
    private const PING_COOLDOWN_SEC = 30;
    private const PING_MAX_WAIT_SEC = 5;

    public static function ping(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $recipientId = Validators::requireNonEmptyString($body, 'recipient_id');
        $deviceId = $ctx->deviceId;

        if (!(new PairingRepository($db))->findPairing($deviceId, $recipientId)) {
            throw new ForbiddenError('Devices are not paired');
        }

        // Atomic rate-limit + concurrent-ping claim.
        // SQLite WAL serializes writers, so the UPSERT's WHERE clause is race-safe:
        //   - fresh row     → INSERT wins, changes()==1
        //   - expired slot  → UPDATE wins, changes()==1
        //   - live slot     → UPDATE's WHERE fails, changes()==0 → reject
        $now = time();
        $claimUntil = $now + self::PING_COOLDOWN_SEC;
        $db->execute(
            'INSERT INTO ping_rate (sender_id, recipient_id, cooldown_until)
             VALUES (:s, :r, :until)
             ON CONFLICT(sender_id, recipient_id) DO UPDATE
             SET cooldown_until = excluded.cooldown_until
             WHERE ping_rate.cooldown_until <= :now',
            [':s' => $deviceId, ':r' => $recipientId,
             ':until' => $claimUntil, ':now' => $now]
        );
        if ($db->changes() === 0) {
            $row = $db->querySingle(
                'SELECT cooldown_until FROM ping_rate WHERE sender_id = :s AND recipient_id = :r',
                [':s' => $deviceId, ':r' => $recipientId]
            );
            $retryAfter = $row ? max(1, (int)$row['cooldown_until'] - $now) : 1;
            throw new RateLimitError(
                'Rate limit: ping already in flight or too recent',
                retryAfter: $retryAfter,
            );
        }

        $devices = new DeviceRepository($db);
        $recipient = $devices->findById($recipientId);
        if (!$recipient) {
            throw new NotFoundError('Recipient not found');
        }

        $baseline = $now;
        $prevLastSeen = (int)($recipient['last_seen_at'] ?? 0);

        // If recipient talked to the server this second, skip FCM — they're online.
        if ($prevLastSeen >= $baseline) {
            Router::json([
                'online' => true,
                'last_seen_at' => $prevLastSeen,
                'rtt_ms' => 0,
                'via' => 'fresh',
            ]);
            return;
        }

        if (empty($recipient['fcm_token']) || !FcmSender::isAvailable()) {
            Router::json([
                'online' => false,
                'last_seen_at' => $prevLastSeen,
                'rtt_ms' => 0,
                'via' => 'no_fcm',
            ]);
            return;
        }

        $start = microtime(true);
        if (!FcmSender::sendDataMessage($recipient['fcm_token'], ['type' => 'ping'])) {
            Router::json([
                'online' => false,
                'last_seen_at' => $prevLastSeen,
                'rtt_ms' => (int)((microtime(true) - $start) * 1000),
                'via' => 'fcm_failed',
            ]);
            return;
        }

        $timeoutMs = self::PING_MAX_WAIT_SEC * 1000;
        while ((microtime(true) - $start) * 1000 < $timeoutMs) {
            $curr = $devices->findById($recipientId);
            if ($curr && (int)$curr['last_seen_at'] >= $baseline) {
                Router::json([
                    'online' => true,
                    'last_seen_at' => (int)$curr['last_seen_at'],
                    'rtt_ms' => (int)((microtime(true) - $start) * 1000),
                    'via' => 'fcm',
                ]);
                return;
            }
            usleep(100000); // 100ms
        }

        Router::json([
            'online' => false,
            'last_seen_at' => $prevLastSeen,
            'rtt_ms' => (int)((microtime(true) - $start) * 1000),
            'via' => 'fcm_timeout',
        ]);
    }

    /**
     * POST /api/devices/pong — phone calls this when it receives a ping FCM.
     * Router auth already bumps last_seen_at; this just acks.
     */
    public static function pong(Database $db, RequestContext $ctx): void
    {
        Router::json(['ok' => true, 't' => time()]);
    }

    /**
     * GET /api/health — public, but doubles as a heartbeat when auth headers
     * are sent. AuthService::optional bumps `last_seen_at` on a successful
     * lookup; missing or invalid credentials silently return unauth'd.
     */
    public static function health(Database $db, RequestContext $ctx): void
    {
        AuthService::optional($db);
        Router::json([
            'status' => 'ok',
            'time' => time(),
        ]);
    }
}
