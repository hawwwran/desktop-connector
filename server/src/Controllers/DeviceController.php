<?php

class DeviceController
{
    public static function register(Database $db): void
    {
        $body = Router::getJsonBody();
        if (!$body || empty($body['public_key'])) {
            Router::json(['error' => 'Missing public_key'], 400);
            return;
        }

        $publicKey = $body['public_key'];
        $deviceType = $body['device_type'] ?? 'unknown';

        // Compute device_id: first 32 hex chars of SHA-256 of raw public key bytes
        $rawKey = base64_decode($publicKey, true);
        if ($rawKey === false || strlen($rawKey) !== 32) {
            Router::json(['error' => 'Invalid public_key: must be 32 bytes base64-encoded'], 400);
            return;
        }

        $deviceId = substr(hash('sha256', $rawKey), 0, 32);

        // Check if already registered
        $existing = $db->querySingle(
            'SELECT device_id, auth_token FROM devices WHERE device_id = :id',
            [':id' => $deviceId]
        );

        if ($existing) {
            // Return existing credentials
            Router::json([
                'device_id' => $existing['device_id'],
                'auth_token' => $existing['auth_token'],
            ]);
            return;
        }

        // Generate auth token
        $authToken = bin2hex(random_bytes(32));
        $now = time();

        $db->execute(
            'INSERT INTO devices (device_id, public_key, auth_token, device_type, created_at, last_seen_at)
             VALUES (:id, :key, :token, :type, :now, :now)',
            [
                ':id' => $deviceId,
                ':key' => $publicKey,
                ':token' => $authToken,
                ':type' => $deviceType,
                ':now' => $now,
            ]
        );

        Router::json([
            'device_id' => $deviceId,
            'auth_token' => $authToken,
        ], 201);
    }

    public static function stats(Database $db, string $deviceId): void
    {
        // Get this device info
        $device = $db->querySingle(
            'SELECT * FROM devices WHERE device_id = :id',
            [':id' => $deviceId]
        );

        // Get pairing stats
        $pairings = $db->queryAll(
            'SELECT * FROM pairings WHERE device_a_id = :id OR device_b_id = :id',
            [':id' => $deviceId]
        );

        $pairedDevices = [];
        foreach ($pairings as $p) {
            $otherId = $p['device_a_id'] === $deviceId ? $p['device_b_id'] : $p['device_a_id'];
            $other = $db->querySingle(
                'SELECT device_id, device_type, last_seen_at FROM devices WHERE device_id = :id',
                [':id' => $otherId]
            );
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

        // Pending transfers for this device (only to/from currently paired device)
        $pairedId = $_GET['paired_with'] ?? null;

        if ($pairedId) {
            $pendingIn = $db->querySingle(
                'SELECT COUNT(*) as count, COALESCE(SUM(chunk_count), 0) as chunks
                 FROM transfers WHERE recipient_id = :id AND sender_id = :paired AND complete = 1 AND downloaded = 0',
                [':id' => $deviceId, ':paired' => $pairedId]
            );
            $pendingOut = $db->querySingle(
                'SELECT COUNT(*) as count FROM transfers
                 WHERE sender_id = :id AND recipient_id = :paired AND downloaded = 0',
                [':id' => $deviceId, ':paired' => $pairedId]
            );
        } else {
            $pendingIn = $db->querySingle(
                'SELECT COUNT(*) as count, COALESCE(SUM(chunk_count), 0) as chunks
                 FROM transfers WHERE recipient_id = :id AND complete = 1 AND downloaded = 0',
                [':id' => $deviceId]
            );
            $pendingOut = $db->querySingle(
                'SELECT COUNT(*) as count FROM transfers
                 WHERE sender_id = :id AND downloaded = 0',
                [':id' => $deviceId]
            );
        }

        Router::json([
            'device_id' => $deviceId,
            'device_type' => $device ? $device['device_type'] : 'unknown',
            'registered_at' => $device ? (int)$device['created_at'] : 0,
            'last_seen_at' => $device ? (int)$device['last_seen_at'] : 0,
            'paired_devices' => $pairedDevices,
            'pending_incoming' => (int)($pendingIn['count'] ?? 0),
            'pending_outgoing' => (int)($pendingOut['count'] ?? 0),
        ]);
    }

    public static function updateFcmToken(Database $db, string $deviceId): void
    {
        $body = Router::getJsonBody();
        if (!$body || !array_key_exists('fcm_token', $body)) {
            Router::json(['error' => 'Missing fcm_token'], 400);
            return;
        }

        $token = $body['fcm_token']; // string or null (to clear)
        $db->execute(
            'UPDATE devices SET fcm_token = :token WHERE device_id = :id',
            [':token' => $token, ':id' => $deviceId]
        );

        Router::json(['status' => 'ok']);
    }

    public static function health(Database $db = null): void
    {
        // If auth headers present, update last_seen (acts as heartbeat)
        if ($db !== null) {
            $deviceId = $_SERVER['HTTP_X_DEVICE_ID'] ?? null;
            $authHeader = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
            if ($deviceId && str_starts_with($authHeader, 'Bearer ')) {
                $token = substr($authHeader, 7);
                $device = $db->querySingle(
                    'SELECT device_id FROM devices WHERE device_id = :id AND auth_token = :token',
                    [':id' => $deviceId, ':token' => $token]
                );
                if ($device) {
                    $db->execute(
                        'UPDATE devices SET last_seen_at = :now WHERE device_id = :id',
                        [':now' => time(), ':id' => $deviceId]
                    );
                }
            }
        }

        Router::json([
            'status' => 'ok',
            'time' => time(),
        ]);
    }
}
