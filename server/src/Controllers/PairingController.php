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
        // Remove any existing unclaimed requests from this phone to this desktop
        $pairings->deleteUnclaimedRequests($ctx->deviceId, $desktopId);
        $pairings->insertPairingRequest($desktopId, $ctx->deviceId, $phonePubkey, time());

        Router::json(['status' => 'ok'], 201);
    }

    public static function poll(Database $db, RequestContext $ctx): void
    {
        $pairings = new PairingRepository($db);
        $requests = $pairings->listUnclaimedRequestsForDesktop($ctx->deviceId);

        foreach ($requests as $req) {
            $pairings->markRequestClaimed((int)$req['id']);
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
        }

        Router::json(['status' => 'ok']);
    }
}
