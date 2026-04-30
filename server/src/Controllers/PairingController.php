<?php

class PairingController
{
    public static function request(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $desktopId = Validators::requireNonEmptyString($body, 'desktop_id');
        $phonePubkey = Validators::requireNonEmptyString($body, 'phone_pubkey');

        // Verify desktop device exists
        if (!(new DeviceRepository($db))->findById($desktopId)) {
            throw new NotFoundError('Desktop device not found');
        }

        $pairings = new PairingRepository($db);
        if ($pairings->findPairing($desktopId, $ctx->deviceId)) {
            $pairings->deleteRequestsBetweenDevices($desktopId, $ctx->deviceId);
            Router::json(['status' => 'ok']);
            return;
        }

        // Remove any existing unclaimed requests from this phone to this desktop
        $pairings->deleteUnclaimedRequests($ctx->deviceId, $desktopId);
        $pairings->insertPairingRequest($desktopId, $ctx->deviceId, $phonePubkey, time());
        AppLog::log('Pairing', sprintf(
            'pairing.request.received desktop_id=%s phone_id=%s',
            AppLog::shortId($desktopId), AppLog::shortId($ctx->deviceId)
        ));

        Router::json(['status' => 'ok'], 201);
    }

    public static function poll(Database $db, RequestContext $ctx): void
    {
        $pairings = new PairingRepository($db);
        $requests = $pairings->listUnclaimedRequestsForDesktop($ctx->deviceId);

        foreach ($requests as $req) {
            $pairings->markRequestClaimed((int)$req['id']);
        }

        if (!empty($requests)) {
            AppLog::log('Pairing', sprintf(
                'pairing.request.claimed desktop_id=%s count=%d',
                AppLog::shortId($ctx->deviceId), count($requests)
            ));
        }

        Router::json(['requests' => $requests]);
    }

    public static function confirm(Database $db, RequestContext $ctx): void
    {
        $body = $ctx->jsonBody();
        $phoneId = Validators::requireNonEmptyString($body, 'phone_id');

        $ids = [$ctx->deviceId, $phoneId];
        sort($ids);

        $pairings = new PairingRepository($db);
        if (!$pairings->findSortedPairing($ids[0], $ids[1])) {
            $pairings->createPairing($ids[0], $ids[1], time());
            AppLog::log('Pairing', sprintf(
                'pairing.confirm.accepted device_a=%s device_b=%s',
                AppLog::shortId($ids[0]), AppLog::shortId($ids[1])
            ));
        }
        $pairings->deleteRequestsBetweenDevices($ctx->deviceId, $phoneId);

        Router::json(['status' => 'ok']);
    }
}
