<?php

class PairingController
{
    public static function request(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $desktopId = Validators::requireNonEmptyString($body, 'desktop_id');
        $phonePubkey = Validators::requireNonEmptyString($body, 'phone_pubkey');

        // Verify desktop device exists
        $desktop = $db->querySingle(
            'SELECT device_id FROM devices WHERE device_id = :id',
            [':id' => $desktopId]
        );
        if (!$desktop) {
            throw new NotFoundError('Desktop device not found');
        }

        // Remove any existing unclaimed requests from this phone to this desktop
        $db->execute(
            'DELETE FROM pairing_requests WHERE phone_id = :phone AND desktop_id = :desktop AND claimed = 0',
            [':phone' => $ctx->deviceId, ':desktop' => $desktopId]
        );

        $db->execute(
            'INSERT INTO pairing_requests (desktop_id, phone_id, phone_pubkey, created_at)
             VALUES (:desktop, :phone, :pubkey, :now)',
            [
                ':desktop' => $desktopId,
                ':phone' => $ctx->deviceId,
                ':pubkey' => $phonePubkey,
                ':now' => time(),
            ]
        );

        Router::json(['status' => 'ok'], 201);
    }

    public static function poll(Database $db, RequestContext $ctx): void
    {
        $requests = $db->queryAll(
            'SELECT id, phone_id, phone_pubkey FROM pairing_requests
             WHERE desktop_id = :desktop AND claimed = 0
             ORDER BY created_at ASC',
            [':desktop' => $ctx->deviceId]
        );

        foreach ($requests as $req) {
            $db->execute(
                'UPDATE pairing_requests SET claimed = 1 WHERE id = :id',
                [':id' => $req['id']]
            );
        }

        Router::json(['requests' => $requests]);
    }

    public static function confirm(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $phoneId = Validators::requireNonEmptyString($body, 'phone_id');

        $ids = [$ctx->deviceId, $phoneId];
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
