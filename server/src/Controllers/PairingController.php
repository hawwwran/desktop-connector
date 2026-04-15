<?php

class PairingController
{
    public static function request(Database $db, string $deviceId): void
    {
        $body = Router::getJsonBody();
        if (!$body || empty($body['desktop_id']) || empty($body['phone_pubkey'])) {
            Router::json(['error' => 'Missing desktop_id or phone_pubkey'], 400);
            return;
        }

        $desktopId = $body['desktop_id'];
        $phonePubkey = $body['phone_pubkey'];

        // Verify desktop device exists
        $desktop = $db->querySingle(
            'SELECT device_id FROM devices WHERE device_id = :id',
            [':id' => $desktopId]
        );
        if (!$desktop) {
            Router::json(['error' => 'Desktop device not found'], 404);
            return;
        }

        // Remove any existing unclaimed requests from this phone to this desktop
        $db->execute(
            'DELETE FROM pairing_requests WHERE phone_id = :phone AND desktop_id = :desktop AND claimed = 0',
            [':phone' => $deviceId, ':desktop' => $desktopId]
        );

        $db->execute(
            'INSERT INTO pairing_requests (desktop_id, phone_id, phone_pubkey, created_at)
             VALUES (:desktop, :phone, :pubkey, :now)',
            [
                ':desktop' => $desktopId,
                ':phone' => $deviceId,
                ':pubkey' => $phonePubkey,
                ':now' => time(),
            ]
        );

        Router::json(['status' => 'ok'], 201);
    }

    public static function poll(Database $db, string $deviceId): void
    {
        $requests = $db->queryAll(
            'SELECT id, phone_id, phone_pubkey FROM pairing_requests
             WHERE desktop_id = :desktop AND claimed = 0
             ORDER BY created_at ASC',
            [':desktop' => $deviceId]
        );

        // Mark as claimed
        foreach ($requests as $req) {
            $db->execute(
                'UPDATE pairing_requests SET claimed = 1 WHERE id = :id',
                [':id' => $req['id']]
            );
        }

        Router::json(['requests' => $requests]);
    }

    public static function confirm(Database $db, string $deviceId): void
    {
        $body = Router::getJsonBody();
        if (!$body || empty($body['phone_id'])) {
            Router::json(['error' => 'Missing phone_id'], 400);
            return;
        }

        $phoneId = $body['phone_id'];

        // Store the pairing (normalize order for uniqueness)
        $ids = [$deviceId, $phoneId];
        sort($ids);

        $existing = $db->querySingle(
            'SELECT id FROM pairings WHERE device_a_id = :a AND device_b_id = :b',
            [':a' => $ids[0], ':b' => $ids[1]]
        );

        if (!$existing) {
            $db->execute(
                'INSERT INTO pairings (device_a_id, device_b_id, created_at) VALUES (:a, :b, :now)',
                [':a' => $ids[0], ':b' => $ids[1], ':now' => time()]
            );
        }

        Router::json(['status' => 'ok']);
    }
}
